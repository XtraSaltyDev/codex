#!/usr/bin/env python3
"""Expose a private Goose ACP endpoint backed by the running Codex app-server."""

import asyncio
import contextlib
import datetime as dt
import hmac
import json
import logging
import os
from pathlib import Path
import sys
import uuid

sys.path.insert(0, str(Path.home() / '.local/share/codex-spark/mobile-python'))
from aiohttp import ClientSession, WSMsgType, web


class RPCError(Exception):
    def __init__(self, message, code=-32602):
        super().__init__(message)
        self.code = code


def iso(timestamp=None):
    return dt.datetime.fromtimestamp(timestamp, dt.timezone.utc).isoformat() if timestamp else dt.datetime.now(dt.timezone.utc).isoformat()


def run_status(turn):
    items = turn.get('items', [])
    users = [i['id'] for i in items if i['type'] == 'userMessage']
    answers = [i['id'] for i in items if i['type'] == 'agentMessage' and i.get('text')]
    return {'id': turn['id'], 'state': {'completed': 'finished', 'inProgress': 'running', 'interrupted': 'cancelled', 'failed': 'failed'}.get(turn['status'], 'interrupted'),
            'userMessageId': users[0] if users else None, 'finalMessageId': answers[-1] if answers else None}


def item_update(item, completed=True):
    kind, identity = item['type'], item['id']
    if kind in ('agentMessage', 'userMessage'):
        text = item.get('text', '') if kind == 'agentMessage' else '\n'.join(c.get('text', '[Attachment]') for c in item.get('content', []))
        return {'sessionUpdate': 'agent_message_chunk' if kind == 'agentMessage' else 'user_message_chunk',
                'messageId': identity, 'content': {'type': 'text', 'text': text}}
    if kind in ('commandExecution', 'fileChange', 'mcpToolCall', 'dynamicToolCall', 'webSearch'):
        title = item.get('command') or item.get('tool') or ('Edit ' + ', '.join(c.get('path', '') for c in item.get('changes', [])) if kind == 'fileChange' else kind)
        return {'sessionUpdate': 'tool_call_update' if completed else 'tool_call', 'toolCallId': identity,
                'title': title, 'status': ('failed' if item.get('status') in ('failed', 'declined') else 'completed') if completed else 'in_progress'}
    return None


class CodexRPC:
    def __init__(self, bridge):
        self.bridge = bridge
        self.ws = None
        self.pending = {}
        self.number = 0
        self.ready = asyncio.Event()

    async def request(self, method, params):
        await asyncio.wait_for(self.ready.wait(), 15)
        return await self.call(method, params)

    async def call(self, method, params):
        self.number += 1
        identity = self.number
        future = asyncio.get_running_loop().create_future()
        self.pending[identity] = future
        try:
            await self.ws.send_json({'id': identity, 'method': method, 'params': params})
            return await asyncio.wait_for(future, 60)
        finally:
            self.pending.pop(identity, None)

    async def read(self):
        async for frame in self.ws:
            if frame.type != WSMsgType.TEXT:
                continue
            packet = json.loads(frame.data)
            if 'method' not in packet:
                future = self.pending.get(packet.get('id'))
                if future and not future.done():
                    if 'error' in packet:
                        error = packet['error']
                        future.set_exception(RPCError(error['message'], error['code']))
                    else:
                        future.set_result(packet.get('result', {}))
            else:
                await self.bridge.event(packet)

    async def maintain(self):
        async with ClientSession() as session:
            while True:
                reader = None
                try:
                    token = self.bridge.upstream_token.read_text().strip()
                    async with session.ws_connect(self.bridge.upstream_url, headers={'Authorization': 'Bearer ' + token}, max_msg_size=32 * 1024 * 1024) as self.ws:
                        reader = asyncio.create_task(self.read())
                        await self.call('initialize', {'clientInfo': {'name': 'goose_mobile_codex', 'title': 'Goose mobile on ai-research', 'version': '1.0.0'}, 'capabilities': {'experimentalApi': True}})
                        await self.ws.send_json({'method': 'initialized'})
                        self.ready.set()
                        logging.info('Connected to Codex app-server')
                        await reader
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    logging.warning('Codex connection ended: %s', type(error).__name__)
                finally:
                    self.ready.clear()
                    self.bridge.loaded.clear()
                    for future in list(self.pending.values()):
                        if not future.done():
                            future.set_exception(RPCError('Codex connection ended. Reconnect to load saved history.', -32000))
                    for pending in list(self.bridge.permissions.values()):
                        pending['obsolete'] = True
                    self.bridge.permissions.clear()
                    for identity, future in list(self.bridge.waiters.items()):
                        if not future.done():
                            future.set_result('interrupted')
                    self.bridge.waiters.clear()
                    if reader:
                        reader.cancel()
                        with contextlib.suppress(asyncio.CancelledError, Exception):
                            await reader
                await asyncio.sleep(3)


