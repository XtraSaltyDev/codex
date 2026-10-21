#!/usr/bin/env python3
"""Verify review retry after a real native launch failure; remove the owned chat and folder."""
import argparse
import asyncio
import contextlib
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import tempfile
import time
import tomllib

from aiohttp import ClientSession, ClientTimeout
from health import RPC
from smoke_mobile_activity import inventory
from smoke_thread_titles import Phone
from thread_titles import isolated_config

ONCE = 'RECOVERY-ONCE\n'


async def until(check, timeout=120):
    async with asyncio.timeout(timeout):
        while not check():
            await asyncio.sleep(.1)


async def main(root, hold):
    root.mkdir(parents=True, exist_ok=True)
    home = Path.home()
    config_path = home / '.codex-spark/config.toml'
    config = tomllib.loads(config_path.read_text())
    protected = [config_path, Path(config['model_catalog_json']), home / '.local/lib/codex-spark/codex']
    hashes = [hashlib.sha256(path.read_bytes()).hexdigest() for path in protected]
    project = Path(tempfile.mkdtemp(prefix='codex-review-retry-'))
    counter = project / 'recovery-counter.txt'; counter.write_text('')
    subprocess.run(['git', '-C', str(project), 'init', '-q'], check=True)
    subprocess.run(['git', '-C', str(project), 'add', 'recovery-counter.txt'], check=True)
    subprocess.run(['git', '-C', str(project), '-c', 'user.name=Recovery fixture', '-c', 'user.email=fixture@example.invalid', 'commit', '-qm', 'Fixture baseline'], check=True)
    overrides = {**isolated_config(config), 'features.shell_tool': True, 'features.unified_exec': True}
    overrides.pop('default_permissions', None)
    identity = checkpoint = original = None
    report = {'title': 'Mobile review retry verification', 'checks': {}}
    async with ClientSession(timeout=ClientTimeout(total=180)) as session:
        phone = await Phone(session, 'http://127.0.0.1:3285/acp', (home / '.config/codex-spark/mobile.token').read_text().strip()).connect()
        before = await inventory(phone)
        (root / 'inventory-before.json').write_text(json.dumps(before, indent=2) + '\n')
        database = sqlite3.connect(f"file:{home / '.local/state/codex-spark/mobile/recovery.sqlite3'}?mode=ro", uri=True)
        database.row_factory = sqlite3.Row
        def record():
            source = database.execute('SELECT reviewed FROM turns WHERE id=? AND thread=?', (checkpoint, identity)).fetchone()
            review = database.execute('SELECT phase,turn FROM submissions WHERE id=? AND thread=?', ('review-' + checkpoint, identity)).fetchone()
            return {'reviewed': bool(source['reviewed']) if source else None, 'review': dict(review) if review else None}
        try:
            async with RPC(session, 'ws://127.0.0.1:4500', (home / '.config/codex-spark/server.token').read_text().strip()) as native:
                created = await native.call('thread/start', {'cwd': str(project), 'ephemeral': False, 'approvalPolicy': 'never', 'sandbox': 'workspace-write', 'config': overrides})
                identity = created['thread']['id']; report['thread'] = identity
                (root / 'owned-chat.json').write_text(json.dumps({'thread': identity, 'directory': str(project)}) + '\n')
                bootstrap = await native.call('turn/start', {'threadId': identity, 'input': [{'type': 'text', 'text': 'Do not use tools. Reply REVIEW-CHECK-READY only.'}]})
                async with asyncio.timeout(120):
                    while not any(p.get('method') == 'turn/completed' and p['params']['turn']['id'] == bootstrap['turn']['id'] for p in native.events):
                        native.events.append(await native.ws.receive_json())
                await phone.call('session/load', {'sessionId': identity})
                await phone.call('_goose/unstable/session/rename', {'sessionId': identity, 'title': report['title']})
                prompt = "This is a controlled recovery check in this temporary folder only. Use the terminal once to execute exactly: printf 'RECOVERY-ONCE\\n' >> recovery-counter.txt; sleep 20\nDo not run any other command or modify any other file. After it finishes, reply ORIGINAL-TASK-FINISHED."
                original = asyncio.create_task(phone.call('session/prompt', {'sessionId': identity, 'prompt': [{'type': 'text', 'text': prompt}], '_meta': {'goose': {'requestId': 'original-' + identity}}}))
                await until(lambda: counter.read_text() == ONCE or original.done())
                assert counter.read_text() == ONCE, 'The controlled terminal append did not execute'
                running = [p['params']['update']['_meta']['goose']['hostRun'] for p in phone.events if p.get('params', {}).get('sessionId') == identity and p.get('params', {}).get('update', {}).get('_meta', {}).get('goose', {}).get('hostRun', {}).get('state') == 'running']
                checkpoint = running[-1]['id']; report['checkpoint'] = checkpoint
                await native.call('turn/interrupt', {'threadId': identity, 'turnId': checkpoint})
                with contextlib.suppress(RuntimeError):
                    await original
                initial = await phone.call('session/load', {'sessionId': identity})
                status = next(run for run in initial['_meta']['hostRuns'] if run['id'] == checkpoint)
                assert status['state'] == 'interrupted' and status['recovery']['canResume'], status
                report['checks']['actualInterruptedTask'] = True
                print(json.dumps({'ready': True, 'thread': identity, 'checkpoint': checkpoint}), flush=True)
                if hold:
                    await until(lambda: (root / 'arm').exists(), hold)
                await native.call('thread/archive', {'threadId': identity})
                report['checks']['ownedThreadTemporarilyUnavailable'] = True
                (root / 'armed.json').write_text(json.dumps({'armed': True}) + '\n')
                if hold:
                    await until(lambda: (root / 'restore').exists(), hold)
                else:
                    try:
                        await phone.call('_goose/unstable/session/resume_run', {'sessionId': identity, 'runId': checkpoint})
                        raise AssertionError('Expected the native pre-acceptance failure')
                    except RuntimeError as error:
                        report['launchError'] = str(error)
                failed = record(); report['afterFailedLaunch'] = failed
                assert failed == {'reviewed': False, 'review': {'phase': 'prepared', 'turn': None}}, failed
                assert counter.read_text() == ONCE
                report['checks']['failureKeptReviewAvailable'] = True
                await phone.call('_goose/unstable/session/unarchive', {'sessionId': identity})
                available = await phone.call('session/load', {'sessionId': identity})
                assert next(run for run in available['_meta']['hostRuns'] if run['id'] == checkpoint)['recovery']['canResume']
                resumed = await native.call('thread/read', {'threadId': identity, 'includeTurns': True})
                assert not any(i.get('clientId') == 'review-' + checkpoint for t in resumed['thread'].get('turns', []) for i in t.get('items', [])), 'A rejected review must have no accepted native turn'
                (root / 'restored.json').write_text(json.dumps({'restored': True}) + '\n')
                if hold:
                    await until(lambda: (root / 'release').exists(), hold)
                else:
                    result = await phone.call('_goose/unstable/session/resume_run', {'sessionId': identity, 'runId': checkpoint})
                    assert result['stopReason'] == 'end_turn', result
                final = await phone.call('session/load', {'sessionId': identity})
                source = next(run for run in final['_meta']['hostRuns'] if run['id'] == checkpoint)
                accepted = record(); report['afterAcceptedReview'] = accepted
                assert accepted['reviewed'] and accepted['review']['phase'] == 'finished', accepted
                assert not source['recovery']['canResume'], source
                assert final['_meta']['hostRuns'][-1]['state'] == 'finished'
                assert counter.read_text() == ONCE, 'Interrupted command was repeated'
                native_history = (await native.call('thread/read', {'threadId': identity, 'includeTurns': True}))['thread']['turns']
                review_turns = [turn for turn in native_history if any(i.get('clientId') == 'review-' + checkpoint for i in turn.get('items', []))]
                assert len(review_turns) == 1, 'Review must have exactly one accepted native turn'
                commands = [i.get('command', '') for turn in review_turns for i in turn.get('items', []) if i['type'] == 'commandExecution']
                assert not any('>>' in command or 'sleep 20' in command for command in commands), commands
                report['reviewCommands'] = commands
                report['checks'].update({'successfulSparkReview': True, 'oneAcceptedReview': True, 'interruptedCommandNotRepeated': True, 'consumedOnlyAfterAcceptance': True})
        finally:
            try:
                if identity:
                    await phone.call('session/cancel', {'sessionId': identity})
                    if original:
                        await asyncio.gather(original, return_exceptions=True)
                    for attempt in range(50):
                        try:
                            await phone.call('session/delete', {'sessionId': identity})
                            break
                        except RuntimeError:
                            if attempt == 49:
                                raise
                            await asyncio.sleep(.2)
                    report['checks']['fixtureDeleted'] = True
                after = await inventory(phone)
                (root / 'inventory-after.json').write_text(json.dumps(after, indent=2) + '\n')
                report['checks']['inventoryUnchanged'] = before == after
                report['remaining'] = {'chats': len(after['chats']), 'projects': len(after['projects'])}
                assert before == after, 'Production inventory changed'
            finally:
                database.close()
                shutil.rmtree(project)
                report['checks']['temporaryFolderRemoved'] = not project.exists()
                raw = config_path.read_text()
                header = '[projects.' + json.dumps(str(project)) + ']'
                match = re.search('^' + re.escape(header) + r'\n.*?(?=^\[|\Z)', raw, re.M | re.S)
                if match:
                    assert tomllib.loads(raw)['projects'][str(project)] == {'trust_level': 'trusted'}
                    staged = config_path.with_name('config.toml.review-cleanup')
                    staged.write_text(raw[:match.start()] + raw[match.end():]); staged.chmod(0o600)
                    os.replace(staged, config_path)
                report['checks']['temporaryTrustEntryRemoved'] = str(project) not in tomllib.loads(config_path.read_text()).get('projects', {})
                report['checks']['profileAndNativeBinaryUnchanged'] = hashes == [hashlib.sha256(path.read_bytes()).hexdigest() for path in protected]
                (root / 'verification.json').write_text(json.dumps(report, indent=2) + '\n')
                await phone.close()
                print(json.dumps(report), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report-dir', type=Path, required=True)
    parser.add_argument('--hold', type=int, default=0, help='Wait for arm, restore and release files during physical phone verification')
    args = parser.parse_args()
    asyncio.run(main(args.report_dir, args.hold))
