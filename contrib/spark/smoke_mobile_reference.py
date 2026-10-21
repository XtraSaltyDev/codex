#!/usr/bin/env python3
"""Verify mobile project browsing and native chat controls, then delete the test chat."""
import argparse
import asyncio
import hashlib
import json
from pathlib import Path
import secrets
import shutil
import tempfile
import time

from aiohttp import ClientSession, web
from goose_mobile import Bridge, app
from health import RPC
from smoke_mobile_activity import inventory
from smoke_thread_titles import Phone


async def main(installed, report_path, hold):
    home = Path.home()
    root = Path(tempfile.mkdtemp(prefix='mobile-reference-'))
    project = home / 'Projects/codex-spark'
    paths = [home / '.codex-spark/config.toml', home / '.codex-spark/models.json']
    before_hashes = [hashlib.sha256(p.read_bytes()).hexdigest() for p in paths]
    report = {'installed': installed}
    identity = None
    runner = bridge = phone = production = None
    async with ClientSession() as session:
        try:
            production = await Phone(session, 'http://127.0.0.1:3285/acp', (home / '.config/codex-spark/mobile.token').read_text().strip()).connect()
            before = await inventory(production)
            if installed:
                phone = production
            else:
                token = root / 'token'; token.write_text(secrets.token_urlsafe(32)); token.chmod(0o600)
                bridge = Bridge(root / 'adapter', token, home / '.config/codex-spark/server.token', push_config=root / 'no-push.json')
                runner = web.AppRunner(app(bridge)); await runner.setup()
                await web.TCPSite(runner, '127.0.0.1', 0).start()
                phone = await Phone(session, f'http://127.0.0.1:{runner.addresses[0][1]}/acp', token.read_text()).connect()
            created = await phone.call('session/new', {'cwd': str(project), 'mcpServers': []})
            identity = created['sessionId']
            await phone.call('_goose/unstable/session/rename', {'sessionId': identity, 'title': 'Mobile UI reference verification'})
            options = (await phone.call('_goose/unstable/session/check_model', {'sessionId': identity}))['_meta']['goose']['modelSelection']
            report['catalog'] = options
            assert [o['id'] for o in options['reasoningOptions']] == ['none', 'low', 'medium', 'xhigh'], options
            assert options['reasoningEffort'] == 'low', options
            listing = await phone.call('_goose/unstable/session/files', {'sessionId': identity})
            assert any(f['name'] == 'contrib' and f['kind'] == 'directory' for f in listing['entries']), listing
            read = await phone.call('_goose/unstable/session/files', {'sessionId': identity, 'action': 'read', 'path': 'contrib/spark/GOOSE_MOBILE.md'})
            assert read['kind'] == 'text' and read['markdown'] and 'Goose' in read['text'], read
            changes = await phone.call('_goose/unstable/session/files', {'sessionId': identity, 'action': 'changes'})
            assert changes['git'], changes
            if changes['files']:
                diff = await phone.call('_goose/unstable/session/files', {'sessionId': identity, 'action': 'changes', 'path': changes['files'][0]['path']})
                assert 'diff' in diff['file'], diff
            report['files'] = {'browse': True, 'markdown': True, 'changes': len(changes['files'])}
            for path in ('../.codex-spark/config.toml', '/etc/passwd'):
                try:
                    await phone.call('_goose/unstable/session/files', {'sessionId': identity, 'action': 'read', 'path': path})
                    raise AssertionError('Project path boundary was not enforced')
                except RuntimeError:
                    pass
            async with RPC(session, 'ws://127.0.0.1:4500', (home / '.config/codex-spark/server.token').read_text().strip()) as native:
                for access in ('readOnly', 'workspace', 'full'):
                    await phone.call('session/set_config_option', {'sessionId': identity, 'configId': 'access', 'value': access})
                    resumed = await native.call('thread/resume', {'threadId': identity, 'excludeTurns': True})
                    assert resumed['sandbox']['type'] == {'readOnly': 'readOnly', 'workspace': 'workspaceWrite', 'full': 'dangerFullAccess'}[access], resumed
                for effort in ('none', 'low', 'medium', 'xhigh', 'low'):
                    await phone.call('session/set_config_option', {'sessionId': identity, 'configId': 'reasoning', 'value': effort})
                    resumed = await native.call('thread/resume', {'threadId': identity, 'excludeTurns': True})
                    assert resumed['reasoningEffort'] == effort, (effort, resumed)
                report['nativeSettings'] = True
            result = await phone.call('session/prompt', {'sessionId': identity, 'prompt': [{'type': 'text', 'text': 'Do not use tools. Reply with exactly MOBILE-REFERENCE-OK.'}]})
            assert result['stopReason'] == 'end_turn', result
            assert any('MOBILE-REFERENCE-OK' in e.get('params', {}).get('update', {}).get('content', {}).get('text', '') for e in phone.events)
            report['liveSpark'] = True
            await phone.call('session/load', {'sessionId': identity})
            options = (await phone.call('_goose/unstable/session/check_model', {'sessionId': identity}))['_meta']['goose']
            assert options['modelSelection']['reasoningEffort'] == 'low' and options['sessionSettings']['access'] == 'full', options
            print(json.dumps({'thread': identity, 'checksPassed': True}), flush=True)
            if hold:
                deadline = time.monotonic() + 900
                while not hold.exists():
                    assert time.monotonic() < deadline, 'Mobile UI did not release its disposable chat'
                    await asyncio.sleep(.5)
        finally:
            if identity and phone:
                await phone.call('session/cancel', {'sessionId': identity})
                for attempt in range(50):
                    try:
                        await phone.call('session/delete', {'sessionId': identity}); break
                    except RuntimeError:
                        if attempt == 49: raise
                        await asyncio.sleep(.2)
                report['fixtureDeleted'] = True
            if production:
                after = await inventory(production)
                assert after == before, (before, after)
                report['inventoryUnchanged'] = {'chats': len(after['chats']), 'projects': len(after['projects'])}
            if phone and phone is not production: await phone.close()
            if production: await production.close()
            if runner: await runner.cleanup()
            if bridge: bridge.journal.close()
            shutil.rmtree(root)
            report['profileUnchanged'] = before_hashes == [hashlib.sha256(p.read_bytes()).hexdigest() for p in paths]
            report_path.write_text(json.dumps(report, indent=2) + '\n')
            print(json.dumps(report), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--installed', action='store_true')
    parser.add_argument('--report', type=Path, required=True)
    parser.add_argument('--hold-until', type=Path)
    args = parser.parse_args()
    asyncio.run(main(args.installed, args.report, args.hold_until))
