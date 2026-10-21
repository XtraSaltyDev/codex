#!/usr/bin/env python3
"""Check live Spark plans and compact mobile tools; always delete the fixture chat."""

import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import signal
import socket
import tempfile
import time
import tomllib

from aiohttp import ClientSession, web
from goose_mobile import Bridge, app
from smoke_thread_titles import Phone


PROMPT = (
    "Use update_plan to create exactly two steps: 'Check mobile activity' in_progress and "
    "'Confirm the result' pending. Then use one terminal call to run "
    "printf 'MOBILE-PLAN-TOOL-OK\\n'. Do not change files, use the network, or run other commands. "
    "After seeing the output, use update_plan again to mark both steps completed. "
    "Finally reply MOBILE-PLAN-OK."
)


def updates(phone):
    return [p['params']['update'] for p in phone.events if p.get('method') == 'session/update']


async def inventory(phone):
    chats = []
    params = {}
    while True:
        result = await phone.call('session/list', params)
        chats.extend(s['sessionId'] for s in result['sessions'])
        cursor = result.get('nextCursor')
        if not cursor or json.loads(cursor)['archived']:
            break
        params = {'cursor': cursor}
    projects = await phone.call('_goose/unstable/workspaces', {})
    return {'chats': sorted(chats), 'projects': sorted(p['path'] for p in projects['projects'])}


