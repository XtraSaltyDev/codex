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
import socket
import sys
import uuid

sys.path.insert(0, str(Path.home() / '.local/share/codex-spark/mobile-python'))
from aiohttp import ClientSession, WSMsgType, web
from mobile_state import MobileState, merge_items
from health import SparkHealth
from work_review import capture as capture_review, finish as finish_review, visible_text


class RPCError(Exception):
    def __init__(self, message, code=-32602):
        super().__init__(message)
        self.code = code


def iso(timestamp=None):
    return dt.datetime.fromtimestamp(timestamp, dt.timezone.utc).isoformat() if timestamp else dt.datetime.now(dt.timezone.utc).isoformat()


def browse_folders(params):
    try:
        requested = params.get('path') or str(Path.home())
        if not isinstance(requested, str) or not Path(requested).expanduser().is_absolute():
            raise RPCError('Choose an absolute folder path on the connected host.')
        path = Path(requested).expanduser().resolve(strict=True)
        if not path.is_dir():
            raise RPCError('Choose an existing folder on the connected host.')
        offset = params.get('offset', 0)
        if type(offset) is not int or offset < 0:
            raise RPCError('Invalid folder page.')
        if params.get('action') == 'createFolder':
            name = params.get('name', '')
            if not isinstance(name, str) or not name.strip() or name != name.strip() or name in ('.', '..') or '/' in name or '\x00' in name:
                raise RPCError('Enter a folder name without slashes or leading or trailing spaces.')
            path = path / name
            path.mkdir(mode=0o700)
            offset = 0
        children = []
        with os.scandir(path) as entries:
            for entry in entries:
                if entry.name.startswith('.') and params.get('showHidden') is not True:
                    continue
                try:
                    if entry.is_dir():
                        children.append({'name': entry.name, 'path': str(path / entry.name)})
                except OSError:
                    continue
        children.sort(key=lambda entry: (entry['name'].casefold(), entry['name']))
        end = offset + 200
        return {'path': str(path), 'parent': str(path.parent) if path.parent != path else None,
                'home': str(Path.home().resolve()), 'directories': children[offset:end],
                'nextOffset': end if end < len(children) else None}
    except FileExistsError:
        raise RPCError('A file or folder with that name already exists. Choose another name or open the existing folder.')
    except (OSError, ValueError, RuntimeError) as error:
        raise RPCError(f'Cannot open or create this folder: {error}') from error


def run_status(turn):
    items = turn.get('items', [])
    users = [i['id'] for i in items if i['type'] == 'userMessage']
    answers = [i['id'] for i in items if i['type'] == 'agentMessage' and i.get('text')]
    return {'id': turn['id'], 'state': {'completed': 'finished', 'inProgress': 'running', 'interrupted': 'cancelled', 'failed': 'failed', 'backendInterrupted': 'interrupted'}.get(turn['status'], 'interrupted'),
            'userMessageId': users[0] if users else None, 'finalMessageId': answers[-1] if answers else None}


def item_update(item, completed=True):
    kind, identity = item['type'], item['id']
    if kind in ('agentMessage', 'userMessage'):
        text = item.get('text', '') if kind == 'agentMessage' else '\n'.join(c.get('text', '[Attachment]') for c in item.get('content', []))
        return {'sessionUpdate': 'agent_message_chunk' if kind == 'agentMessage' else 'user_message_chunk',
                'messageId': identity, 'content': {'type': 'text', 'text': text}}
    if kind in ('commandExecution', 'fileChange', 'mcpToolCall', 'dynamicToolCall', 'webSearch'):
        title = item.get('command') or item.get('tool') or ('Edit ' + ', '.join(c.get('path', '') for c in item.get('changes', [])) if kind == 'fileChange' else kind)
        uncertain = bool(item.get('_mobileUncertain'))
        if uncertain:
            title = 'Outcome unconfirmed: ' + title
        completed = completed and item.get('status') != 'inProgress'
        update = {'sessionUpdate': 'tool_call_update' if completed else 'tool_call', 'toolCallId': identity,
                  'title': visible_text(title, 4096)[0], 'status': 'failed' if uncertain or item.get('status') in ('failed', 'declined') or item.get('exitCode') not in (None, 0) else 'completed' if completed else 'in_progress'}
        if kind == 'commandExecution':
            output, truncated = visible_text(item.get('aggregatedOutput') or '')
            update['_meta'] = {'goose': {'commandResult': {'output': output, 'exitCode': item.get('exitCode'),
                               'truncated': truncated, 'uncertain': bool(item.get('_mobileUncertain'))}}}
        return update
    return None


