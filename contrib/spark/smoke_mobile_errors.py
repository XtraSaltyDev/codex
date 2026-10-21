#!/usr/bin/env python3
"""Verify installed mobile failure transport with a real Spark rejection; delete the chat."""
import argparse
import asyncio
import hashlib
import json
from pathlib import Path
import time
import tomllib

from aiohttp import ClientSession, ClientTimeout, web
from health import RPC
from smoke_mobile_activity import inventory
from smoke_thread_titles import Phone
from thread_titles import isolated_config


async def main(root, hold):
    root.mkdir(parents=True, exist_ok=True)
    home = Path.home()
    config_path = home / '.codex-spark/config.toml'
    config = tomllib.loads(config_path.read_text())
    paths = [config_path, Path(config['model_catalog_json']), home / '.local/lib/codex-spark/codex']
    hashes = [hashlib.sha256(path.read_bytes()).hexdigest() for path in paths]
    report = {'title': 'Mobile error verification', 'checks': {}, 'providerRequests': []}
    identity = runner = phone = None
    armed = False
    async with ClientSession(timeout=ClientTimeout(total=180)) as session:
        async def relay(request):
            nonlocal armed
            payload = await request.json()
            assert not payload.get('tools'), 'Diagnostic chat must not have tools'
            reject = armed
            armed = False
            if reject:
                payload = {**payload, 'input': [{'role': 'user', 'content': 'a\n' * 270000}], 'max_output_tokens': 1}
            else:
                payload = {**payload, 'max_output_tokens': 4096}
            async with session.post(config['model_providers']['spark']['base_url'].rstrip('/') + '/responses', json=payload) as response:
                body = await response.read()
                report['providerRequests'].append({'status': response.status, 'oversizedSyntheticInput': reject})
                if reject:
                    error = json.loads(body)['error']
                    assert response.status == 400 and error['param'] == 'input_tokens', error
                    report['liveSparkError'] = {'httpStatus': response.status, 'message': error['message']}
                return web.Response(body=body, status=response.status, headers={'Content-Type': response.headers.get('Content-Type', 'application/json')})
        application = web.Application()
        application.router.add_post('/v1/responses', relay)
        runner = web.AppRunner(application)
        await runner.setup()
        site = web.TCPSite(runner, '127.0.0.1', 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        phone = await Phone(session, 'http://127.0.0.1:3285/acp', (home / '.config/codex-spark/mobile.token').read_text().strip()).connect()
        before = await inventory(phone)
        (root / 'inventory-before.json').write_text(json.dumps(before, indent=2) + '\n')
        try:
            async with RPC(session, 'ws://127.0.0.1:4500', (home / '.config/codex-spark/server.token').read_text().strip()) as native:
                created = await native.call('thread/start', {'cwd': str(home / 'Projects/codex-spark'), 'ephemeral': False,
                    'approvalPolicy': 'never', 'sandbox': 'read-only', 'config': {**isolated_config(config),
                    'model_providers.spark.base_url': f'http://127.0.0.1:{port}/v1', 'model_post_turn_compact_threshold_percent': 0}})
                identity = created['thread']['id']
                report['thread'] = identity
                (root / 'owned-chat.json').write_text(json.dumps(report) + '\n')
                bootstrap = await native.call('turn/start', {'threadId': identity,
                    'input': [{'type': 'text', 'text': 'Do not use tools. Reply ERROR-CHECK-READY only.'}]})
                while True:
                    terminal = next((packet['params']['turn'] for packet in native.events
                        if packet.get('method') == 'turn/completed' and packet['params']['turn']['id'] == bootstrap['turn']['id']), None)
                    if terminal:
                        assert terminal['status'] == 'completed', terminal
                        break
                    native.events.append(await asyncio.wait_for(native.ws.receive_json(), 180))
                await phone.call('session/load', {'sessionId': identity, 'cwd': str(home / 'Projects/codex-spark'), 'mcpServers': []})
                await phone.call('_goose/unstable/session/rename', {'sessionId': identity, 'title': report['title']})
                assert len(report['providerRequests']) >= 1, 'Per-thread relay was not used'
                report['checks']['liveSparkBootstrap'] = True
                armed = True
                print(json.dumps({'ready': True, 'thread': identity, 'root': str(root)}), flush=True)
                if hold:
                    deadline = time.monotonic() + hold
                    while not (root / 'release').exists():
                        assert time.monotonic() < deadline, 'Phone verification did not release the owned chat'
                        await asyncio.sleep(.5)
                else:
                    try:
                        await phone.call('session/prompt', {'sessionId': identity, 'prompt': [{'type': 'text', 'text': 'Do not use tools. Check the controlled failure.'}]})
                        raise AssertionError('Expected the context-limit failure')
                    except RuntimeError as error:
                        assert 'context_limit' in str(error) and 'check the host log' not in str(error), str(error)
                    report['checks']['liveRpcCause'] = True
                    await phone.call('session/prompt', {'sessionId': identity, 'prompt': [{'type': 'text', 'text': 'Do not use tools. Reply SPARK-ERROR-RECOVERED only.'}]})
                replay = await phone.call('session/load', {'sessionId': identity, 'mcpServers': []})
                failures = [run for run in replay['_meta']['hostRuns'] if run['state'] == 'failed']
                assert len(failures) == 1, failures
                failure = failures[0]['error']
                assert failure['category'] == 'context_limit' and len(failure['message'].encode()) <= 2048, failure
                assert replay['_meta']['hostRuns'][-1]['state'] == 'finished', replay['_meta']['hostRuns']
                assert any(packet.get('params', {}).get('update', {}).get('_meta', {}).get('goose', {}).get('hostRun', {}).get('error') == failure for packet in phone.events)
                report['failure'] = failure
                report['checks'].update({'replayedCause': True, 'liveHostRunCause': True, 'successfulFollowup': True})
        finally:
            try:
                if identity:
                    await phone.call('session/cancel', {'sessionId': identity})
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
                report['checks']['inventoryUnchanged'] = after == before
                report['remaining'] = {'chats': len(after['chats']), 'projects': len(after['projects'])}
                assert after == before, 'Production inventory changed'
            finally:
                report['checks']['profileAndNativeBinaryUnchanged'] = hashes == [hashlib.sha256(path.read_bytes()).hexdigest() for path in paths]
                (root / 'verification.json').write_text(json.dumps(report, indent=2) + '\n')
                await phone.close()
                await runner.cleanup()
                print(json.dumps(report), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report-dir', type=Path, required=True)
    parser.add_argument('--hold', type=int, default=0, help='Wait up to this many seconds for a release file after phone testing')
    args = parser.parse_args()
    asyncio.run(main(args.report_dir, args.hold))
