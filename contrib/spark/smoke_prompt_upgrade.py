#!/usr/bin/env python3
"""Verify stock prompt upgrades and custom/history preservation using disposable chats."""
import argparse
import asyncio
import contextlib
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import tomllib

from aiohttp import ClientSession, ClientTimeout, web
from health import RPC
from smoke_mobile_activity import inventory
from smoke_thread_titles import Phone


def digest(data):
    return hashlib.sha256(data).hexdigest()


async def wait_file(path, timeout=900):
    async with asyncio.timeout(timeout):
        while not path.exists():
            await asyncio.sleep(.2)


async def turn(native, identity, text):
    result = await native.call('turn/start', {'threadId': identity, 'input': [{'type': 'text', 'text': text}]})
    async with asyncio.timeout(180):
        while not any(e.get('method') == 'turn/completed' and e['params']['turn']['id'] == result['turn']['id'] for e in native.events):
            native.events.append(await native.ws.receive_json())
    completed = next(e['params']['turn'] for e in native.events if e.get('method') == 'turn/completed' and e['params']['turn']['id'] == result['turn']['id'])
    assert completed['status'] == 'completed', completed


async def main(root):
    root.mkdir(parents=True, exist_ok=True)
    home = Path.home(); repo = Path(__file__).resolve().parents[2]
    config_path = home / '.codex-spark/config.toml'; config_before = config_path.read_bytes()
    config = tomllib.loads(config_before.decode())
    catalog_path = Path(config['model_catalog_json']); catalog_hash = digest(catalog_path.read_bytes())
    stock = json.loads(catalog_path.read_text())['models'][0]['model_messages']['instructions_template']
    legacy = (repo / 'codex-rs/models-manager/prompt.md').read_text()
    custom = legacy + '\nAlways include CUSTOM-BASE-PRESERVED in final replies.\n'
    workspace = Path(tempfile.mkdtemp(prefix='codex-prompt-upgrade-'))
    owned = []; captures = []; histories = {}
    report = {'checks': {}, 'owned': owned, 'requests': captures}
    original_files = {str(p): {'size': p.stat().st_size, 'sha256': digest(p.read_bytes())} for p in (home / '.codex-spark/sessions').rglob('*.jsonl')}
    token = (home / '.config/codex-spark/server.token').read_text().strip()
    async with ClientSession(timeout=ClientTimeout(total=240)) as session:
        async def relay(request):
            payload = await request.json()
            developer_texts = [c.get('text', '') for item in payload.get('input', []) if item.get('role') == 'developer' for c in item.get('content', [])]
            instructions = developer_texts[0] if developer_texts else payload.get('instructions', '')
            texts = json.dumps(payload.get('input', []), ensure_ascii=False)
            captures.append({'instructionsSha256': digest(instructions.encode()),
                             'stock': instructions == stock, 'custom': instructions == custom,
                             'developerPreserved': 'DEV-PRESERVED' in texts,
                             'agentsPreserved': 'AGENTS-PRESERVED' in texts,
                             'legacyMemoryPresent': 'LEGACY-MEMORY-731' in texts,
                             'customMemoryPresent': 'CUSTOM-MEMORY-842' in texts})
            async with session.post(config['model_providers']['spark']['base_url'].rstrip('/') + '/responses', json=payload) as upstream:
                captures[-1]['httpStatus'] = upstream.status
                response = web.StreamResponse(status=upstream.status, headers={'Content-Type': upstream.headers.get('Content-Type', 'text/event-stream')})
                await response.prepare(request)
                async for chunk in upstream.content.iter_any():
                    await response.write(chunk)
                await response.write_eof()
                return response
        application = web.Application(client_max_size=16 * 1024 * 1024)
        application.router.add_post('/v1/responses', relay)
        runner = web.AppRunner(application); await runner.setup()
        site = web.TCPSite(runner, '127.0.0.1', 0); await site.start()
        port = site._server.sockets[0].getsockname()[1]
        phone = await Phone(session, 'http://127.0.0.1:3285/acp', (home / '.config/codex-spark/mobile.token').read_text().strip()).connect()
        before = await inventory(phone)
        (root / 'inventory-before.json').write_text(json.dumps(before, indent=2) + '\n')
        try:
            async with RPC(session, 'ws://127.0.0.1:4500', token) as native:
                for kind, memory in [('legacy', 'LEGACY-MEMORY-731'), ('custom', 'CUSTOM-MEMORY-842')]:
                    folder = workspace / kind; folder.mkdir()
                    (folder / 'AGENTS.md').write_text('When a request contains PROMPT-UPGRADE-CHECK, include AGENTS-PRESERVED in your final response.\n')
                    (folder / 'calculator.py').write_text('def add(a, b):\n    return a - b\n')
                    (folder / 'test_calculator.py').write_text('import unittest\nfrom calculator import add\nclass AdditionTests(unittest.TestCase):\n    def test_add(self):\n        self.assertEqual(add(2, 3), 5)\n        self.assertEqual(add(-2, 3), 1)\n')
                    subprocess.run(['git', '-C', str(folder), 'init', '-q'], check=True)
                    args = {'cwd': str(folder), 'ephemeral': False, 'approvalPolicy': 'never', 'sandbox': 'workspace-write',
                            'developerInstructions': config.get('developer_instructions', '') + '\nWhen a request contains PROMPT-UPGRADE-CHECK, include DEV-PRESERVED in your final response.'}
                    if kind == 'custom': args['baseInstructions'] = custom
                    created = await native.call('thread/start', args); identity = created['thread']['id']
                    record = {'kind': kind, 'thread': identity, 'directory': str(folder), 'memory': memory}; owned.append(record)
                    (root / 'owned.json').write_text(json.dumps(owned, indent=2) + '\n')
                    await native.call('thread/name/set', {'threadId': identity, 'name': 'Prompt upgrade ' + kind + ' verification'})
                    await turn(native, identity, 'Remember this exact token for our next messages: ' + memory + '. Do not use tools. Reply SAVED-HISTORY-READY only.')
                    histories[identity] = (await native.call('thread/read', {'threadId': identity, 'includeTurns': True}))['thread']['turns']
                    await native.call('thread/archive', {'threadId': identity})
                    assert identity not in (await native.call('thread/loaded/list', {}))['data']
                    rollout = next((home / '.codex-spark/archived_sessions').rglob('*' + identity + '.jsonl'))
                    record['rollout'] = str(rollout)
                    if kind == 'legacy':
                        # Only the owned fixture's metadata simulates a pre-upgrade release.
                        lines = rollout.read_text().splitlines(keepends=True)
                        first = json.loads(lines[0]); assert first['type'] == 'session_meta'
                        first['payload']['base_instructions'] = {'text': legacy, 'provenance': {'type': 'model', 'model': config['model']}}
                        lines[0] = json.dumps(first) + '\n'
                        staged = rollout.with_suffix('.upgrade-seed'); staged.write_text(''.join(lines)); staged.chmod(0o600); os.replace(staged, rollout)
                    raw = rollout.read_bytes(); record.update({'rollout': str(rollout), 'prefixBytes': len(raw), 'prefixSha256': digest(raw)})
                    (root / 'owned.json').write_text(json.dumps(owned, indent=2) + '\n')
            (root / 'seeded.json').write_text(json.dumps({'seeded': True, 'owned': owned}) + '\n')
            print(json.dumps({'seeded': True, 'threads': [r['thread'] for r in owned]}), flush=True)
            await wait_file(root / 'deployed')
            await phone.close()
            phone = await Phone(session, 'http://127.0.0.1:3285/acp', (home / '.config/codex-spark/mobile.token').read_text().strip()).connect()
            async with RPC(session, 'ws://127.0.0.1:4500', token) as native:
                for record in owned:
                    await native.call('thread/unarchive', {'threadId': record['thread']})
                    record['rollout'] = str(next((home / '.codex-spark/sessions').rglob('*' + record['thread'] + '.jsonl')))
                    (root / 'owned.json').write_text(json.dumps(owned, indent=2) + '\n')
                    await native.call('thread/resume', {'threadId': record['thread'], 'excludeTurns': True,
                        'config': {'model_providers.spark.base_url': f'http://127.0.0.1:{port}/v1'}})
                    result = await phone.call('session/load', {'sessionId': record['thread']})
                    assert result['_meta']['hostRuns'], 'Saved mobile history must be present'
                    start = len(captures)
                    await phone.call('session/prompt', {'sessionId': record['thread'], 'prompt': [{'type': 'text', 'text': 'PROMPT-UPGRADE-CHECK. Do not use tools. Repeat the exact token I asked you to remember, together with your required markers.'}]})
                    records = captures[start:]; assert records and all(r['httpStatus'] == 200 for r in records)
                    assert all(r[record['kind'] if record['kind'] == 'custom' else 'stock'] for r in records), records
                    assert all(r['developerPreserved'] and r['agentsPreserved'] for r in records), records
                report['checks'].update({'liveStockRequestUpgraded': True, 'liveCustomRequestUnchanged': True, 'developerAndAgentsInstructionsPreserved': True})
                (root / 'ready.json').write_text(json.dumps({'ready': True, 'owned': owned}) + '\n')
                print(json.dumps({'ready': True, 'owned': owned}), flush=True)
                await wait_file(root / 'release')
                for record in owned:
                    history = (await native.call('thread/read', {'threadId': record['thread'], 'includeTurns': True}))['thread']['turns']
                    assert history[:len(histories[record['thread']])] == histories[record['thread']], 'Original native history changed'
                    raw = Path(record['rollout']).read_bytes()
                    assert digest(raw[:record['prefixBytes']]) == record['prefixSha256'], 'Saved rollout prefix was rewritten'
                    assert record['memory'] in json.dumps(history[-1]), 'Phone follow-up lost saved history'
                fixed = subprocess.run(['python3', '-m', 'unittest', '-v'], cwd=workspace / 'legacy', capture_output=True, text=True)
                (root / 'fixture-tests.log').write_text(fixed.stdout + fixed.stderr)
                assert fixed.returncode == 0, fixed.stderr
                assert (workspace / 'custom/calculator.py').read_text() == 'def add(a, b):\n    return a - b\n'
                report['checks'].update({'savedHistoryAndRolloutPrefixesUnchanged': True, 'legacyPhonePatchTestsPassed': True, 'customWorkspaceUnchanged': True})
        finally:
            for record in owned:
                with contextlib.suppress(Exception): await phone.call('session/cancel', {'sessionId': record['thread']})
                for attempt in range(50):
                    try:
                        await phone.call('session/delete', {'sessionId': record['thread']}); break
                    except Exception:
                        if attempt == 49: raise
                        await asyncio.sleep(.2)
            after = await inventory(phone); await phone.close(); await runner.cleanup()
            report['checks']['inventoryUnchanged'] = before == after
            report['remaining'] = {'chats': len(after['chats']), 'projects': len(after['projects'])}
            shutil.rmtree(workspace)
            raw = config_path.read_text()
            for record in owned:
                header = '[projects.' + json.dumps(record['directory']) + ']'
                match = re.search('^' + re.escape(header) + r'\n.*?(?=^\[|\Z)', raw, re.M | re.S)
                if match:
                    assert tomllib.loads(raw)['projects'][record['directory']] == {'trust_level': 'trusted'}
                    raw = raw[:match.start()] + raw[match.end():]
            assert raw.encode() == config_before, 'Unexpected production profile edit; do not overwrite it'
            staged = config_path.with_suffix('.prompt-cleanup'); staged.write_text(raw); staged.chmod(0o600); os.replace(staged, config_path)
            report['checks'].update({'ownedChatsAndFoldersRemoved': all(not Path(r['directory']).exists() and not any((home / '.codex-spark/sessions').rglob('*' + r['thread'] + '.jsonl')) and not any((home / '.codex-spark/archived_sessions').rglob('*' + r['thread'] + '.jsonl')) for r in owned),
                                    'profileAndCatalogUnchanged': config_path.read_bytes() == config_before and digest(catalog_path.read_bytes()) == catalog_hash,
                                    'originalChatHistoryPrefixesUnchanged': all(Path(p).exists() and digest(Path(p).read_bytes()[:r['size']]) == r['sha256'] for p, r in original_files.items())})
            (root / 'verification.json').write_text(json.dumps(report, indent=2) + '\n')
            print(json.dumps(report), flush=True)
        assert all(report['checks'].values()), report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report-dir', type=Path, required=True)
    asyncio.run(main(parser.parse_args().report_dir))
