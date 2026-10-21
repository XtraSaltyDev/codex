#!/usr/bin/env python3
"""Verify mobile auto-naming and shared native names using the configured Spark backend."""

import asyncio
import json
from pathlib import Path
import secrets
import tempfile

from goose_mobile import Bridge, app
from aiohttp import ClientSession, WSMsgType, web


class Phone:
    def __init__(self, session, url, token):
        self.session, self.url, self.token = session, url, token
        self.number = 0
        self.events = []
        self.pending = {}

    async def connect(self):
        self.ws = await self.session.ws_connect(self.url, headers={'X-Secret-Key': self.token})
        self.reader = asyncio.create_task(self.read())
        await self.call('initialize', {'protocolVersion': 1})
        return self

    async def read(self):
        async for frame in self.ws:
            if frame.type != WSMsgType.TEXT:
                continue
            packet = json.loads(frame.data)
            self.events.append(packet)
            future = self.pending.get(packet.get('id'))
            if future and not future.done():
                if 'error' in packet:
                    future.set_exception(RuntimeError(str(packet['error'])))
                else:
                    future.set_result(packet.get('result', {}))

    async def call(self, method, params):
        self.number += 1
        number = self.number
        future = asyncio.get_running_loop().create_future()
        self.pending[number] = future
        try:
            await self.ws.send_json({'jsonrpc': '2.0', 'id': number, 'method': method, 'params': params})
            return await asyncio.wait_for(future, 120)
        finally:
            self.pending.pop(number, None)

    async def close(self):
        await self.ws.close()
        await self.reader


async def main():
    home = Path.home()
    root = home / '.local/state/codex-spark/title-acceptance'
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    fixture = Path(tempfile.mkdtemp(prefix='naming-', dir=root))
    token = fixture / 'token'
    token.write_text(secrets.token_urlsafe(32)); token.chmod(0o600)
    project = fixture / 'project'; project.mkdir()
    bridge = Bridge(fixture / 'adapter', token, home / '.config/codex-spark/server.token',
                    push_config=fixture / 'unconfigured-push.json')
    runner = web.AppRunner(app(bridge))
    identity = None
    try:
        await runner.setup()
        site = web.TCPSite(runner, '127.0.0.1', 0)
        await site.start()
        url = f'http://127.0.0.1:{runner.addresses[0][1]}/acp'
        await asyncio.wait_for(bridge.rpc.ready.wait(), 30)
        async with ClientSession() as session:
            phone = await Phone(session, url, token.read_text()).connect()
            identity = (await phone.call('session/new', {'cwd': str(project), 'mcpServers': []}))['sessionId']
            print(json.dumps({'fixture': str(fixture), 'thread': identity}), flush=True)
            # Keep the ordinary fixture turn read-only as well as the hidden title request.
            bridge.metadata['sessions'][identity]['fullAccess'] = False
            bridge.save()
            result = await phone.call('session/prompt', {'sessionId': identity,
                '_meta': {'goose': {'requestId': 'title-acceptance-first'}},
                'prompt': [{'type': 'text', 'text': 'Explain how to fix a login timeout in one short sentence. Do not use tools.'}]})
            assert result['stopReason'] == 'end_turn', result
            async with asyncio.timeout(90):
                while bridge.titles.pending:
                    await asyncio.sleep(.1)
            native = (await bridge.rpc.request('thread/read', {'threadId': identity, 'includeTurns': True}))['thread']
            generated = native.get('name')
            assert generated and len(generated) <= 36, native
            assert len(native['turns']) == 1, 'Hidden naming must not add a conversation turn'
            assert not any(t['snapshot'].get('items') and t['thread'] != identity for t in bridge.journal.turns())
            updates = [p['params']['update'] for p in phone.events if p.get('method') == 'session/update']
            assert any(u.get('title') == generated for u in updates), updates
            assert all('title' not in u or u['sessionUpdate'] == 'session_info_update' for u in updates)
            await phone.call('_goose/unstable/session/rename', {'sessionId': identity, 'title': 'Manual naming acceptance'})
            await phone.close()
            phone = await Phone(session, url, token.read_text()).connect()
            await phone.call('session/load', {'sessionId': identity, 'mcpServers': []})
            assert any(p.get('params', {}).get('update', {}).get('title') == 'Manual naming acceptance' for p in phone.events)
            # A native rename follows the same API used by /rename in the terminal.
            await bridge.rpc.request('thread/name/set', {'threadId': identity, 'name': 'Terminal naming acceptance'})
            async with asyncio.timeout(10):
                while not any(p.get('params', {}).get('update', {}).get('title') == 'Terminal naming acceptance' for p in phone.events):
                    await asyncio.sleep(.05)
            await phone.close()
            await bridge.rpc.request('thread/archive', {'threadId': identity})
            report = {'thread': identity, 'generatedTitle': generated, 'model': bridge.backend['modelId'],
                      'automaticTitle': True, 'nativePersistence': True, 'mobileTitleUpdate': True,
                      'manualRenameReplay': True, 'nativeRenameBroadcast': True, 'visibleTurns': len(native['turns']),
                      'archived': True}
            (fixture / 'result.json').write_text(json.dumps(report, indent=2) + '\n')
            print(json.dumps(report, indent=2), flush=True)
    finally:
        await runner.cleanup()
        bridge.journal.close()


if __name__ == '__main__':
    asyncio.run(main())