class CodexRPC:
    def __init__(self, bridge):
        self.bridge = bridge
        self.ws = None
        self.pending = {}
        self.number = 0
        self.ready = asyncio.Event()
        self.epoch = str(uuid.uuid4())

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
                        self.epoch = str(uuid.uuid4())
                        reader = asyncio.create_task(self.read())
                        await self.call('initialize', {'clientInfo': {'name': 'goose_mobile_codex', 'title': 'Goose mobile on ai-research', 'version': '1.0.0'}, 'capabilities': {'experimentalApi': True}})
                        await self.ws.send_json({'method': 'initialized'})
                        await self.bridge.reconcile()
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
                            future.set_exception(RPCError('Backend connection interrupted. Saved progress will be restored; the request will not be resent.', -32010))
                    self.bridge.waiters.clear()
                    await asyncio.gather(*(ws.close(code=1012, message=b'Backend reconnecting; do not resend') for ws in list(self.bridge.clients)), return_exceptions=True)
                    if reader:
                        reader.cancel()
                        with contextlib.suppress(asyncio.CancelledError, Exception):
                            await reader
                await asyncio.sleep(3)


class Bridge:
    def __init__(self, state, token, upstream_token, upstream_url='ws://127.0.0.1:4500', projectless_root=None):
        self.state = Path(state)
        self.state.mkdir(parents=True, exist_ok=True)
        self.metadata_path = self.state / 'metadata.json'
        self.metadata = json.loads(self.metadata_path.read_text()) if self.metadata_path.exists() else {'sessions': {}, 'projects': []}
        self.journal = MobileState(self.state / 'recovery.sqlite3')
        self.token = Path(token).read_text().strip()
        self.upstream_token = Path(upstream_token)
        self.upstream_url = upstream_url
        self.rpc = CodexRPC(self)
        self.clients = {}
        self.loading = set()
        self.loaded = set()
        self.turns = {}
        self.inflight = {}
        self.waiters = {}
        self.permissions = {}
        self.tasks = set()
        self.locks = {}
        self.stream_locks = {}
        self.backend = {}
        self.health_probe = SparkHealth(state=self.state, upstream_url=upstream_url)
        self.root = Path(projectless_root).expanduser().resolve() if projectless_root is not None else self.state / 'chats'
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)

    def status(self, turn):
        status = run_status(turn)
        result = self.journal.review_result(turn['id'])
        if result is not None:
            status['workReview'] = result
        if turn['status'] == 'backendInterrupted':
            record = next((r for r in self.journal.turns() if r['id'] == turn['id']), None)
            tools = [i for i in turn.get('items', []) if item_update(i) and i['type'] not in ('userMessage', 'agentMessage')]
            status['recovery'] = {'runId': turn['id'], 'canResume': bool(record and not record['reviewed']), 'readOnlyReview': True,
                                  'completedTools': sum(i.get('status') == 'completed' for i in tools),
                                  'uncertainTools': max(1, sum(bool(i.get('_mobileUncertain')) for i in tools))}
        return status

    def saved_history(self, identity, thread):
        turns = {t['id']: t for t in thread.get('turns', [])}
        for record in self.journal.turns(identity):
            saved = record['snapshot']
            turns[saved['id']] = saved
        return list(turns.values())

    async def reconcile(self):
        # Reattach and observe. No prompt, approval decision, or tool is retransmitted.
        identities = {r['thread'] for r in self.journal.turns() if r['snapshot']['status'] == 'inProgress'}
        identities.update(r['thread'] for r in self.journal.db.execute("SELECT thread FROM submissions WHERE phase IN ('prepared','dispatching')"))
        for identity in identities:
            try:
                result = await self.rpc.call('thread/resume', {'threadId': identity})
                self.remember_settings(result)
                thread = result['thread']; self.loaded.add(identity)
            except RPCError as error:
                if not any(word in str(error).lower() for word in ('not found', 'no rollout', 'no session', 'does not exist', 'not materialized', 'not loaded')):
                    raise
                thread = {'turns': [], 'status': {'type': 'notLoaded'}}
            native = {t['id']: t for t in thread.get('turns', [])}
            active = thread.get('status', {}).get('type') == 'active'
            for submission in self.journal.for_thread(identity):
                matched = False
                for turn in native.values():
                    if any(i.get('clientId') == submission['id'] for i in turn.get('items', []) if i['type'] == 'userMessage'):
                        self.journal.phase(submission['id'], 'finished' if turn['status'] in ('completed', 'failed') else 'accepted', turn['id'])
                        matched = True
                if not matched and submission['phase'] in ('prepared', 'dispatching') and not submission['turn'] and not active:
                    checkpoint = {'id': submission['id'], 'status': 'backendInterrupted', 'items': [{'id': submission['id'], 'type': 'userMessage', 'content': [{'type': 'text', 'text': submission['text']}]}]}
                    self.journal.save_turn(identity, checkpoint)
                    self.journal.phase(submission['id'], 'unconfirmed')
                    await self.complete_review(checkpoint, interrupted=True)
            for record in self.journal.turns(identity):
                saved = record['snapshot']
                if saved['status'] != 'inProgress':
                    continue
                current = native.get(saved['id'])
                if active:
                    current = current or saved
                    # The durable stream may contain output not yet flushed into native history.
                    current['items'] = merge_items(current.get('items', []), saved.get('items', [])); current['status'] = 'inProgress'
                elif current and current['status'] == 'completed':
                    current['items'] = merge_items(current.get('items', []), saved.get('items', []))
                else:
                    current = saved
                    current['status'] = 'interrupted' if record['stopped'] else 'backendInterrupted'
                    for item in current.get('items', []):
                        if item.get('status') == 'inProgress':
                            item['_mobileUncertain'] = True
                self.turns[identity] = current
                self.journal.save_turn(identity, current)
                if current['status'] != 'inProgress':
                    await self.complete_review(current, interrupted=True)
                if record['stopped'] and active:
                    await self.rpc.call('turn/interrupt', {'threadId': identity, 'turnId': current['id']})

    async def acknowledge(self, identity, request):
        if request:
            await self.update(identity, {'sessionUpdate': 'session_info_update', '_meta': {'goose': {'acceptedRequestId': request, 'sessionSettings': self.session_settings(identity)}}})

    async def start_prompt(self, ws, identity, text, request, review=False):
        async with self.locks.setdefault(identity, asyncio.Lock()):
            try:
                submission, fresh = self.journal.prepare(request, identity, text)
            except ValueError as error:
                raise RPCError(str(error)) from error
            self.clients[ws].add(identity)
            if not fresh and submission['phase'] != 'prepared':
                await self.acknowledge(identity, request if submission['phase'] in ('accepted', 'finished') else None)
                submission = self.journal.submission(request)
                if identity in self.waiters and self.inflight.get(identity) == request:
                    future = self.waiters[identity]
                elif submission['phase'] == 'finished':
                    turn = next((r['snapshot'] for r in self.journal.turns(identity) if r['id'] == submission['turn']), None)
                    if turn and turn['status'] == 'backendInterrupted':
                        raise RPCError('This message was already submitted. Load its saved progress; it will not be sent or executed again.', -32011)
                    return {'stopReason': 'end_turn' if turn and turn['status'] == 'completed' else 'cancelled', '_meta': {'goose': {'acceptedRequestId': request}}}
                elif submission['phase'] == 'accepted' and self.turns.get(identity, {}).get('status') == 'inProgress':
                    future = asyncio.get_running_loop().create_future(); self.waiters[identity] = future
                    self.inflight[identity] = request
                else:
                    raise RPCError('This message was already submitted. Load its saved progress; it will not be sent or executed again.', -32011)
            else:
                thread = await self.ensure_loaded(identity)
                active = self.turns.get(identity)
                if identity in self.waiters or (active and active['status'] == 'inProgress') or thread.get('status', {}).get('type') == 'active':
                    raise RPCError('This conversation already has a running task. Reconnect to follow it.', -32000)
                baseline = await asyncio.to_thread(capture_review, thread['cwd'])
                self.journal.prepare_review(request, identity, baseline)
                future = asyncio.get_running_loop().create_future(); self.waiters[identity] = future
                self.inflight[identity] = request
                self.journal.phase(request, 'dispatching')
                args = {'threadId': identity, 'clientUserMessageId': request, 'input': [{'type': 'text', 'text': text}]}
                if review:
                    args.update({'approvalPolicy': 'never', 'sandboxPolicy': {'type': 'readOnly'}})
                elif self.metadata['sessions'].get(identity, {}).get('fullAccess', False):
                    args.update({'approvalPolicy': 'never', 'sandboxPolicy': {'type': 'dangerFullAccess'}})
                else:
                    args.update({'approvalPolicy': 'on-request', 'sandboxPolicy': {'type': 'workspaceWrite', 'writableRoots': [thread['cwd']], 'networkAccess': True, 'excludeTmpdirEnvVar': False, 'excludeSlashTmp': False}})
                try:
                    response = await self.rpc.request('turn/start', args)
                    turn = response['turn']
                    if self.journal.submission(request)['phase'] != 'finished':
                        self.journal.phase(request, 'accepted', turn['id'])
                    await self.acknowledge(identity, request)
                except Exception:
                    self.waiters.pop(identity, None); self.inflight.pop(identity, None)
                    raise
        status = await asyncio.shield(future)
        if status == 'failed':
            raise RPCError('The Codex turn failed. Saved history is available; check the host log.', -32000)
        return {'stopReason': 'end_turn' if status == 'completed' else 'cancelled', '_meta': {'goose': {'acceptedRequestId': request}}}

    def save(self):
        temporary = self.metadata_path.with_suffix('.tmp')
        temporary.write_text(json.dumps(self.metadata, indent=2) + '\n')
        temporary.chmod(0o600)
        temporary.replace(self.metadata_path)

    async def complete_review(self, turn, interrupted=False):
        pending = self.journal.pending_review(turn['id'])
        if pending:
            result = await asyncio.to_thread(finish_review, pending['baseline'], interrupted)
            self.journal.finish_review(pending['request'], result)

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
                turn = self.journal.record_event(identity, packet)
                if turn:
                    self.turns[identity] = turn
                await self.apply_event(packet)
        else:
            await self.apply_event(packet)

    async def apply_event(self, packet):
        method, params = packet['method'], packet.get('params', {})
        identity = params.get('threadId')
        if 'id' in packet:
            if method in ('item/commandExecution/requestApproval', 'item/fileChange/requestApproval', 'item/tool/requestUserInput'):
                key = 'codex-' + self.rpc.epoch + '-' + str(packet['id'])
                self.permissions[key] = {'packet': packet, 'obsolete': False}
                await self.deliver_permission(key)
            else:
                await self.rpc.ws.send_json({'id': packet['id'], 'error': {'code': -32601, 'message': 'This mobile adapter does not support ' + method}})
            return
        if not identity:
            return
        if method == 'turn/started':
            request = self.inflight.get(identity)
            if request:
                self.journal.phase(request, 'accepted', params['turn']['id'])
            await self.acknowledge(identity, request)
            await self.update(identity, {'sessionUpdate': 'session_info_update', '_meta': {'goose': {'hostRun': self.status(params['turn'])}}})
        elif method == 'item/agentMessage/delta':
            await self.update(identity, {'sessionUpdate': 'agent_message_chunk', 'messageId': params['itemId'], 'content': {'type': 'text', 'text': params['delta']}})
        elif method in ('item/started', 'item/completed'):
            item = params['item']
            if item['type'] == 'userMessage' and item.get('clientId'):
                request = self.journal.submission(item['clientId'])
                if request:
                    self.journal.phase(request['id'], 'accepted', params.get('turnId'))
                    await self.acknowledge(identity, request['id'])
            if item['type'] == 'agentMessage':
                return  # Text arrives as deltas; history replay uses authoritative completed items.
            if item['type'] == 'userMessage' and method == 'item/completed':
                return
            update = item_update(item, method == 'item/completed')
            if update:
                await self.update(identity, update)
        elif method == 'turn/completed':
            turn = self.turns[identity]
            record = next((r for r in self.journal.turns(identity) if r['id'] == turn['id']), None)
            if turn['status'] == 'interrupted' and not (record and record['stopped']):
                turn['status'] = 'backendInterrupted'
                for item in turn.get('items', []):
                    if item.get('status') == 'inProgress': item['_mobileUncertain'] = True
                self.journal.save_turn(identity, turn)
            await self.complete_review(turn, interrupted=turn['status'] != 'completed')
            for request in self.journal.for_thread(identity):
                if request['turn'] == turn['id'] and turn['status'] != 'backendInterrupted':
                    self.journal.phase(request['id'], 'finished', turn['id'])
            self.inflight.pop(identity, None)
            await self.update(identity, {'sessionUpdate': 'session_info_update', '_meta': {'goose': {'hostRun': self.status(turn)}}})
            for key, pending in list(self.permissions.items()):
                if pending['packet'].get('params', {}).get('turnId') == turn['id']:
                    self.permissions.pop(key)
            future = self.waiters.pop(identity, None)
            if future and not future.done():
                if turn['status'] == 'backendInterrupted':
                    future.set_exception(RPCError('Backend work was interrupted. Saved progress is available for read-only review; the request will not be repeated.', -32010))
                else:
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
        meta['goose'] = {'sessionSettings': self.session_settings(thread['id'])}
        meta.setdefault('workspace', {'kind': 'project', 'path': thread['cwd']})
        if archived:
            meta.setdefault('archivedAt', iso(thread['updatedAt']))
        return {'sessionId': thread['id'], 'cwd': thread['cwd'], 'title': thread.get('name') or thread.get('preview') or 'New chat', 'updatedAt': iso(thread['updatedAt']), '_meta': meta}

    def session_settings(self, identity):
        meta = self.metadata['sessions'].get(identity, {})
        full = meta.get('fullAccess', False)
        return {'access': 'full' if full else 'workspace', 'networkAccess': True,
                'approvalPolicy': 'never' if full else 'on-request', 'modelId': meta.get('modelId')}

    def remember_settings(self, result):
        meta = self.metadata['sessions'].setdefault(result['thread']['id'], {})
        if result.get('model'):
            meta['modelId'] = result['model']
        if result.get('sandbox', {}).get('type') == 'dangerFullAccess':
            meta['fullAccess'] = True
        self.save()

    async def ensure_loaded(self, identity, include_turns=False):
        if identity not in self.loaded:
            result = await self.rpc.request('thread/resume', {'threadId': identity, 'excludeTurns': not include_turns})
            self.remember_settings(result)
            self.loaded.add(identity)
            return result['thread']
        try:
            return (await self.rpc.request('thread/read', {'threadId': identity, 'includeTurns': include_turns}))['thread']
        except RPCError as error:
            if include_turns and 'not materialized yet' in str(error):
                return (await self.rpc.request('thread/read', {'threadId': identity}))['thread']
            raise

    async def confirm_deleted_thread(self, identity, error):
        missing_messages = {f'no rollout found for thread id {identity}',
                            f'thread not found: {identity}', f'thread not loaded: {identity}'}
        if error.code not in (-32600, -32602) or str(error) not in missing_messages:
            raise error
        if identity in (await self.rpc.request('thread/loaded/list', {}))['data']:
            raise error
        # A missing rollout alone does not establish that indexed or archived work is gone.
        async for _, thread in self.indexed_threads():
            if thread['id'] == identity:
                raise error

    async def indexed_threads(self):
        for archived in (False, True):
            cursor = None
            seen = set()
            while True:
                args = {'archived': archived, 'limit': 100, 'modelProviders': [], 'useStateDbOnly': True,
                        'sourceKinds': ['cli', 'vscode', 'exec', 'appServer', 'subAgent', 'subAgentReview',
                                        'subAgentCompact', 'subAgentThreadSpawn', 'subAgentOther', 'unknown']}
                if cursor:
                    args['cursor'] = cursor
                page = await self.rpc.request('thread/list', args)
                for thread in page['data']:
                    yield archived, thread
                cursor = page.get('nextCursor')
                if not cursor:
                    break
                if cursor in seen:
                    raise RPCError('Could not verify saved chat state. Reconnect and try again.', -32000)
                seen.add(cursor)

    async def draft_session_states(self, identities):
        if not isinstance(identities, list) or len(identities) > 100 or any(not isinstance(identity, str) or not identity or len(identity) > 128 for identity in identities):
            raise RPCError('Provide up to 100 saved chat IDs.')
        identities = list(dict.fromkeys(identities))
        if not identities:
            return {'sessions': []}
        loaded = set((await self.rpc.request('thread/loaded/list', {}))['data'])
        states = {identity: {'sessionId': identity, 'state': 'available' if identity in loaded else 'missing'} for identity in identities}
        async for archived, thread in self.indexed_threads():
            if thread['id'] in states:
                session = self.session(thread, archived)
                if not archived:
                    session['_meta'].pop('archivedAt', None)
                states[thread['id']] = {'sessionId': thread['id'], 'state': 'archived' if archived else 'available', 'session': session}
        return {'sessions': list(states.values())}

    async def dispatch(self, ws, method, params):
        identity = params.get('sessionId')
        if method == '_goose/unstable/health':
            return await self.health_probe.collect()
        if method == 'initialize':
            await asyncio.wait_for(self.rpc.ready.wait(), 15)
            config = (await self.rpc.request('config/read', {'includeLayers': False}))['config']
            provider = config.get('model_provider', '')
            self.backend = {'kind': 'codex', 'name': 'Codex Spark', 'agentName': 'Codex',
                            'executionHost': socket.gethostname(), 'modelId': config.get('model'),
                            'providerName': config.get('model_providers', {}).get(provider, {}).get('name', provider),
                            'defaultAccess': {'danger-full-access': 'full', 'workspace-write': 'workspace', 'read-only': 'readOnly'}.get(config.get('sandbox_mode')),
                            'approvalPolicy': config.get('approval_policy')}
            return {'protocolVersion': 1, 'agentInfo': {'name': 'Codex Spark', 'version': '1.0.0'}, 'authMethods': [],
                    'agentCapabilities': {'loadSession': True, 'promptCapabilities': {}, 'sessionCapabilities': {'list': {}, 'delete': {}},
                                          '_meta': {'goose': {'hostOwnedRuns': True, 'restartRecovery': True, 'submissionReceipts': True, 'workspaces': True,
                                                              'workspaceAssignment': False, 'newChatWorkspaces': True, 'workspaceBrowsing': True, 'workspaceCreation': True,
                                                              'draftSessionValidation': True,
                                                              'backend': self.backend, 'healthStatus': True, 'customNotifications': True}}}}
        if method == '_goose/unstable/workspaces':
            action = params.get('action', 'list')
            if action in ('browse', 'createFolder'):
                return await asyncio.to_thread(browse_folders, params)
            if action == 'add':
                path = str(Path(params['path']).expanduser().resolve(strict=True))
                if not Path(path).is_dir():
                    raise RPCError('Choose an existing directory on ai-research.')
                if path not in self.metadata['projects']:
                    self.metadata['projects'].append(path); self.save()
            elif action == 'assign':
                raise RPCError('Moving an existing Codex conversation to another folder is not supported. Start a new chat in that folder.')
            result = {'projectlessRoot': str(self.root), 'projects': [{'path': p, 'name': Path(p).name} for p in self.metadata['projects']]}
            if action == 'add':
                result['addedProject'] = {'path': path, 'name': Path(path).name}
            return result
        if method == '_goose/unstable/session/draft_states':
            return await self.draft_session_states(params.get('sessionIds'))
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
                workspace = {'kind': 'project', 'path': str(cwd)}
            response = await self.rpc.request('thread/start', {'cwd': str(cwd), 'approvalPolicy': 'never', 'sandbox': 'danger-full-access', 'historyMode': 'legacy'})
            thread = response['thread']; identity = thread['id']
            self.loaded.add(identity); self.clients[ws].add(identity)
            self.metadata['sessions'][identity] = {'workspace': workspace, 'cwd': str(cwd), 'fullAccess': True}
            self.remember_settings(response)
            if workspace['kind'] == 'project' and str(cwd) not in self.metadata['projects']:
                self.metadata['projects'].append(str(cwd))
            self.save()
            return {'sessionId': identity, '_meta': {'workingDir': str(cwd), 'workspace': workspace, 'goose': {'sessionSettings': self.session_settings(identity)}}}
        if method == 'session/load':
            self.loading.add((ws, identity))
            try:
                thread = await self.ensure_loaded(identity, include_turns=True)
                async with self.stream_locks.setdefault(identity, asyncio.Lock()):
                    self.clients[ws].add(identity)
                    turns = self.saved_history(identity, thread)
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
                    return {'_meta': {'hostRuns': [self.status(t) for t in turns], 'workspace': self.session(thread)['_meta']['workspace'],
                                      'goose': {'sessionSettings': self.session_settings(identity),
                                                'acceptedRequests': [r['id'] for r in self.journal.for_thread(identity) if r['phase'] in ('accepted', 'finished')]}}}
            finally:
                self.loading.discard((ws, identity))
        if method == 'session/prompt':
            if any(c.get('type') != 'text' for c in params.get('prompt', [])):
                raise RPCError('This model currently accepts text only.')
            text = '\n'.join(c['text'] for c in params.get('prompt', []))
            if not text.strip():
                raise RPCError('Enter a message.')
            request = params.get('_meta', {}).get('goose', {}).get('requestId') or str(uuid.uuid4())
            if not isinstance(request, str) or len(request) > 128:
                raise RPCError('Invalid submission ID.')
            return await self.start_prompt(ws, identity, text, request)
        if method == '_goose/unstable/session/submission':
            record = self.journal.submission(params['requestId'])
            if record and record['thread'] != identity:
                raise RPCError('Submission belongs to another conversation.')
            return {'state': record['phase'] if record else 'unknown'}
        if method == '_goose/unstable/session/resume_run':
            record = next((r for r in self.journal.turns(identity) if r['id'] == params['runId']), None)
            if not record or record['snapshot']['status'] != 'backendInterrupted':
                raise RPCError('This interrupted checkpoint is no longer available.')
            saved = record['snapshot']
            checkpoint = json.dumps(saved, ensure_ascii=False)
            goal = next((r['text'] for r in self.journal.for_thread(identity) if r['turn'] == saved['id'] or r['id'] == saved['id']), '')
            text = ('Review this interrupted task using read-only inspection. Do not repeat any previous command, apply patches, change files, restart services, or perform network mutations. '
                    'Report which actions are confirmed complete and which outcomes remain uncertain. Check actual file/service state where possible. '
                    'Then explain the next step and wait for the user to give a new instruction. This is a recovery review, not permission to re-execute work.\n'
                    + 'Original task: ' + goal + '\nSaved checkpoint: ' + checkpoint)
            self.journal.reviewed(saved['id'])
            return await self.start_prompt(ws, identity, text, 'review-' + saved['id'], review=True)
        if method == 'session/cancel':
            turn = self.turns.get(identity)
            if turn and turn['status'] == 'inProgress':
                self.journal.stopped(turn['id'])
                await self.rpc.request('turn/interrupt', {'threadId': identity, 'turnId': turn['id']})
            return {}
        if method == '_goose/unstable/session/rename':
            await self.rpc.request('thread/name/set', {'threadId': identity, 'name': params['title']})
            return {}
        if method in ('_goose/unstable/session/archive', '_goose/unstable/session/unarchive', 'session/delete'):
            if identity in self.waiters:
                raise RPCError('Stop the running task first.')
            action = method.rsplit('/', 1)[-1]
            try:
                await self.rpc.request('thread/' + action, {'threadId': identity})
            except RPCError as error:
                if action != 'delete':
                    raise
                await self.confirm_deleted_thread(identity, error)
            self.loaded.discard(identity)
            if action == 'delete':
                self.metadata['sessions'].pop(identity, None)
                self.turns.pop(identity, None)
                for subscriptions in self.clients.values():
                    subscriptions.discard(identity)
                with self.journal.db:
                    for table in ('turns', 'submissions', 'events', 'work_reviews'):
                        self.journal.db.execute(f'DELETE FROM {table} WHERE thread=?', (identity,))
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
        if not hmac.compare_digest(request.headers.get('X-Secret-Key', ''), self.token):
            raise web.HTTPUnauthorized()
        result = await self.health_probe.collect()
        return web.json_response(result, status=200 if result['status'] == 'ok' else 503, headers={'Cache-Control': 'no-store'})


def app(bridge):
    application = web.Application()
    application.router.add_get('/acp', bridge.websocket)
    application.router.add_get('/status', bridge.health)
    async def shutdown(application):
        await asyncio.gather(*(ws.close(code=1012, message=b'Adapter restarting; saved work continues') for ws in list(bridge.clients)), return_exceptions=True)
    application.on_shutdown.append(shutdown)
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
    parser.add_argument('--projectless-root', type=Path, default=Path.home() / 'Codex')
    args = parser.parse_args()
    os.umask(0o077)
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    home = Path.home()
    bridge = Bridge(home / '.local/state/codex-spark/mobile', home / '.config/codex-spark/mobile.token', home / '.config/codex-spark/server.token', projectless_root=args.projectless_root)
    bridge.health_probe.mobile_url = f'ws://127.0.0.1:{args.port}/acp'
    web.run_app(app(bridge), host='127.0.0.1', port=args.port, access_log=None)
