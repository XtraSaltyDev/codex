#!/usr/bin/env python3
"""Exercise real Codex/Spark work through transport loss and process restarts."""
import asyncio
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import uuid

from goose_mobile import Bridge, app
from aiohttp import ClientSession, web, WSMsgType


class Phone:
    def __init__(self, session, token):
        self.session = session; self.token = token; self.number = 0; self.events = []; self.pending = {}

    async def connect(self):
        self.ws = await self.session.ws_connect('http://127.0.0.1:14912/acp', headers={'X-Secret-Key': self.token})
        self.reader = asyncio.create_task(self.read())
        await self.call('initialize', {'protocolVersion': 1})
        return self

    async def read(self):
        try:
            async for frame in self.ws:
                if frame.type != WSMsgType.TEXT: continue
                packet = json.loads(frame.data); self.events.append(packet)
                if 'method' not in packet and packet.get('id') in self.pending:
                    future = self.pending.pop(packet['id'])
                    if 'error' in packet: future.set_exception(RuntimeError(str(packet['error'])))
                    else: future.set_result(packet.get('result', {}))
        finally:
            for future in self.pending.values():
                if not future.done(): future.set_exception(ConnectionError('Transport disconnected'))
            self.pending.clear()

    def start(self, method, params):
        self.number += 1; number = self.number
        future = asyncio.get_running_loop().create_future(); self.pending[number] = future
        async def send():
            await self.ws.send_json({'jsonrpc': '2.0', 'id': number, 'method': method, 'params': params})
            return await future
        return asyncio.create_task(send())

    async def call(self, method, params):
        return await asyncio.wait_for(self.start(method, params), 180)

    async def close(self):
        await self.ws.close(); await self.reader


async def until(check, timeout=90):
    async with asyncio.timeout(timeout):
        while not await check(): await asyncio.sleep(.2)