class Bridge:
    def __init__(self, state, token, upstream_token, upstream_url='ws://127.0.0.1:4500'):
        self.state = Path(state)
        self.state.mkdir(parents=True, exist_ok=True)
        self.metadata_path = self.state / 'metadata.json'
        self.metadata = json.loads(self.metadata_path.read_text()) if self.metadata_path.exists() else {'sessions': {}, 'projects': []}
        self.token = Path(token).read_text().strip()
        self.upstream_token = Path(upstream_token)
        self.upstream_url = upstream_url
        self.rpc = CodexRPC(self)
        self.clients = {}
        self.loading = set()
        self.loaded = set()
        self.turns = {}
        self.waiters = {}
        self.permissions = {}
        self.tasks = set()
        self.locks = {}
        self.stream_locks = {}
        self.root = self.state / 'chats'
        self.root.mkdir(exist_ok=True)

    def save(self):
        temporary = self.metadata_path.with_suffix('.tmp')
        temporary.write_text(json.dumps(self.metadata, indent=2) + '\n')
        temporary.chmod(0o600)
        temporary.replace(self.metadata_path)

    def spawn(self, coroutine):
        task = asyncio.create_task(coroutine)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return task

    async def send(self, ws, packet):
        if not ws.closed:
            with contextlib.suppress(ConnectionError, RuntimeError, TimeoutError):
                await asyncio.wait_for(ws.send_json({'jsonrpc': '2.0', **packet}), 10)

    async def update(self, identity, update, only=None):
        packet = {'method': 'session/update', 'params': {'sessionId': identity, 'update': update}}
        clients = [only] if only is not None else [ws for ws, ids in self.clients.items() if identity in ids and (ws, identity) not in self.loading]
        await asyncio.gather(*(self.send(ws, packet) for ws in clients))

    async def event(self, packet):
        identity = packet.get('params', {}).get('threadId')
        if identity and 'id' not in packet:
            async with self.stream_locks.setdefault(identity, asyncio.Lock()):
                await self.apply_event(packet)
        else:
            await self.apply_event(packet)

    async def apply_event(self, packet):
        method, params = packet['method'], packet.get('params', {})
        identity = params.get('threadId')
        if 'id' in packet:
            if method in ('item/commandExecution/requestApproval', 'item/fileChange/requestApproval', 'item/tool/requestUserInput'):
                key = 'codex-' + str(packet['id'])
                self.permissions[key] = {'packet': packet, 'obsolete': False}
                await self.deliver_permission(key)
            else:
                await self.rpc.ws.send_json({'id': packet['id'], 'error': {'code': -32601, 'message': 'This mobile adapter does not support ' + method}})
            return
        if not identity:
            return
        if method == 'turn/started':
            self.turns[identity] = params['turn']
            await self.update(identity, {'sessionUpdate': 'session_info_update', '_meta': {'goose': {'hostRun': run_status(params['turn'])}}})
        elif method == 'item/agentMessage/delta':
            turn = self.turns.get(identity)
            if turn:
                item = next((i for i in turn['items'] if i['id'] == params['itemId']), None)
                if item is None:
                    item = {'id': params['itemId'], 'type': 'agentMessage', 'text': ''}
                    turn['items'].append(item)
                item['text'] += params['delta']
            await self.update(identity, {'sessionUpdate': 'agent_message_chunk', 'messageId': params['itemId'], 'content': {'type': 'text', 'text': params['delta']}})
        elif method in ('item/started', 'item/completed'):
            item = params['item']
            turn = self.turns.get(identity)
            if turn:
                previous = next((i for i in turn['items'] if i['id'] == item['id']), None)
                if previous is None:
                    turn['items'].append(dict(item))
                else:
                    previous.update(item)
            if item['type'] == 'agentMessage':
                return  # Text arrives as deltas; history replay uses authoritative completed items.
            if item['type'] == 'userMessage' and method == 'item/completed':
                return
            update = item_update(item, method == 'item/completed')
            if update:
                await self.update(identity, update)
        elif method == 'turn/completed':
            turn = params['turn']
            cached = self.turns.get(identity)
            if not turn.get('items') and cached and cached['id'] == turn['id']:
                turn['items'] = cached['items']
            self.turns[identity] = turn
            await self.update(identity, {'sessionUpdate': 'session_info_update', '_meta': {'goose': {'hostRun': run_status(turn)}}})
            for key, pending in list(self.permissions.items()):
                if pending['packet'].get('params', {}).get('turnId') == turn['id']:
                    self.permissions.pop(key)
            future = self.waiters.pop(identity, None)
            if future and not future.done():
                future.set_result(turn['status'])
        elif method == 'thread/name/updated':
            await self.update(identity, {'sessionUpdate': 'session_info_update', 'title': params.get('threadName') or params.get('name') or 'New chat'})

    async def deliver_permission(self, key, only=None):
        packet = self.permissions[key]['packet']
        params = packet['params']; identity = params['threadId']
        if packet['method'] == 'item/tool/requestUserInput':
            properties = {q['id']: {'type': 'string', 'title': q.get('header', q['id']), 'description': q['question']} for q in params['questions']}
            message = {'id': key, 'method': 'elicitation/create', 'params': {'sessionId': identity, 'mode': 'form', 'message': 'Codex needs your input', 'requestedSchema': {'type': 'object', 'properties': properties, 'required': list(properties)}}}
        else:
            message = {'id': key, 'method': 'session/request_permission', 'params': {'sessionId': identity,
                       'toolCall': {'toolCallId': params['itemId'], 'title': params.get('command') or params.get('reason') or 'Allow file change outside the workspace?', 'rawInput': params},
                       'options': [{'optionId': 'accept', 'name': 'Allow once', 'kind': 'allow_once'}, {'optionId': 'decline', 'name': 'Decline', 'kind': 'reject_once'}]}}
        clients = [only] if only is not None else [ws for ws, ids in self.clients.items() if identity in ids]
        await asyncio.gather(*(self.send(ws, message) for ws in clients))

    async def answer(self, ws, packet):
        pending = self.permissions.get(packet.get('id'))
        if not pending or pending['obsolete']:
            return
        original = pending['packet']; params = original['params']
        if params['threadId'] not in self.clients.get(ws, set()):
            return
        response = packet.get('result', {})
        if original['method'] == 'item/tool/requestUserInput':
            values = response.get('content', {}) if response.get('action') == 'accept' else {}
            result = {'answers': {q['id']: {'answers': [str(values[q['id']])] if q['id'] in values else []} for q in params['questions']}}
        else:
            outcome = response.get('outcome', {})
            result = {'decision': 'accept' if outcome.get('outcome') == 'selected' and outcome.get('optionId') == 'accept' else 'decline'}
        self.permissions.pop(packet['id'], None)
        await self.rpc.ws.send_json({'id': original['id'], 'result': result})

    def session(self, thread, archived=False):
        meta = dict(self.metadata['sessions'].get(thread['id'], {}))
        meta.setdefault('workspace', {'kind': 'project', 'path': thread['cwd']})
        if archived:
            meta.setdefault('archivedAt', iso(thread['updatedAt']))
        return {'sessionId': thread['id'], 'cwd': thread['cwd'], 'title': thread.get('name') or thread.get('preview') or 'New chat', 'updatedAt': iso(thread['updatedAt']), '_meta': meta}

    async def ensure_loaded(self, identity, include_turns=False):
        if identity not in self.loaded:
            result = await self.rpc.request('thread/resume', {'threadId': identity, 'excludeTurns': not include_turns})
            self.loaded.add(identity)
            return result['thread']
        try:
            return (await self.rpc.request('thread/read', {'threadId': identity, 'includeTurns': include_turns}))['thread']
        except RPCError as error:
            if include_turns and 'not materialized yet' in str(error):
                return (await self.rpc.request('thread/read', {'threadId': identity}))['thread']
            raise

    async def dispatch(self, ws, method, params):
        identity = params.get('sessionId')
        if method == 'initialize':
            await asyncio.wait_for(self.rpc.ready.wait(), 15)
            return {'protocolVersion': 1, 'agentInfo': {'name': 'Codex Spark', 'version': '1.0.0'}, 'authMethods': [],
                    'agentCapabilities': {'loadSession': True, 'promptCapabilities': {}, 'sessionCapabilities': {'list': {}, 'delete': {}},
                                          '_meta': {'goose': {'hostOwnedRuns': True, 'workspaces': True, 'customNotifications': True}}}}
        if method == '_goose/unstable/workspaces':
            action = params.get('action', 'list')
            if action == 'add':
                path = str(Path(params['path']).expanduser().resolve(strict=True))
                if not Path(path).is_dir():
                    raise RPCError('Choose an existing directory on ai-research.')
                if path not in self.metadata['projects']:
                    self.metadata['projects'].append(path); self.save()
            elif action == 'assign':
                raise RPCError('Moving an existing Codex conversation to another folder is not supported. Start a new chat in that folder.')
            return {'projectlessRoot': str(self.root), 'projects': [{'path': p, 'name': Path(p).name} for p in self.metadata['projects']]}
        if method == 'session/list':
            query = params.get('_meta', {}).get('query', '')
            cursor = json.loads(params['cursor']) if params.get('cursor') else {'archived': False, 'cursor': None, 'query': query}
            if cursor['query'] != query:
                raise RPCError('Search cursor does not match query.')
            args = {'limit': 50, 'sortKey': 'updated_at', 'archived': cursor['archived']}
            if cursor['cursor']:
                args['cursor'] = cursor['cursor']
            method_name = 'thread/search' if query else 'thread/list'
            if query:
                args['searchTerm'] = query
            response = await self.rpc.request(method_name, args)
            sessions = [self.session(t['thread'] if query else t, cursor['archived']) for t in response['data']]
            next_cursor = response.get('nextCursor')
            result = {'sessions': sessions}
            if next_cursor or not cursor['archived']:
                result['nextCursor'] = json.dumps({'archived': cursor['archived'] if next_cursor else True, 'cursor': next_cursor, 'query': query})
            return result
        if method == 'session/new':
            workspace = params.get('_meta', {}).get('workspace', {'kind': 'project', 'path': params.get('cwd')})
            if workspace['kind'] == 'projectless':
                cwd = self.root / str(uuid.uuid4()); cwd.mkdir()
            else:
                cwd = Path(workspace.get('path') or params['cwd']).expanduser().resolve(strict=True)
                if not cwd.is_dir():
                    raise RPCError('Choose an existing directory on ai-research.')
            response = await self.rpc.request('thread/start', {'cwd': str(cwd), 'approvalPolicy': 'on-request', 'sandbox': 'workspace-write', 'historyMode': 'legacy'})
            thread = response['thread']; identity = thread['id']
            self.loaded.add(identity); self.clients[ws].add(identity)
            self.metadata['sessions'][identity] = {'workspace': workspace}
            if workspace['kind'] == 'project' and str(cwd) not in self.metadata['projects']:
                self.metadata['projects'].append(str(cwd))
            self.save()
            return {'sessionId': identity, '_meta': {'workingDir': str(cwd), 'workspace': workspace}}
        if method == 'session/load':
            self.loading.add((ws, identity))
            try:
                thread = await self.ensure_loaded(identity, include_turns=True)
                async with self.stream_locks.setdefault(identity, asyncio.Lock()):
                    self.clients[ws].add(identity)
                    turns = thread.get('turns', [])
                    active = self.turns.get(identity)
                    if active and active['status'] == 'inProgress':
                        turns = [t for t in turns if t['id'] != active['id']] + [active]
                    elif turns and turns[-1]['status'] == 'inProgress':
                        self.turns[identity] = turns[-1]
                    for turn in turns:
                        for item in turn.get('items', []):
                            update = item_update(item)
                            if update:
                                await self.update(identity, update, only=ws)
                    for key, pending in list(self.permissions.items()):
                        if pending['packet']['params']['threadId'] == identity:
                            await self.deliver_permission(key, only=ws)
                    return {'_meta': {'hostRuns': [run_status(t) for t in turns], 'workspace': self.session(thread)['_meta']['workspace']}}
            finally:
                self.loading.discard((ws, identity))
        if method == 'session/prompt':
            if any(c.get('type') != 'text' for c in params.get('prompt', [])):
                raise RPCError('This model currently accepts text only.')
            text = '\n'.join(c['text'] for c in params.get('prompt', []))
            if not text.strip():
                raise RPCError('Enter a message.')
            async with self.locks.setdefault(identity, asyncio.Lock()):
                thread = await self.ensure_loaded(identity)
                active = self.turns.get(identity)
                if identity in self.waiters or (active and active['status'] == 'inProgress') or thread.get('status', {}).get('type') == 'active':
                    raise RPCError('This conversation already has a running task. Reconnect to follow it.', -32000)
                self.clients[ws].add(identity)
                future = asyncio.get_running_loop().create_future(); self.waiters[identity] = future
                try:
                    await self.rpc.request('turn/start', {'threadId': identity, 'input': [{'type': 'text', 'text': text}]})
                except Exception:
                    self.waiters.pop(identity, None); raise
            status = await future
            if status == 'failed':
                raise RPCError('The Codex turn failed. Saved history is available; check the host log.', -32000)
            return {'stopReason': 'end_turn' if status == 'completed' else 'cancelled'}
        if method == 'session/cancel':
            turn = self.turns.get(identity)
            if turn and turn['status'] == 'inProgress':
                await self.rpc.request('turn/interrupt', {'threadId': identity, 'turnId': turn['id']})
            return {}
        if method == '_goose/unstable/session/rename':
            await self.rpc.request('thread/name/set', {'threadId': identity, 'name': params['title']})
            return {}
        if method in ('_goose/unstable/session/archive', '_goose/unstable/session/unarchive', 'session/delete'):
            if identity in self.waiters:
                raise RPCError('Stop the running task first.')
            action = method.rsplit('/', 1)[-1]
            await self.rpc.request('thread/' + action, {'threadId': identity})
            self.loaded.discard(identity)
            if action == 'delete':
                self.metadata['sessions'].pop(identity, None)
            elif action == 'archive':
                self.metadata['sessions'].setdefault(identity, {})['archivedAt'] = iso()
            else:
                self.metadata['sessions'].setdefault(identity, {}).pop('archivedAt', None)
            self.save()
            return {}
        raise RPCError('Unsupported method: ' + method, -32601)

    async def handle_packet(self, ws, packet):
        if 'method' not in packet:
            await self.answer(ws, packet); return
        try:
            result = await self.dispatch(ws, packet['method'], packet.get('params', {}))
            response = {'result': result}
        except RPCError as error:
            response = {'error': {'code': error.code, 'message': str(error)}}
        except Exception as error:
            logging.exception('Mobile method failed: %s', packet['method'])
            response = {'error': {'code': -32603, 'message': 'Host request failed: ' + type(error).__name__}}
        if 'id' in packet:
            await self.send(ws, {'id': packet['id'], **response})

    async def websocket(self, request):
        if not hmac.compare_digest(request.headers.get('X-Secret-Key', ''), self.token):
            raise web.HTTPUnauthorized()
        ws = web.WebSocketResponse(heartbeat=20, max_msg_size=4 * 1024 * 1024)
        await ws.prepare(request)
        self.clients[ws] = set()
        try:
            async for frame in ws:
                if frame.type == WSMsgType.TEXT:
                    try:
                        packet = json.loads(frame.data)
                        if not isinstance(packet, dict):
                            raise ValueError('Expected an object')
                        self.spawn(self.handle_packet(ws, packet))
                    except (ValueError, TypeError):
                        await self.send(ws, {'id': None, 'error': {'code': -32700, 'message': 'Invalid JSON request'}})
        finally:
            self.clients.pop(ws, None)
        return ws

    async def health(self, request):
        return web.json_response({'status': 'ok' if self.rpc.ready.is_set() else 'unavailable', 'backend': 'Codex Spark'}, status=200 if self.rpc.ready.is_set() else 503)


def app(bridge):
    application = web.Application()
    application.router.add_get('/acp', bridge.websocket)
    application.router.add_get('/status', bridge.health)
    async def lifecycle(application):
        upstream = asyncio.create_task(bridge.rpc.maintain())
        yield
        upstream.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await upstream
        for task in list(bridge.tasks):
            task.cancel()
        await asyncio.gather(*bridge.tasks, return_exceptions=True)
    application.cleanup_ctx.append(lifecycle)
    return application


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=3285)
    args = parser.parse_args()
    os.umask(0o077)
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    home = Path.home()
    bridge = Bridge(home / '.local/state/codex-spark/mobile', home / '.config/codex-spark/mobile.token', home / '.config/codex-spark/server.token')
    web.run_app(app(bridge), host='127.0.0.1', port=args.port, access_log=None)
