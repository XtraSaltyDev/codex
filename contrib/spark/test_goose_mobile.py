import asyncio
from pathlib import Path
import tempfile
import unittest

from goose_mobile import Bridge, RPCError, item_update, run_status
from mobile_state import merge_items


class Socket:
    closed = False

    def __init__(self):
        self.packets = []

    async def send_json(self, packet):
        self.packets.append(packet)


class MobileTests(unittest.IsolatedAsyncioTestCase):
    async def test_initialize_reports_backend_without_exposing_provider_credentials(self):
        self.bridge.rpc.ready.set()
        async def request(method, params):
            self.assertEqual(method, 'config/read')
            return {'config': {'model': 'provider/model-a', 'model_provider': 'spark',
                               'model_providers': {'spark': {'name': 'DGX Spark', 'api_key': 'private-key'}},
                               'sandbox_mode': 'danger-full-access', 'approval_policy': 'never'}}
        self.bridge.rpc.request = request
        result = await self.bridge.dispatch(self.phone, 'initialize', {})
        meta = result['agentCapabilities']['_meta']['goose']
        self.assertEqual(meta['backend']['modelId'], 'provider/model-a')
        self.assertEqual(meta['backend']['defaultAccess'], 'full')
        self.assertEqual(meta['backend']['providerName'], 'DGX Spark')
        self.assertFalse(meta['workspaceAssignment'])
        self.assertNotIn('private-key', str(result))
        self.assertNotIn('api_key', str(result))

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
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        token = root / 'token'; token.write_text('test-only-token')
        self.bridge = Bridge(root / 'state', token, token)
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
        self.assertEqual(await future, 'completed')
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

    async def test_crash_after_dispatch_preserves_message_and_never_retransmits(self):
        self.bridge.journal.prepare('uncertain', 'thread-a', 'append a record')
        self.bridge.journal.phase('uncertain', 'dispatching')
        restored = Bridge(self.bridge.state, Path(self.temp.name) / 'token', Path(self.temp.name) / 'token')
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
        restored = Bridge(self.bridge.state, Path(self.temp.name) / 'token', Path(self.temp.name) / 'token')
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