async def main():
    home = Path.home(); source = Path(__file__).resolve().parent
    root = Path(tempfile.mkdtemp(prefix='restart-', dir=home / '.local/state/codex-spark/recovery-lab'))
    runtime = root / 'codex-home'; runtime.mkdir()
    shutil.copy2(home / '.codex-spark/config.toml', runtime / 'config.toml')
    token = root / 'token'; token.write_text(str(uuid.uuid4())); token.chmod(0o600)
    fixture = root / 'project'; fixture.mkdir()
    unit = 'codex-recovery-lab-' + root.name
    command = ['systemd-run', '--user', '--unit=' + unit, '--property=KillMode=control-group', '--property=RuntimeMaxSec=900', '--setenv=CODEX_HOME=' + str(runtime),
               str(home / '.local/lib/codex-spark/codex'), 'app-server', '--strict-config', '--listen', 'ws://127.0.0.1:14911', '--ws-auth', 'capability-token', '--ws-token-file', str(token)]
    subprocess.run(command, check=True, capture_output=True)
    runner = None; bridge = None
    async def start_bridge():
        nonlocal runner, bridge
        bridge = Bridge(root / 'adapter', token, token, 'ws://127.0.0.1:14911')
        runner = web.AppRunner(app(bridge)); await runner.setup()
        await web.TCPSite(runner, '127.0.0.1', 14912).start()
        await asyncio.wait_for(bridge.rpc.ready.wait(), 45)
    async def stop_bridge():
        await runner.cleanup(); bridge.journal.close()
    async def marked(name):
        return (fixture / name).exists()
    async def finished(phone, identity):
        result = await phone.call('session/load', {'sessionId': identity})
        return result['_meta']['hostRuns'][-1]['state'] == 'finished'
    def prompt(identity, filename, seconds):
        text = f'Use the terminal to execute exactly once: python3 -c "import time; open(\'{filename}\', \'a\').write(\'once\\n\'); time.sleep({seconds})". Then reply DONE. Do not run it a second time. This is a controlled recovery test.'
        return {'sessionId': identity, 'prompt': [{'type': 'text', 'text': text}], '_meta': {'goose': {'requestId': str(uuid.uuid4())}}}
    async with ClientSession() as session:
        try:
            await start_bridge()
            phone = await Phone(session, token.read_text()).connect()
            identity = (await phone.call('session/new', {'cwd': str(fixture), '_meta': {'workspace': {'kind': 'project', 'path': str(fixture)}}}))['sessionId']
            print(json.dumps({'fixture': str(fixture), 'thread': identity}), flush=True)
            request = prompt(identity, 'vpn.txt', 8)
            waiting = phone.start('session/prompt', request)
            await until(lambda: marked('vpn.txt'))
            await phone.close()
            await asyncio.gather(waiting, return_exceptions=True)
            phone = await Phone(session, token.read_text()).connect()
            await until(lambda: finished(phone, identity))
            await phone.call('session/prompt', request)
            assert (fixture / 'vpn.txt').read_text() == 'once\n'
            print('PASS transport loss, rejoin and identical-request retry execute once', flush=True)
            request = prompt(identity, 'adapter.txt', 12)
            waiting = phone.start('session/prompt', request)
            await until(lambda: marked('adapter.txt'))
            await stop_bridge(); await asyncio.gather(waiting, return_exceptions=True)
            await start_bridge()
            phone = await Phone(session, token.read_text()).connect()
            await until(lambda: finished(phone, identity))
            await phone.call('session/prompt', request)
            assert (fixture / 'adapter.txt').read_text() == 'once\n'
            print('PASS adapter restart rejoins live Codex work with one execution', flush=True)
            request = prompt(identity, 'backend.txt', 120)
            waiting = phone.start('session/prompt', request)
            await until(lambda: marked('backend.txt'))
            previous_epoch = bridge.rpc.epoch
            subprocess.run(['systemctl', '--user', 'kill', '--kill-whom=main', '--signal=KILL', unit + '.service'], check=True)
            await asyncio.gather(waiting, return_exceptions=True)
            subprocess.run(['systemctl', '--user', 'restart', unit + '.service'], check=True)
            async def ready(): return bridge.rpc.ready.is_set() and bridge.rpc.epoch != previous_epoch
            await until(ready, 45)
            phone = await Phone(session, token.read_text()).connect()
            history = await phone.call('session/load', {'sessionId': identity})
            interrupted = history['_meta']['hostRuns'][-1]
            assert interrupted['state'] == 'interrupted', interrupted
            assert interrupted['recovery']['uncertainTools'] >= 1, interrupted
            assert interrupted['recovery']['readOnlyReview']
            try:
                await phone.call('session/prompt', request)
            except RuntimeError as error:
                assert 'will not be sent or executed again' in str(error)
            else: raise AssertionError('Uncertain submission was dispatched twice')
            assert (fixture / 'backend.txt').read_text() == 'once\n'
            await phone.call('_goose/unstable/session/resume_run', {'sessionId': identity, 'runId': interrupted['id']})
            assert (fixture / 'backend.txt').read_text() == 'once\n'
            print('PASS backend hard crash preserves checkpoint and read-only review never repeats action', flush=True)
            result = {'root': str(root), 'thread': identity, 'vpn_interruption': True, 'duplicate_request_deduplication': True, 'adapter_restart': True, 'backend_hard_crash': True, 'read_only_review': True, 'marker_counts': {name: (fixture / name).read_text().count('once\n') for name in ['vpn.txt', 'adapter.txt', 'backend.txt']}}
            (root / 'result.json').write_text(json.dumps(result, indent=2) + '\n')
            print(json.dumps(result, indent=2), flush=True)
            await phone.close()
        finally:
            if runner: await runner.cleanup()
            if bridge: bridge.journal.close()
            subprocess.run(['systemctl', '--user', 'stop', unit + '.service'], capture_output=True)


if __name__ == '__main__':
    asyncio.run(main())
