import asyncio
import base64
import json
from pathlib import Path
import tempfile
import unittest
import uuid
from unittest.mock import AsyncMock, patch

from goose_mobile import Bridge, RPCError, item_update, run_status, turn_failure
from mobile_state import merge_items


class Socket:
    closed = False

    def __init__(self):
        self.packets = []

    async def send_json(self, packet):
        self.packets.append(packet)


class MobileTests(unittest.IsolatedAsyncioTestCase):
    async def test_failure_cause_survives_live_rpc_replay_restart_and_duplicate_receipt(self):
        self.bridge.complete_review = AsyncMock()
        async def loaded(identity, include_turns=False):
            return {'id': identity, 'cwd': self.temp.name, 'updatedAt': 1, 'status': {'type': 'idle'}}
        self.bridge.ensure_loaded = loaded
        for index, (info, message, category) in enumerate([
            ('contextWindowExceeded', 'The context window is full.', 'context_limit'),
            ('other', 'Incomplete response returned, reason: max_output_tokens', 'output_limit'),
            ('rateLimitExceeded', 'Rate limit exceeded. Try again shortly.', 'rate_limit'),
            ({'httpConnectionFailed': {'httpStatusCode': 503}}, 'Provider is unavailable.', 'connection'),
        ]):
            with self.subTest(category=category):
                turn = {'id': f'failed-{index}', 'status': 'inProgress', 'items': [
                    {'id': f'user-{index}', 'type': 'userMessage', 'content': [{'type': 'text', 'text': 'Test'}]}]}
                calls = []
                async def request(method, params):
                    calls.append(method)
                    await self.bridge.event({'method': 'turn/started', 'params': {'threadId': 'thread-a', 'turn': turn}})
                    error = {'message': message, 'codexErrorInfo': info, 'additionalDetails': 'PRIVATE HOST DIAGNOSTICS'}
                    await self.bridge.event({'method': 'error', 'params': {'threadId': 'thread-a', 'turnId': turn['id'], 'error': error, 'willRetry': False}})
                    await self.bridge.event({'method': 'turn/completed', 'params': {'threadId': 'thread-a', 'turn': {**turn, 'status': 'failed', 'error': error}}})
                    return {'turn': turn}
                self.bridge.rpc.request = request
                params = {'sessionId': 'thread-a', 'prompt': [{'type': 'text', 'text': 'Test'}],
                          '_meta': {'goose': {'requestId': f'failed-request-{index}'}}}
                for _ in range(2):
                    await self.bridge.handle_packet(self.phone, {'id': index, 'method': 'session/prompt', 'params': params})
                    failure = self.phone.packets[-1]['error']
                    self.assertEqual(failure['message'], message)
                    self.assertEqual(failure['data']['error']['category'], category)
                self.assertEqual(calls, ['turn/start'])
                replay = await self.bridge.dispatch(self.phone, 'session/load', {'sessionId': 'thread-a'})
                status = replay['_meta']['hostRuns'][-1]
                self.assertEqual(status['error'], failure['data']['error'])
                self.assertEqual(status['userMessageId'], f'user-{index}')
                self.assertEqual(status['error']['outputTokenLimitReached'], category == 'output_limit')
                self.assertNotIn('PRIVATE HOST DIAGNOSTICS', json.dumps(self.phone.packets))
        restarted = Bridge(self.bridge.state, Path(self.temp.name) / 'token', Path(self.temp.name) / 'token', push_config=Path(self.temp.name) / 'no-push')
        self.addCleanup(restarted.journal.close)
        self.assertEqual(restarted.status(restarted.journal.turns('thread-a')[-1]['snapshot'])['error']['category'], 'connection')

    async def test_terminal_notification_is_fallback_only_and_retry_is_not_a_failure(self):
        turn = {'id': 'retry', 'status': 'inProgress', 'items': []}
        await self.bridge.event({'method': 'turn/started', 'params': {'threadId': 'thread-a', 'turn': turn}})
        for retry in (True, False):
            await self.bridge.event({'method': 'error', 'params': {'threadId': 'thread-a', 'turnId': 'retry',
                'error': {'message': 'The context window is full.', 'codexErrorInfo': 'contextWindowExceeded'}, 'willRetry': retry}})
            self.assertNotIn('error', run_status(self.bridge.turns['thread-a']))
            if retry:
                self.assertNotIn('_mobileError', self.bridge.turns['thread-a'])
        await self.bridge.event({'method': 'turn/completed', 'params': {'threadId': 'thread-a', 'turn': {**turn, 'status': 'failed', 'error': None}}})
        self.assertEqual(run_status(self.bridge.turns['thread-a'])['error']['category'], 'context_limit')
        for state in ('completed', 'interrupted', 'backendInterrupted'):
            self.assertNotIn('error', run_status({**self.bridge.turns['thread-a'], 'status': state}))

    def test_mobile_failure_is_bounded_and_redacted_without_private_diagnostics(self):
        failure = turn_failure({'status': 'failed', 'error': {'message': '\x1b[31mProvider failed: Authorization: Bearer private-token api_key=private-key https://user:password@example.com ' + '🙂' * 3000,
            'additionalDetails': 'PRIVATE DETAILS'}})
        self.assertLessEqual(len(failure['message'].encode()), 2048)
        self.assertTrue(failure['messageTruncated'])
        self.assertNotIn('private-token', failure['message'])
        self.assertNotIn('private-key', failure['message'])
        self.assertNotIn('user:password', failure['message'])
        self.assertNotIn('\x1b', failure['message'])
        self.assertNotIn('PRIVATE DETAILS', str(failure))

    async def test_native_cancellation_does_not_generate_a_failure_notification(self):
        await self.bridge.event({'method': 'turn/started', 'params': {'threadId': 'thread-a', 'turn': {'id': 'cancelled', 'status': 'inProgress', 'items': []}}})
        await self.bridge.event({'method': 'turn/completed', 'params': {'threadId': 'thread-a', 'turn': {'id': 'cancelled', 'status': 'interrupted', 'items': []}}})
        self.bridge.push.reconcile(self.bridge.journal.turns())
        self.assertEqual(self.bridge.journal.db.execute('SELECT count(*) FROM push_events').fetchone()[0], 0)
        self.assertEqual(self.bridge.turns['thread-a']['status'], 'backendInterrupted')

    async def test_notifications_observe_completion_failure_and_input_without_executing_work(self):
        with patch.object(self.bridge.push, 'config', return_value={'environment': 'sandbox', 'topic': 'computer.goose.mobile.dev'}):
            await self.bridge.dispatch(self.phone, '_goose/unstable/notifications/register', {
                'hostId': self.bridge.push.host_id, 'installationId': str(uuid.uuid4()), 'enabled': True, 'deviceToken': 'ab' * 32})
        for status in ('completed', 'failed'):
            turn = 'notification-' + status
            await self.bridge.event({'method': 'turn/started', 'params': {'threadId': 'thread-a', 'turn': {'id': turn, 'status': 'inProgress', 'items': []}}})
            await self.bridge.event({'id': 77, 'method': 'item/tool/requestUserInput', 'params': {
                'threadId': 'thread-a', 'turnId': turn, 'itemId': 'input-' + turn,
                'questions': [{'id': 'question', 'header': 'Question', 'question': 'Private question must not appear in APNs'}]}})
            await self.bridge.event({'method': 'turn/completed', 'params': {'threadId': 'thread-a', 'turn': {'id': turn, 'status': status, 'items': []}}})
            await self.bridge.event({'method': 'turn/completed', 'params': {'threadId': 'thread-a', 'turn': {'id': turn, 'status': status, 'items': []}}})
        rows = self.bridge.journal.db.execute('SELECT e.kind,d.state,e.payload FROM push_events e JOIN push_deliveries d ON e.id=d.event ORDER BY e.rowid').fetchall()
        self.assertEqual([(r['kind'], r['state']) for r in rows], [('input', 'obsolete'), ('completed', 'pending'), ('input', 'obsolete'), ('failed', 'pending')])
        self.assertTrue(all('Private question' not in row['payload'] for row in rows))
        self.assertEqual(self.bridge.rpc.ws.packets, [])

    async def test_notification_resolution_checks_host_and_event_and_only_reads_the_saved_chat(self):
        event = self.bridge.push.enqueue('thread-a', 'turn-a', 'completed')
        calls = []
        async def request(method, params):
            calls.append(method)
            self.assertEqual(method, 'thread/resume')
            return {'thread': {'id': 'thread-a', 'cwd': '/tmp/project', 'updatedAt': 1, 'name': 'Right chat'}}
        self.bridge.rpc.request = request
        args = {'hostId': self.bridge.push.host_id, 'sessionId': 'thread-a', 'eventId': event}
        for bad in ({'hostId': 'another-host'}, {'sessionId': 'another-chat'}, {'eventId': 'unknown-event'}):
            with self.assertRaises(RPCError):
                await self.bridge.dispatch(self.phone, '_goose/unstable/notifications/resolve', {**args, **bad})
        self.assertEqual(calls, [])
        result = await self.bridge.dispatch(self.phone, '_goose/unstable/notifications/resolve', args)
        self.assertEqual(result['session']['title'], 'Right chat')
        self.assertEqual(calls, ['thread/resume'])

    async def test_draft_states_distinguish_loaded_indexed_archived_and_missing_chats(self):
        self.seed_deletion_state()
        async def request(method, params):
            if method == 'thread/loaded/list':
                return {'data': ['loaded']}
            self.assertTrue(params['useStateDbOnly'])
            self.assertEqual(params['modelProviders'], [])
            self.assertIn('subAgent', params['sourceKinds'])
            if params['archived']:
                return {'data': [{'id': 'archived', 'cwd': '/tmp/archive', 'updatedAt': 1}]}
            if not params.get('cursor'):
                return {'data': [], 'nextCursor': 'later-page'}
            return {'data': [{'id': 'indexed', 'cwd': '/tmp/indexed', 'updatedAt': 2}]}
        self.bridge.rpc.request = request
        result = await self.bridge.dispatch(self.phone, '_goose/unstable/session/draft_states', {'sessionIds': ['thread-a', 'loaded', 'indexed', 'archived']})
        states = {entry['sessionId']: entry for entry in result['sessions']}
        self.assertEqual({key: value['state'] for key, value in states.items()}, {'thread-a': 'missing', 'loaded': 'available', 'indexed': 'available', 'archived': 'archived'})
        self.assertIn('archivedAt', states['archived']['session']['_meta'])
        self.assert_deletion_state(False)

    async def test_draft_state_failure_never_returns_partial_missing_results(self):
        self.seed_deletion_state()
        async def request(method, params):
            if method == 'thread/loaded/list':
                return {'data': []}
            if params['archived']:
                raise RPCError('Backend connection interrupted', -32000)
            return {'data': []}
        self.bridge.rpc.request = request
        with self.assertRaisesRegex(RPCError, 'connection interrupted'):
            await self.bridge.dispatch(self.phone, '_goose/unstable/session/draft_states', {'sessionIds': ['thread-a']})
        self.assert_deletion_state(False)

    async def test_draft_state_requests_are_bounded_and_empty_request_needs_no_backend(self):
        async def request(method, params):
            self.fail('Invalid or empty requests must not query the backend')
        self.bridge.rpc.request = request
        for identities in (None, 'one-id', [''], [None], ['x' * 129], ['id'] * 101):
            with self.assertRaises(RPCError):
                await self.bridge.draft_session_states(identities)
        self.assertEqual(await self.bridge.draft_session_states([]), {'sessions': []})

    def seed_deletion_state(self):
        for identity in ('thread-a', 'keep'):
            self.bridge.metadata['sessions'][identity] = {'cwd': '/tmp/' + identity}
            self.bridge.journal.prepare('request-' + identity, identity, 'saved draft')
            self.bridge.journal.save_turn(identity, {'id': 'turn-' + identity, 'status': 'completed', 'items': []})
            self.bridge.journal.db.execute('INSERT INTO events(thread,packet) VALUES(?,?)', (identity, '{}'))
            self.bridge.journal.prepare_review('request-' + identity, identity, {'root': '/tmp/' + identity})
            self.bridge.turns[identity] = {'id': 'turn-' + identity, 'status': 'completed', 'items': []}
            self.bridge.loaded.add(identity)
        self.bridge.save()

    def assert_deletion_state(self, deleted):
        for identity in ('thread-a', 'keep'):
            count = 0 if identity == 'thread-a' and deleted else 1
            self.assertEqual(identity in self.bridge.metadata['sessions'], bool(count))
            for table in ('turns', 'submissions', 'events', 'work_reviews'):
                self.assertEqual(self.bridge.journal.db.execute(f'SELECT count(*) FROM {table} WHERE thread=?', (identity,)).fetchone()[0], count)

    async def test_missing_chat_delete_is_idempotent_after_verifying_all_index_pages(self):
        self.seed_deletion_state()
        calls = []
        async def request(method, params):
            calls.append((method, params))
            if method == 'thread/delete':
                raise RPCError('no rollout found for thread id thread-a', -32600)
            if method == 'thread/loaded/list':
                return {'data': []}
            self.assertEqual(method, 'thread/list')
            self.assertTrue(params['useStateDbOnly'])
            self.assertIn('subAgent', params['sourceKinds'])
            if not params['archived'] and not params.get('cursor'):
                return {'data': [{'id': 'keep'}], 'nextCursor': 'next-page'}
            return {'data': []}
        self.bridge.rpc.request = request
        for _ in range(2):
            self.assertEqual(await self.bridge.dispatch(self.phone, 'session/delete', {'sessionId': 'thread-a'}), {})
        self.assert_deletion_state(True)
        self.assertNotIn('thread-a', self.bridge.loaded)
        self.assertNotIn('thread-a', self.bridge.turns)
        self.assertNotIn('thread-a', self.bridge.clients[self.phone])
        self.assertEqual(sum(method == 'thread/list' and params['archived'] for method, params in calls), 2)
        restarted = Bridge(self.bridge.state, Path(self.temp.name) / 'token', Path(self.temp.name) / 'token', push_config=Path(self.temp.name) / 'unconfigured-push.json')
        self.addCleanup(restarted.journal.close)
        self.assertNotIn('thread-a', restarted.metadata['sessions'])

    async def test_missing_rollout_does_not_delete_loaded_indexed_or_archived_work(self):
        self.seed_deletion_state()
        for location in ('loaded', 'active-first', 'active-next', 'archived'):
            with self.subTest(location=location):
                async def request(method, params):
                    if method == 'thread/delete':
                        raise RPCError('no rollout found for thread id thread-a', -32600)
                    if method == 'thread/loaded/list':
                        return {'data': ['thread-a'] if location == 'loaded' else []}
                    if (location == 'active-first' and not params['archived']) or (location == 'archived' and params['archived']) or (location == 'active-next' and params.get('cursor')):
                        return {'data': [{'id': 'thread-a'}]}
                    return {'data': [], 'nextCursor': 'page-two' if location == 'active-next' and not params['archived'] else None}
                self.bridge.rpc.request = request
                with self.assertRaisesRegex(RPCError, 'no rollout'):
                    await self.bridge.dispatch(self.phone, 'session/delete', {'sessionId': 'thread-a'})
                self.assert_deletion_state(False)

    async def test_delete_preserves_saved_work_on_unrelated_errors_and_unknown_confirmation(self):
        self.seed_deletion_state()
        for message, code in [('Permission denied', -32603), ('no rollout found for thread id different-chat', -32600), ('no rollout found for thread id thread-a', -32000)]:
            with self.subTest(message=message, code=code):
                async def request(method, params):
                    self.assertEqual(method, 'thread/delete')
                    raise RPCError(message, code)
                self.bridge.rpc.request = request
                with self.assertRaises(RPCError):
                    await self.bridge.dispatch(self.phone, 'session/delete', {'sessionId': 'thread-a'})
                self.assert_deletion_state(False)
        async def disconnected(method, params):
            raise RPCError('thread not found: thread-a' if method == 'thread/delete' else 'Connection interrupted', -32600 if method == 'thread/delete' else -32000)
        self.bridge.rpc.request = disconnected
        with self.assertRaisesRegex(RPCError, 'Connection interrupted'):
            await self.bridge.dispatch(self.phone, 'session/delete', {'sessionId': 'thread-a'})
        self.assert_deletion_state(False)

    async def test_delete_keeps_saved_work_when_index_cursor_repeats(self):
        self.seed_deletion_state()
        async def request(method, params):
            if method == 'thread/delete':
                raise RPCError('thread not found: thread-a', -32600)
            return {'data': []} if method == 'thread/loaded/list' else {'data': [], 'nextCursor': 'same'}
        self.bridge.rpc.request = request
        with self.assertRaisesRegex(RPCError, 'Could not verify'):
            await self.bridge.dispatch(self.phone, 'session/delete', {'sessionId': 'thread-a'})
        self.assert_deletion_state(False)

    async def test_normal_delete_cleans_all_recovery_data_but_archive_does_not(self):
        self.seed_deletion_state()
        async def request(method, params):
            if method == 'thread/archive':
                raise RPCError('no rollout found for thread id thread-a', -32600)
            self.assertEqual(method, 'thread/delete')
            return {}
        self.bridge.rpc.request = request
        with self.assertRaises(RPCError):
            await self.bridge.dispatch(self.phone, '_goose/unstable/session/archive', {'sessionId': 'thread-a'})
        self.assert_deletion_state(False)
        await self.bridge.dispatch(self.phone, 'session/delete', {'sessionId': 'thread-a'})
        self.assert_deletion_state(True)

    async def test_running_chat_cannot_be_deleted(self):
        self.seed_deletion_state()
        self.bridge.waiters['thread-a'] = asyncio.get_running_loop().create_future()
        async def request(method, params):
            self.fail('Must not contact the backend while this chat is running')
        self.bridge.rpc.request = request
        with self.assertRaisesRegex(RPCError, 'Stop the running task'):
            await self.bridge.dispatch(self.phone, 'session/delete', {'sessionId': 'thread-a'})
        self.assert_deletion_state(False)

    async def test_remote_folder_browser_lists_only_directories_and_resolves_navigation(self):
        root = Path(self.temp.name).resolve()
        (root / 'Zulu').mkdir()
        (root / 'alpha folder').mkdir()
        (root / '.hidden').mkdir()
        (root / 'file.txt').write_text('not a folder')
        (root / 'alias').symlink_to(root / 'alpha folder', target_is_directory=True)
        listed = await self.bridge.dispatch(self.phone, '_goose/unstable/workspaces', {'action': 'browse', 'path': str(root)})
        self.assertEqual([p['name'] for p in listed['directories']], ['alias', 'alpha folder', 'state', 'Zulu'])
        self.assertEqual(listed['parent'], str(root.parent))
        self.assertEqual(listed['home'], str(Path.home().resolve()))
        opened = await self.bridge.dispatch(self.phone, '_goose/unstable/workspaces', {'action': 'browse', 'path': str(root / 'alias')})
        self.assertEqual(opened['path'], str(root / 'alpha folder'))
        hidden = await self.bridge.dispatch(self.phone, '_goose/unstable/workspaces', {'action': 'browse', 'path': str(root), 'showHidden': True})
        self.assertIn('.hidden', [p['name'] for p in hidden['directories']])
        self.assertEqual(self.bridge.metadata['projects'], [])

    async def test_remote_folder_browser_home_root_pages_and_errors(self):
        root = Path(self.temp.name).resolve()
        for index in range(205):
            (root / f'folder-{index:03}').mkdir()
        first = await self.bridge.dispatch(self.phone, '_goose/unstable/workspaces', {'action': 'browse', 'path': str(root)})
        second = await self.bridge.dispatch(self.phone, '_goose/unstable/workspaces', {'action': 'browse', 'path': str(root), 'offset': first['nextOffset']})
        self.assertEqual(len(first['directories']), 200)
        self.assertEqual(len(second['directories']), 6)
        self.assertIsNone(second['nextOffset'])
        with patch('goose_mobile.Path.home', return_value=root):
            home = await self.bridge.dispatch(self.phone, '_goose/unstable/workspaces', {'action': 'browse'})
        self.assertEqual(home['path'], str(root))
        filesystem_root = await self.bridge.dispatch(self.phone, '_goose/unstable/workspaces', {'action': 'browse', 'path': '/'})
        self.assertIsNone(filesystem_root['parent'])
        for path in ('relative', str(root / 'missing'), str(root / 'token')):
            with self.assertRaises(RPCError):
                await self.bridge.dispatch(self.phone, '_goose/unstable/workspaces', {'action': 'browse', 'path': path})
        with patch('goose_mobile.os.scandir', side_effect=PermissionError('Permission denied')):
            with self.assertRaisesRegex(RPCError, 'Permission denied'):
                await self.bridge.dispatch(self.phone, '_goose/unstable/workspaces', {'action': 'browse', 'path': str(root)})

    async def test_remote_folder_creation_is_explicit_and_cannot_escape_or_overwrite(self):
        root = Path(self.temp.name).resolve()
        async def create(name):
            return await self.bridge.dispatch(self.phone, '_goose/unstable/workspaces', {'action': 'createFolder', 'path': str(root), 'name': name})
        for name in ('', ' ', '.', '..', '../escape', '/absolute', 'nested/folder', ' padded ', 'null\x00name', None):
            with self.assertRaises(RPCError):
                await create(name)
        created = await create('New folder 🌱')
        self.assertEqual(created['path'], str(root / 'New folder 🌱'))
        self.assertTrue(Path(created['path']).is_dir())
        sentinel = Path(created['path']) / 'keep.txt'
        sentinel.write_text('preserved')
        with self.assertRaisesRegex(RPCError, 'already exists'):
            await create('New folder 🌱')
        with self.assertRaisesRegex(RPCError, 'already exists'):
            await create('token')
        self.assertEqual(sentinel.read_text(), 'preserved')
        self.assertEqual(self.bridge.metadata['projects'], [])

    async def test_projectless_chats_use_separate_folders_in_configured_codex_root(self):
        root = Path(self.temp.name).resolve() / 'Codex'
        token = Path(self.temp.name) / 'token'
        bridge = Bridge(Path(self.temp.name) / 'new-state', token, token, projectless_root=root, push_config=Path(self.temp.name) / 'unconfigured-push.json')
        self.addCleanup(bridge.journal.close)
        bridge.clients[self.phone] = set()
        calls = []
        async def request(method, params):
            self.assertEqual(method, 'thread/start')
            calls.append(params)
            return {'thread': {'id': 'new-' + str(len(calls))}, 'model': 'test-model'}
        bridge.rpc.request = request
        results = [await bridge.dispatch(self.phone, 'session/new', {'_meta': {'workspace': {'kind': 'projectless'}}}) for _ in range(2)]
        directories = [Path(result['_meta']['workingDir']) for result in results]
        self.assertEqual(len(set(directories)), 2)
        for directory in directories:
            self.assertEqual(directory.parent, root)
            self.assertTrue(directory.is_dir())
        listed = await bridge.dispatch(self.phone, '_goose/unstable/workspaces', {})
        self.assertEqual(listed['projectlessRoot'], str(root))
        self.assertEqual(listed['projects'], [])
        self.assertTrue(all(call['sandbox'] == 'danger-full-access' for call in calls))

    async def test_project_selection_and_added_folder_return_canonical_host_paths(self):
        project = Path(self.temp.name).resolve() / 'project with spaces'
        project.mkdir()
        alias = str(project / '..' / project.name)
        added = await self.bridge.dispatch(self.phone, '_goose/unstable/workspaces', {'action': 'add', 'path': alias})
        self.assertEqual(added['addedProject'], {'path': str(project), 'name': project.name})
        async def request(method, params):
            self.assertEqual(params['cwd'], str(project))
            return {'thread': {'id': 'selected-project'}, 'model': 'test-model'}
        self.bridge.rpc.request = request
        result = await self.bridge.dispatch(self.phone, 'session/new', {'_meta': {'workspace': {'kind': 'project', 'path': alias}}})
        self.assertEqual(result['_meta']['workspace'], {'kind': 'project', 'path': str(project)})
        self.assertEqual(result['_meta']['workingDir'], str(project))

    async def test_initialize_reports_backend_without_exposing_provider_credentials(self):
        self.bridge.rpc.ready.set()
        async def request(method, params):
            if method == 'model/list':
                return {'data': [{'model': 'provider/model-a', 'inputModalities': ['text', 'image']}], 'nextCursor': None}
            self.assertEqual(method, 'config/read')
            return {'config': {'model': 'provider/model-a', 'model_provider': 'spark',
                               'model_providers': {'spark': {'name': 'DGX Spark', 'api_key': 'private-key'}},
                               'sandbox_mode': 'danger-full-access', 'approval_policy': 'never'}}
        self.bridge.rpc.request = request
        result = await self.bridge.dispatch(self.phone, 'initialize', {})
        meta = result['agentCapabilities']['_meta']['goose']
        self.assertEqual(meta['backend']['modelId'], 'provider/model-a')
        self.assertEqual(meta['backend']['inputModalities'], ['text', 'image'])
        self.assertEqual(result['agentCapabilities']['promptCapabilities'], {'image': True})
        self.assertEqual(meta['backend']['defaultAccess'], 'full')
        self.assertEqual(meta['backend']['providerName'], 'DGX Spark')
        self.assertFalse(meta['workspaceAssignment'])
        self.assertTrue(meta['workspaceBrowsing'])
        self.assertTrue(meta['workspaceCreation'])
        self.assertTrue(meta['draftSessionValidation'])
        self.assertNotIn('private-key', str(result))
        self.assertNotIn('api_key', str(result))

    async def test_capabilities_follow_selected_model_across_catalog_pages(self):
        self.bridge.rpc.ready.set()
        async def request(method, params):
            if method == 'config/read':
                return {'config': {'model': 'text-model'}}
            self.assertEqual(method, 'model/list')
            if not params.get('cursor'):
                return {'data': [{'model': 'text-model', 'inputModalities': ['text']}], 'nextCursor': 'page2'}
            return {'data': [{'model': 'vision-model', 'inputModalities': ['text', 'image']}], 'nextCursor': None}
        self.bridge.rpc.request = request
        result = await self.bridge.dispatch(self.phone, 'initialize', {})
        self.assertEqual(result['agentCapabilities']['promptCapabilities'], {})
        self.assertEqual(self.bridge.backend['inputModalities'], ['text'])
        self.assertEqual(self.bridge.model_modalities['vision-model'], ['text', 'image'])
        with self.assertRaisesRegex(RPCError, 'not enabled'):
            await self.bridge.dispatch(self.phone, 'session/prompt', {'sessionId': 'thread-a',
                'prompt': [{'type': 'image', 'mimeType': 'image/png', 'data': 'aW1hZ2U='}]})

    async def test_image_receipt_dispatches_once_and_replays_after_disconnect(self):
        image = {'type': 'image', 'mimeType': 'image/png', 'data': base64.b64encode(b'image one').decode()}
        self.bridge.backend = {'modelId': 'vision'}
        self.bridge.model_modalities = {'vision': ['text', 'image']}
        self.bridge.titles.schedule = lambda *args: None
        calls = []
        started = asyncio.Event()
        async def loaded(identity, include_turns=False):
            return {'id': identity, 'cwd': '/tmp', 'updatedAt': 1, 'status': {'type': 'idle'}, 'turns': self.bridge.saved_history(identity, {})}
        async def request(method, params):
            calls.append((method, params))
            turn = {'id': 'image-turn', 'status': 'inProgress', 'items': [
                {'id': 'user-image', 'clientId': 'image-request', 'type': 'userMessage', 'content': params['input']}]}
            await self.bridge.event({'method': 'turn/started', 'params': {'threadId': 'thread-a', 'turn': turn}})
            started.set()
            return {'turn': turn}
        self.bridge.ensure_loaded = loaded
        self.bridge.rpc.request = request
        params = {'sessionId': 'thread-a', 'prompt': [image], '_meta': {'goose': {'requestId': 'image-request'}}}
        first = asyncio.create_task(self.bridge.dispatch(self.phone, 'session/prompt', params))
        await started.wait()
        second = asyncio.create_task(self.bridge.dispatch(self.phone, 'session/prompt', params))
        await asyncio.sleep(0)
        await self.bridge.event({'method': 'turn/completed', 'params': {'threadId': 'thread-a', 'turn': {'id': 'image-turn', 'status': 'completed', 'items': []}}})
        self.assertEqual((await first)['stopReason'], 'end_turn')
        self.assertEqual((await second)['stopReason'], 'end_turn')
        self.assertEqual((await self.bridge.dispatch(self.phone, 'session/prompt', params))['stopReason'], 'end_turn')
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][1]['input'][0]['type'], 'image')
        self.assertEqual(calls[0][1]['input'][0]['detail'], 'auto')
        changed = {**params, 'prompt': [{**image, 'data': base64.b64encode(b'image two').decode()}]}
        with self.assertRaisesRegex(RPCError, 'different message'):
            await self.bridge.dispatch(self.phone, 'session/prompt', changed)
        self.phone.packets.clear()
        await self.bridge.dispatch(self.phone, 'session/load', {'sessionId': 'thread-a'})
        images = [p['params']['update']['content'] for p in self.phone.packets if p.get('params', {}).get('update', {}).get('content', {}).get('type') == 'image']
        self.assertEqual(images, [image])

    async def test_uncertain_image_submission_preserves_attachment_without_retransmission(self):
        native = [{'type': 'image', 'url': 'data:image/png;base64,aW1hZ2U=', 'detail': 'auto'}]
        self.bridge.journal.prepare('uncertain-image', 'thread-a', '[Image]', native)
        self.bridge.journal.phase('uncertain-image', 'dispatching')
        calls = []
        async def request(method, params):
            calls.append(method)
            self.assertEqual(method, 'thread/resume')
            return {'thread': {'id': 'thread-a', 'turns': [], 'status': {'type': 'idle'}}}
        self.bridge.rpc.call = request
        await self.bridge.reconcile()
        self.assertEqual(calls, ['thread/resume'])
        self.assertEqual(self.bridge.journal.submission('uncertain-image')['phase'], 'unconfirmed')
        saved = self.bridge.journal.turns('thread-a')[0]['snapshot']['items'][0]['content']
        self.assertEqual(saved, native)

    def test_existing_chat_does_not_inherit_new_full_access_default(self):
        self.bridge.backend = {'defaultAccess': 'full', 'modelId': 'host-default'}
        self.assertEqual(self.bridge.session_settings('thread-a')['access'], 'workspace')
        self.assertIsNone(self.bridge.session_settings('thread-a')['modelId'])
        self.bridge.remember_settings({'thread': {'id': 'thread-a'}, 'model': 'existing-model', 'sandbox': {'type': 'workspaceWrite'}})
        self.assertEqual(self.bridge.session_settings('thread-a')['modelId'], 'existing-model')
        self.assertEqual(self.bridge.session_settings('thread-a')['access'], 'workspace')

    def test_hydrated_user_message_keeps_its_original_stream_identity(self):
        native = [{'type': 'userMessage', 'id': 'item-1', 'clientId': 'same-request', 'content': []}]
        saved = [{'type': 'userMessage', 'id': 'stream-id', 'clientId': 'same-request', 'content': []}]
        self.assertEqual(merge_items(native, saved), saved)

    def setUp(self):
        review = patch('goose_mobile.capture_review', return_value={'unavailable': True})
        review.start()
        self.addCleanup(review.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        token = root / 'token'; token.write_text('test-only-token')
        self.bridge = Bridge(root / 'state', token, token, push_config=root / 'unconfigured-push.json')
        self.addCleanup(self.bridge.journal.close)
        self.bridge.rpc.epoch = 'test'
        self.bridge.rpc.ws = Socket()
        self.phone = Socket()
        self.bridge.clients[self.phone] = {'thread-a'}

    async def test_pending_approval_survives_phone_disconnect_and_requires_explicit_allow(self):
        self.bridge.clients.clear()
        request = {'id': 17, 'method': 'item/commandExecution/requestApproval', 'params': {'threadId': 'thread-a', 'turnId': 'turn-a', 'itemId': 'item-a', 'command': 'example requiring approval'}}
        await self.bridge.event(request)
        self.assertEqual(self.bridge.rpc.ws.packets, [])
        self.bridge.clients[self.phone] = {'thread-a'}
        await self.bridge.deliver_permission('codex-test-17', only=self.phone)
        self.assertEqual(self.phone.packets[-1]['method'], 'session/request_permission')
        await self.bridge.answer(self.phone, {'id': 'codex-test-17', 'result': {'outcome': {'outcome': 'cancelled'}}})
        self.assertEqual(self.bridge.rpc.ws.packets[-1], {'id': 17, 'result': {'decision': 'decline'}})

    async def test_other_session_cannot_answer_permission_or_answer_twice(self):
        request = {'id': 18, 'method': 'item/fileChange/requestApproval', 'params': {'threadId': 'thread-a', 'turnId': 'turn-a', 'itemId': 'edit-a'}}
        await self.bridge.event(request)
        wrong_phone = Socket(); self.bridge.clients[wrong_phone] = {'thread-b'}
        answer = {'id': 'codex-test-18', 'result': {'outcome': {'outcome': 'selected', 'optionId': 'accept'}}}
        await self.bridge.answer(wrong_phone, answer)
        self.assertEqual(self.bridge.rpc.ws.packets, [])
        await self.bridge.answer(self.phone, answer)
        await self.bridge.answer(self.phone, answer)
        self.assertEqual(self.bridge.rpc.ws.packets, [{'id': 18, 'result': {'decision': 'accept'}}])

    async def test_user_question_translates_to_phone_form(self):
        await self.bridge.event({'id': 19, 'method': 'item/tool/requestUserInput', 'params': {'threadId': 'thread-a', 'turnId': 'turn-a', 'questions': [{'id': 'choice', 'header': 'Choice', 'question': 'Which folder?'}]}})
        self.assertEqual(self.phone.packets[-1]['method'], 'elicitation/create')
        await self.bridge.answer(self.phone, {'id': 'codex-test-19', 'result': {'action': 'accept', 'content': {'choice': '/tmp/project'}}})
        self.assertEqual(self.bridge.rpc.ws.packets[-1]['result'], {'answers': {'choice': {'answers': ['/tmp/project']}}})

    async def test_duplicate_prompt_does_not_start_a_second_turn(self):
        async def loaded(identity, include_turns=False):
            return {'status': {'type': 'idle'}}
        self.bridge.ensure_loaded = loaded
        self.bridge.turns['thread-a'] = {'id': 'turn-a', 'status': 'inProgress', 'items': []}
        with self.assertRaisesRegex(RPCError, 'already has a running task'):
            await self.bridge.dispatch(self.phone, 'session/prompt', {'sessionId': 'thread-a', 'prompt': [{'type': 'text', 'text': 'do it twice'}]})
        self.assertEqual(self.bridge.rpc.ws.packets, [])

    async def test_completed_turn_uses_exact_final_item_and_resolves_waiter(self):
        turn = {'id': 'turn-a', 'status': 'inProgress', 'items': []}
        await self.bridge.event({'method': 'turn/started', 'params': {'threadId': 'thread-a', 'turn': turn}})
        for identity, text in [('progress', 'Working'), ('final', 'Finished')]:
            await self.bridge.event({'method': 'item/agentMessage/delta', 'params': {'threadId': 'thread-a', 'itemId': identity, 'delta': text}})
        future = asyncio.get_running_loop().create_future(); self.bridge.waiters['thread-a'] = future
        await self.bridge.event({'method': 'turn/completed', 'params': {'threadId': 'thread-a', 'turn': {'id': 'turn-a', 'status': 'completed', 'items': []}}})
        self.assertEqual((await future)['status'], 'completed')
        status = self.phone.packets[-1]['params']['update']['_meta']['goose']['hostRun']
        self.assertEqual(status['finalMessageId'], 'final')
        self.assertEqual(status['state'], 'finished')
        self.assertEqual(item_update({'type': 'commandExecution', 'id': 'cmd', 'command': 'test', 'status': 'failed'})['status'], 'failed')

    async def test_completion_summary_does_not_drop_durable_tool_activity(self):
        await self.bridge.event({'method': 'turn/started', 'params': {'threadId': 'thread-a', 'turn': {'id': 'turn-a', 'status': 'inProgress', 'items': []}}})
        await self.bridge.event({'method': 'item/completed', 'params': {'threadId': 'thread-a', 'turnId': 'turn-a', 'item': {'type': 'commandExecution', 'id': 'tool', 'command': 'append once', 'status': 'completed'}}})
        await self.bridge.event({'method': 'turn/completed', 'params': {'threadId': 'thread-a', 'turn': {'id': 'turn-a', 'status': 'completed', 'items': [{'type': 'agentMessage', 'id': 'answer', 'text': 'Done'}]}}})
        saved = self.bridge.journal.turns('thread-a')[0]['snapshot']
        self.assertEqual([i['id'] for i in saved['items']], ['tool', 'answer'])

    async def test_plan_updates_replace_steps_and_replay_after_restart(self):
        user = {'type': 'userMessage', 'id': 'user', 'content': [{'type': 'text', 'text': 'Work'}]}
        await self.bridge.event({'method': 'turn/started', 'params': {'threadId': 'thread-a', 'turn': {'id': 'plan-turn', 'status': 'inProgress', 'items': [user]}}})
        for status in ('inProgress', 'completed'):
            await self.bridge.event({'method': 'turn/plan/updated', 'params': {'threadId': 'thread-a', 'turnId': 'plan-turn',
                'explanation': 'Checking the result', 'plan': [{'step': 'Inspect', 'status': status}]}})
        await self.bridge.event({'method': 'turn/completed', 'params': {'threadId': 'thread-a', 'turn': {'id': 'plan-turn', 'status': 'completed',
            'items': [{'id': 'answer', 'type': 'agentMessage', 'text': 'Done'}]}}})
        live = [p['params']['update'] for p in self.phone.packets if p['params']['update']['sessionUpdate'] == 'plan']
        self.assertEqual([u['entries'][0]['status'] for u in live], ['in_progress', 'completed'])
        self.assertEqual(live[-1]['_meta']['goose'], {'turnId': 'plan-turn', 'explanation': 'Checking the result'})
        token = Path(self.temp.name) / 'token'
        restored = Bridge(self.bridge.state, token, token, push_config=Path(self.temp.name) / 'no-push.json')
        self.addCleanup(restored.journal.close)
        restored.clients[self.phone] = set()
        async def loaded(identity, include_turns=False):
            return {'id': identity, 'cwd': '/tmp', 'updatedAt': 1, 'turns': []}
        restored.ensure_loaded = loaded
        self.phone.packets.clear()
        await restored.dispatch(self.phone, 'session/load', {'sessionId': 'thread-a'})
        updates = [p['params']['update'] for p in self.phone.packets]
        self.assertEqual([u['sessionUpdate'] for u in updates[:3]], ['user_message_chunk', 'plan', 'agent_message_chunk'])
        self.assertEqual(updates[1], live[-1])

    async def test_plan_reconnect_observes_latest_update_without_duplicates(self):
        async def loaded(identity, include_turns=False):
            for turn_id, plan in [('old', [{'step': 'Earlier', 'status': 'completed'}]), ('new', [])]:
                await self.bridge.event({'method': 'turn/plan/updated', 'params': {'threadId': identity, 'turnId': turn_id, 'plan': plan}})
            return {'id': identity, 'cwd': '/tmp', 'updatedAt': 1, 'turns': []}
        self.bridge.ensure_loaded = loaded
        await self.bridge.dispatch(self.phone, 'session/load', {'sessionId': 'thread-a'})
        plans = [p['params']['update'] for p in self.phone.packets if p['params']['update']['sessionUpdate'] == 'plan']
        self.assertEqual([p['_meta']['goose']['turnId'] for p in plans], ['old', 'new'])
        self.assertEqual(plans[-1]['entries'], [])

    async def test_native_reconcile_preserves_saved_plan(self):
        saved = {'id': 'plan-turn', 'status': 'inProgress', 'items': [],
                 'plan': [{'step': 'Inspect', 'status': 'inProgress'}], 'planExplanation': 'Checking'}
        self.bridge.journal.save_turn('thread-a', saved)
        async def request(method, params):
            return {'thread': {'id': 'thread-a', 'status': {'type': 'idle'},
                               'turns': [{'id': 'plan-turn', 'status': 'completed', 'items': []}]}}
        self.bridge.rpc.call = request
        await self.bridge.reconcile()
        turn = self.bridge.journal.turns('thread-a')[0]['snapshot']
        self.assertEqual(turn['status'], 'completed')
        self.assertEqual(turn['plan'], saved['plan'])
        self.assertEqual(turn['planExplanation'], 'Checking')

    def test_tool_descriptors_keep_details_out_of_stream_title(self):
        for kind, descriptor in [('commandExecution', 'Used a tool'), ('fileChange', 'Edited a file'),
                                 ('webSearch', 'Searched the web'), ('mcpToolCall', 'Used a tool')]:
            item = {'id': 'tool', 'type': kind, 'command': 'printf private-command', 'status': 'inProgress',
                    'aggregatedOutput': 'private-output'}
            started = item_update(item, False)
            self.assertEqual(started['title'], descriptor)
            self.assertEqual(started['status'], 'in_progress')
            self.assertEqual(started['_meta']['goose']['toolDetail'], 'printf private-command')
            finished = item_update({**item, 'status': 'completed', 'exitCode': 1})
            self.assertEqual(finished['title'], descriptor)
            self.assertEqual(finished['toolCallId'], started['toolCallId'])
            self.assertEqual(finished['status'], 'failed')
            if kind == 'commandExecution':
                self.assertEqual(finished['_meta']['goose']['commandResult']['output'], 'private-output')

    async def test_reconnect_does_not_duplicate_deltas_during_history_read(self):
        self.bridge.turns['thread-a'] = {'id': 'turn-a', 'status': 'inProgress', 'items': [{'id': 'answer', 'type': 'agentMessage', 'text': 'Before'}]}
        self.bridge.journal.save_turn('thread-a', self.bridge.turns['thread-a'])
        async def loaded(identity, include_turns=False):
            await self.bridge.event({'method': 'item/agentMessage/delta', 'params': {'threadId': identity, 'itemId': 'answer', 'delta': ' during'}})
            return {'id': identity, 'cwd': '/tmp', 'updatedAt': 1, 'turns': []}
        self.bridge.ensure_loaded = loaded
        await self.bridge.dispatch(self.phone, 'session/load', {'sessionId': 'thread-a'})
        chunks = [p['params']['update']['content']['text'] for p in self.phone.packets if p.get('params', {}).get('update', {}).get('sessionUpdate') == 'agent_message_chunk']
        self.assertEqual(chunks, ['Before during'])

    async def test_same_submission_waits_for_one_turn_and_completed_retry_never_executes(self):
        started = asyncio.Event(); calls = []
        async def loaded(identity, include_turns=False):
            return {'cwd': '/tmp', 'status': {'type': 'idle'}}
        async def request(method, params):
            if method == 'thread/read':
                return {'thread': {'name': 'Existing title'}}
            calls.append((method, params))
            turn = {'id': 'once', 'status': 'inProgress', 'items': []}
            await self.bridge.event({'method': 'turn/started', 'params': {'threadId': 'thread-a', 'turn': turn}})
            started.set()
            return {'turn': turn}
        self.bridge.ensure_loaded = loaded; self.bridge.rpc.request = request
        first = asyncio.create_task(self.bridge.start_prompt(self.phone, 'thread-a', 'write once', 'request-a'))
        await started.wait()
        second = asyncio.create_task(self.bridge.start_prompt(self.phone, 'thread-a', 'write once', 'request-a'))
        await asyncio.sleep(0)
        await self.bridge.event({'method': 'turn/completed', 'params': {'threadId': 'thread-a', 'turn': {'id': 'once', 'status': 'completed', 'items': []}}})
        self.assertEqual((await first)['stopReason'], 'end_turn')
        self.assertEqual((await second)['stopReason'], 'end_turn')
        self.assertEqual((await self.bridge.start_prompt(self.phone, 'thread-a', 'write once', 'request-a'))['stopReason'], 'end_turn')
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][1]['clientUserMessageId'], 'request-a')
        self.assertEqual(calls[0][1]['sandboxPolicy']['type'], 'workspaceWrite')
        self.assertEqual(calls[0][1]['sandboxPolicy']['writableRoots'], ['/tmp'])
        self.assertTrue(calls[0][1]['sandboxPolicy']['networkAccess'])
        with self.assertRaises(RPCError):
            await self.bridge.start_prompt(self.phone, 'thread-a', 'different action', 'request-a')

    async def test_completion_during_duplicate_receipt_returns_saved_result_without_dispatch(self):
        self.bridge.journal.prepare('request-a', 'thread-a', 'write once')
        self.bridge.journal.phase('request-a', 'accepted', 'turn-a')
        turn = {'id': 'turn-a', 'status': 'completed', 'items': []}
        self.bridge.journal.save_turn('thread-a', turn)
        async def acknowledge(identity, request):
            self.bridge.journal.phase(request, 'finished', 'turn-a')
            self.bridge.turns[identity] = turn
        self.bridge.acknowledge = acknowledge
        result = await self.bridge.start_prompt(self.phone, 'thread-a', 'write once', 'request-a')
        self.assertEqual(result['stopReason'], 'end_turn')
        self.assertEqual(self.bridge.rpc.ws.packets, [])

    async def test_crash_after_dispatch_preserves_message_and_never_retransmits(self):
        self.bridge.journal.prepare('uncertain', 'thread-a', 'append a record')
        self.bridge.journal.phase('uncertain', 'dispatching')
        restored = Bridge(self.bridge.state, Path(self.temp.name) / 'token', Path(self.temp.name) / 'token', push_config=Path(self.temp.name) / 'unconfigured-push.json')
        self.addCleanup(restored.journal.close); restored.clients[self.phone] = {'thread-a'}
        with self.assertRaisesRegex(RPCError, 'will not be sent or executed again'):
            await restored.start_prompt(self.phone, 'thread-a', 'append a record', 'uncertain')
        self.assertEqual(restored.journal.submission('uncertain')['text'], 'append a record')

    async def test_new_chat_keeps_full_access_after_adapter_restart(self):
        async def create(method, params):
            self.assertEqual(method, 'thread/start')
            self.assertEqual(params['sandbox'], 'danger-full-access')
            self.assertEqual(params['approvalPolicy'], 'never')
            return {'thread': {'id': 'full-access', 'cwd': self.temp.name}}
        self.bridge.rpc.request = create
        result = await self.bridge.dispatch(self.phone, 'session/new', {'cwd': self.temp.name})
        identity = result['sessionId']
        restored = Bridge(self.bridge.state, Path(self.temp.name) / 'token', Path(self.temp.name) / 'token', push_config=Path(self.temp.name) / 'unconfigured-push.json')
        self.addCleanup(restored.journal.close); restored.clients[self.phone] = {identity}
        async def loaded(thread, include_turns=False):
            return {'cwd': self.temp.name, 'status': {'type': 'idle'}}
        async def request(method, params):
            self.assertEqual(params['sandboxPolicy'], {'type': 'dangerFullAccess'})
            self.assertEqual(params['approvalPolicy'], 'never')
            turn = {'id': 'full-turn', 'status': 'completed', 'items': []}
            await restored.event({'method': 'turn/started', 'params': {'threadId': identity, 'turn': {**turn, 'status': 'inProgress'}}})
            await restored.event({'method': 'turn/completed', 'params': {'threadId': identity, 'turn': turn}})
            return {'turn': turn}
        restored.ensure_loaded = loaded; restored.rpc.request = request
        self.assertEqual((await restored.start_prompt(self.phone, identity, 'inspect', 'full-request'))['stopReason'], 'end_turn')

    async def test_backend_restart_preserves_partial_output_and_marks_uncertain_action(self):
        turn = {'id': 'crashed', 'status': 'inProgress', 'items': [
            {'type': 'agentMessage', 'id': 'partial', 'text': 'Saved partial output'},
            {'type': 'commandExecution', 'id': 'action', 'command': 'append marker', 'status': 'inProgress'}]}
        self.bridge.journal.save_turn('thread-a', turn)
        calls = []
        async def call(method, params):
            calls.append(method)
            return {'thread': {'id': 'thread-a', 'status': {'type': 'idle'}, 'turns': []}}
        self.bridge.rpc.call = call
        await self.bridge.reconcile()
        saved = self.bridge.journal.turns('thread-a')[0]['snapshot']
        self.assertEqual(saved['items'][0]['text'], 'Saved partial output')
        status = self.bridge.status(saved)
        self.assertEqual(status['state'], 'interrupted')
        self.assertEqual(status['recovery']['uncertainTools'], 1)
        self.assertTrue(status['recovery']['readOnlyReview'])
        self.assertEqual(calls, ['thread/resume'])

    async def test_live_task_rejoins_without_submitting_or_cancelling_work(self):
        turn = {'id': 'live', 'status': 'inProgress', 'items': [{'type': 'agentMessage', 'id': 'partial', 'text': 'Recent output'}]}
        self.bridge.journal.save_turn('thread-a', turn)
        async def call(method, params):
            self.assertEqual(method, 'thread/resume')
            return {'thread': {'id': 'thread-a', 'status': {'type': 'active'}, 'turns': []}}
        self.bridge.rpc.call = call
        await self.bridge.reconcile()
        self.assertEqual(self.bridge.turns['thread-a']['status'], 'inProgress')
        self.assertEqual(self.bridge.turns['thread-a']['items'][0]['text'], 'Recent output')

    async def test_recovery_review_uses_read_only_sandbox_and_never_approves_tools(self):
        self.bridge.metadata['sessions']['thread-a'] = {'fullAccess': True}
        saved = {'id': 'crashed', 'status': 'backendInterrupted', 'items': []}
        self.bridge.journal.save_turn('thread-a', saved)
        async def loaded(identity, include_turns=False):
            return {'cwd': '/tmp', 'status': {'type': 'idle'}}
        async def request(method, params):
            self.assertEqual(params['sandboxPolicy'], {'type': 'readOnly'})
            self.assertEqual(params['approvalPolicy'], 'never')
            self.assertIn('Do not repeat any previous command', params['input'][0]['text'])
            turn = {'id': 'review', 'status': 'completed', 'items': []}
            await self.bridge.event({'method': 'turn/started', 'params': {'threadId': 'thread-a', 'turn': {**turn, 'status': 'inProgress'}}})
            await self.bridge.event({'method': 'turn/completed', 'params': {'threadId': 'thread-a', 'turn': turn}})
            return {'turn': turn}
        self.bridge.ensure_loaded = loaded; self.bridge.rpc.request = request
        result = await self.bridge.dispatch(self.phone, '_goose/unstable/session/resume_run', {'sessionId': 'thread-a', 'runId': 'crashed'})
        self.assertEqual(result['stopReason'], 'end_turn')
        self.assertTrue(self.bridge.journal.turns('thread-a')[0]['reviewed'])


if __name__ == '__main__':
    unittest.main()