async def main(installed, report_path, hold):
    home = Path.home()
    config_path = home / '.codex-spark/config.toml'
    config = tomllib.loads(config_path.read_text())
    paths = [config_path, Path(config['model_catalog_json'])]
    hashes = [hashlib.sha256(p.read_bytes()).hexdigest() for p in paths]
    root = Path(tempfile.mkdtemp(prefix='mobile-activity-'))
    bridge = runner = server = phone = None
    server_log = None
    identity = None
    report = {'installedAdapter': installed, 'checks': {}}
    before = None
    try:
        if installed:
            url = 'http://127.0.0.1:3285/acp'
            token = (home / '.config/codex-spark/mobile.token').read_text().strip()
            project = home / 'Projects/codex-spark'
        else:
            runtime = root / 'runtime'; runtime.mkdir()
            shutil.copyfile(config_path, runtime / 'config.toml')
            project = root / 'project'; project.mkdir()
            token = secrets.token_urlsafe(32)
            token_path = root / 'token'; token_path.write_text(token); token_path.chmod(0o600)
            with socket.socket() as sock:
                sock.bind(('127.0.0.1', 0)); port = sock.getsockname()[1]
            upstream = f'ws://127.0.0.1:{port}'
            server_log = (root / 'server.log').open('wb')
            server = await asyncio.create_subprocess_exec(str(home / '.local/lib/codex-spark/codex'),
                '-c', 'features.hooks=false', 'app-server', '--strict-config', '--listen', upstream,
                '--ws-auth', 'capability-token', '--ws-token-file', str(token_path),
                env={**os.environ, 'CODEX_HOME': str(runtime)}, stdout=asyncio.subprocess.DEVNULL,
                stderr=server_log, start_new_session=True)
            bridge = Bridge(root / 'adapter', token_path, token_path, upstream_url=upstream,
                            push_config=root / 'no-push.json')
            runner = web.AppRunner(app(bridge)); await runner.setup()
            await web.TCPSite(runner, '127.0.0.1', 0).start()
            await asyncio.wait_for(bridge.rpc.ready.wait(), 30)
            bridge.metadata['projects'] = [str(project)]
            bridge.save()
            url = f'http://127.0.0.1:{runner.addresses[0][1]}/acp'
        async with ClientSession() as session:
            phone = await Phone(session, url, token).connect()
            before = await inventory(phone)
            try:
                identity = (await phone.call('session/new', {'cwd': str(project), 'mcpServers': []}))['sessionId']
                await phone.call('_goose/unstable/session/rename', {'sessionId': identity, 'title': 'Mobile activity verification'})
                result = await phone.call('session/prompt', {'sessionId': identity,
                    'prompt': [{'type': 'text', 'text': PROMPT}], '_meta': {'goose': {'requestId': 'mobile-plan-acceptance'}}})
                assert result['stopReason'] == 'end_turn', result
                live = updates(phone)
                plans = [u for u in live if u['sessionUpdate'] == 'plan']
                assert len(plans) >= 2 and any(e['status'] == 'in_progress' for e in plans[0]['entries']), plans
                assert len(plans[-1]['entries']) == 2 and all(e['status'] == 'completed' for e in plans[-1]['entries']), plans
                tools = [u for u in live if u['sessionUpdate'] in ('tool_call', 'tool_call_update')]
                assert tools and all(u['title'] in ('Used a tool', 'Edited a file', 'Searched the web') for u in tools), tools
                command = next(u for u in tools if 'MOBILE-PLAN-TOOL-OK' in u.get('_meta', {}).get('goose', {}).get('commandResult', {}).get('output', ''))
                assert command['status'] == 'completed' and command['_meta']['goose']['commandResult']['exitCode'] == 0, command
                assert 'printf' in command['_meta']['goose']['toolDetail'], command
                assert 'MOBILE-PLAN-OK' in ''.join(u.get('content', {}).get('text', '') for u in live if u['sessionUpdate'] == 'agent_message_chunk')
                report['checks']['live'] = {'plans': plans, 'command': command, 'passed': True}
                await phone.close(); phone = await Phone(session, url, token).connect()
                await phone.call('session/load', {'sessionId': identity})
                replay = updates(phone)
                assert [u for u in replay if u['sessionUpdate'] == 'plan'] == [plans[-1]], replay
                assert any(u == command for u in replay), replay
                report['checks']['reconnect'] = True
                print(json.dumps({'thread': identity, 'live': True, 'replay': True}), flush=True)
                if hold:
                    deadline = time.monotonic() + 600
                    while not hold.exists():
                        assert time.monotonic() < deadline, 'UI verification did not release fixture'
                        await asyncio.sleep(.5)
                    await phone.close(); phone = await Phone(session, url, token).connect()
                    await phone.call('session/load', {'sessionId': identity})
                    replayed = [u for u in updates(phone) if u['sessionUpdate'] == 'plan']
                    assert plans[-1] in replayed
                    assert all(e['status'] == 'completed' for e in replayed[-1]['entries'])
                    report['checks']['afterUI'] = True
            finally:
                if identity:
                    if phone is None or phone.ws.closed:
                        phone = await Phone(session, url, token).connect()
                    await phone.call('session/cancel', {'sessionId': identity})
                    # Cancellation completion must settle before permanent fixture deletion.
                    for attempt in range(50):
                        try:
                            await phone.call('session/delete', {'sessionId': identity})
                            break
                        except RuntimeError as error:
                            if 'running task' not in str(error) or attempt == 49:
                                raise
                            await asyncio.sleep(.2)
                    report['fixtureDeleted'] = True
                    after = await inventory(phone)
                    assert after == before, (before, after)
                    report['inventoryUnchanged'] = {'chats': len(after['chats']), 'projects': len(after['projects'])}
                await phone.close(); phone = None
    finally:
        if runner:
            await runner.cleanup()
        if bridge:
            bridge.journal.close()
        if server and server.returncode is None:
            os.killpg(server.pid, signal.SIGINT)
            try:
                await asyncio.wait_for(server.wait(), 10)
            except TimeoutError:
                os.killpg(server.pid, signal.SIGKILL); await server.wait()
        if server_log:
            server_log.close()
        report['productionProfileUnchanged'] = hashes == [hashlib.sha256(p.read_bytes()).hexdigest() for p in paths]
        report_path.write_text(json.dumps(report, indent=2) + '\n')
        shutil.rmtree(root)
    assert report['productionProfileUnchanged']
    print(json.dumps(report), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--installed', action='store_true')
    parser.add_argument('--report', type=Path, required=True)
    parser.add_argument('--hold-until', type=Path)
    args = parser.parse_args()
    asyncio.run(main(args.installed, args.report, args.hold_until))
