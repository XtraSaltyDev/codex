#!/usr/bin/env python3
"""Stall an authenticated TCP subscriber while real Spark and healthy mobile work continue."""
import argparse
import asyncio
import hashlib
import json
from pathlib import Path
import socket
import time
import tomllib

from aiohttp import ClientSession, ClientTimeout, TCPConnector
from health import RPC
from smoke_mobile_activity import inventory
from smoke_thread_titles import Phone
from thread_titles import isolated_config


def tcp_state(port):
    for line in Path('/proc/net/tcp').read_text().splitlines()[1:]:
        fields = line.split()
        if int(fields[1].split(':')[1], 16) == 3285 and int(fields[2].split(':')[1], 16) == port:
            return {'state': fields[3], 'sendQueueBytes': int(fields[4].split(':')[0], 16)}
    return None


def small_window(address):
    family, kind, protocol, _, _ = address
    sock = socket.socket(family, kind, protocol)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
    if hasattr(socket, 'TCP_WINDOW_CLAMP'):
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_WINDOW_CLAMP, 4096)
    return sock


async def main(root, hold):
    root.mkdir(parents=True, exist_ok=True)
    home = Path.home()
    config_path = home / '.codex-spark/config.toml'
    config = tomllib.loads(config_path.read_text())
    protected = [config_path, Path(config['model_catalog_json']), home / '.local/lib/codex-spark/codex']
    hashes = [hashlib.sha256(path.read_bytes()).hexdigest() for path in protected]
    token = (home / '.config/codex-spark/mobile.token').read_text().strip()
    owned = []
    report = {'title': 'Mobile connection verification', 'checks': {}, 'cycles': [], 'nativeRpcSeconds': [], 'healthyChunksWhileBlocked': 0}
    async with ClientSession(timeout=ClientTimeout(total=180)) as session:
        phone = await Phone(session, 'http://127.0.0.1:3285/acp', token).connect()
        before = await inventory(phone)
        (root / 'inventory-before.json').write_text(json.dumps(before, indent=2) + '\n')
        slow_session = slow = None
        try:
            async with RPC(session, 'ws://127.0.0.1:4500', (home / '.config/codex-spark/server.token').read_text().strip()) as native:
                async def create(text, expected):
                    created = await native.call('thread/start', {'cwd': str(home / 'Projects/codex-spark'), 'ephemeral': False,
                        'approvalPolicy': 'never', 'sandbox': 'read-only', 'config': isolated_config(config)})
                    identity = created['thread']['id']; owned.append(identity)
                    (root / 'owned-chats.json').write_text(json.dumps(owned) + '\n')
                    started = await native.call('turn/start', {'threadId': identity, 'input': [{'type': 'text', 'text': text}]})
                    async with asyncio.timeout(180):
                        while True:
                            terminal = next((p['params']['turn'] for p in native.events if p.get('method') == 'turn/completed' and p['params']['turn']['id'] == started['turn']['id']), None)
                            if terminal:
                                assert terminal['status'] == expected, {'status': terminal['status'], 'error': terminal.get('error')}
                                break
                            native.events.append(await native.ws.receive_json())
                    native.events.clear()
                    return identity
                healthy = await create('Do not use tools. Reply CONNECTION-CHECK-READY only.', 'completed')
                report['thread'] = healthy
                await phone.call('session/load', {'sessionId': healthy})
                await phone.call('_goose/unstable/session/rename', {'sessionId': healthy, 'title': report['title']})
                large = await create('Synthetic transport fixture. Do not use tools.\n' + '🙂' * 700_000, 'failed')
                await native.call('thread/name/set', {'threadId': large, 'name': 'Disposable stalled-reader fixture'})
                report['largeHistoryUTF8Bytes'] = 700_000 * 4
                report['largeHistoryJSONBytes'] = 700_000 * 12
                print(json.dumps({'ready': True, 'thread': healthy, 'root': str(root)}), flush=True)
                deadline = time.monotonic() + (hold or 90)
                position = len(phone.events)
                prompt = None
                while time.monotonic() < deadline and not (root / 'release').exists():
                    slow_session = ClientSession(connector=TCPConnector(socket_factory=small_window), timeout=ClientTimeout(total=None))
                    slow = await slow_session.ws_connect('http://127.0.0.1:3285/acp', headers={'X-Secret-Key': token}, max_msg_size=16 * 1024 * 1024)
                    await slow.send_json({'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {'protocolVersion': 1}})
                    async with asyncio.timeout(10):
                        while (await slow.receive_json()).get('id') != 1:
                            pass
                    transport = slow._writer.transport
                    port = transport.get_extra_info('sockname')[1]
                    transport.pause_reading()
                    await slow.send_json({'jsonrpc': '2.0', 'id': 2, 'method': 'session/load', 'params': {'sessionId': large}})
                    cycle = {'started': time.monotonic(), 'maxSendQueueBytes': 0, 'serverEvicted': False}
                    report['cycles'].append(cycle)
                    async with asyncio.timeout(5):
                        while True:
                            state = tcp_state(port)
                            if state and state['sendQueueBytes'] > 65536:
                                cycle['maxSendQueueBytes'] = state['sendQueueBytes']
                                break
                            await asyncio.sleep(.05)
                    if not hold and prompt is None:
                        prompt = asyncio.create_task(phone.call('session/prompt', {'sessionId': healthy,
                            'prompt': [{'type': 'text', 'text': 'Do not use tools. Reply SPARK-SLOW-PEER-OK only.'}]}))
                    while time.monotonic() - cycle['started'] < 15 and not (root / 'release').exists():
                        state = tcp_state(port)
                        if not state or state['state'] != '01':
                            cycle['serverEvicted'] = True
                            cycle['secondsUntilEviction'] = round(time.monotonic() - cycle['started'], 3)
                            break
                        cycle['maxSendQueueBytes'] = max(cycle['maxSendQueueBytes'], state['sendQueueBytes'])
                        started = time.monotonic()
                        await asyncio.wait_for(phone.call('session/list', {'limit': 1}), 2)
                        report['nativeRpcSeconds'].append(round(time.monotonic() - started, 4))
                        for packet in phone.events[position:]:
                            update = packet.get('params', {}).get('update', {})
                            if packet.get('params', {}).get('sessionId') == healthy and update.get('sessionUpdate') == 'agent_message_chunk' and state['sendQueueBytes'] > 65536:
                                report['healthyChunksWhileBlocked'] += 1
                        position = len(phone.events)
                        await asyncio.sleep(.1)
                    transport.abort()
                    await slow_session.close(); slow_session = slow = None
                    if not (root / 'release').exists():
                        assert cycle['serverEvicted'], cycle
                    (root / 'progress.json').write_text(json.dumps(report, indent=2) + '\n')
                    if not hold:
                        await prompt
                        break
                if hold:
                    assert (root / 'release').exists(), 'Phone verification did not release the fixture'
                replay = await phone.call('session/load', {'sessionId': healthy})
                text = '\n'.join(p.get('params', {}).get('update', {}).get('content', {}).get('text', '') for p in phone.events)
                assert 'SPARK-SLOW-PEER-OK' in text, 'Healthy Spark answer missing'
                if hold:
                    assert 'SPARK-SLOW-PEER-REJOINED' in text, 'Phone follow-up missing'
                assert all(run['state'] == 'finished' for run in replay['_meta']['hostRuns'])
                assert report['healthyChunksWhileBlocked'] > 0, report
                assert any(cycle['serverEvicted'] for cycle in report['cycles']), report
                report['checks'].update({'realTcpBackpressure': True, 'nativeRpcContinued': True,
                    'healthySparkStreamWhileBlocked': True, 'stalledPeerEvicted': True, 'savedReplay': True})
        finally:
            try:
                if slow:
                    slow._writer.transport.abort()
                if slow_session:
                    await slow_session.close()
                for identity in reversed(owned):
                    await phone.call('session/cancel', {'sessionId': identity})
                    for attempt in range(50):
                        try:
                            await phone.call('session/delete', {'sessionId': identity})
                            break
                        except RuntimeError:
                            if attempt == 49:
                                raise
                            await asyncio.sleep(.2)
                after = await inventory(phone)
                (root / 'inventory-after.json').write_text(json.dumps(after, indent=2) + '\n')
                report['checks']['fixturesDeleted'] = True
                report['checks']['inventoryUnchanged'] = before == after
                report['remaining'] = {'chats': len(after['chats']), 'projects': len(after['projects'])}
                assert before == after, 'Production inventory changed'
            finally:
                report['checks']['profileAndNativeBinaryUnchanged'] = hashes == [hashlib.sha256(path.read_bytes()).hexdigest() for path in protected]
                if report['nativeRpcSeconds']:
                    report['maxNativeRpcSeconds'] = max(report['nativeRpcSeconds'])
                (root / 'verification.json').write_text(json.dumps(report, indent=2) + '\n')
                await phone.close()
                print(json.dumps(report), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report-dir', required=True, type=Path)
    parser.add_argument('--hold', type=int, default=0, help='Cycle stalled peers until a release file, for this many seconds')
    args = parser.parse_args()
    asyncio.run(main(args.report_dir, args.hold))
