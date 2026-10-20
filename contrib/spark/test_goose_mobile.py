import asyncio
from pathlib import Path
import tempfile
import unittest

from goose_mobile import Bridge, RPCError, item_update, run_status


class Socket:
    closed = False

    def __init__(self):
        self.packets = []

    async def send_json(self, packet):
        self.packets.append(packet)


class MobileTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        token = root / 'token'; token.write_text('test-only-token')
        self.bridge = Bridge(root / 'state', token, token)
        self.bridge.rpc.ws = Socket()
        self.phone = Socket()
        self.bridge.clients[self.phone] = {'thread-a'}

    async def test_pending_approval_survives_phone_disconnect_and_requires_explicit_allow(self):
        self.bridge.clients.clear()
        request = {'id': 17, 'method': 'item/commandExecution/requestApproval', 'params': {'threadId': 'thread-a', 'turnId': 'turn-a', 'itemId': 'item-a', 'command': 'example requiring approval'}}
        await self.bridge.event(request)
        self.assertEqual(self.bridge.rpc.ws.packets, [])
        self.bridge.clients[self.phone] = {'thread-a'}
        await self.bridge.deliver_permission('codex-17', only=self.phone)
        self.assertEqual(self.phone.packets[-1]['method'], 'session/request_permission')
        await self.bridge.answer(self.phone, {'id': 'codex-17', 'result': {'outcome': {'outcome': 'cancelled'}}})
        self.assertEqual(self.bridge.rpc.ws.packets[-1], {'id': 17, 'result': {'decision': 'decline'}})

    async def test_other_session_cannot_answer_permission_or_answer_twice(self):
        request = {'id': 18, 'method': 'item/fileChange/requestApproval', 'params': {'threadId': 'thread-a', 'turnId': 'turn-a', 'itemId': 'edit-a'}}
        await self.bridge.event(request)
        wrong_phone = Socket(); self.bridge.clients[wrong_phone] = {'thread-b'}
        answer = {'id': 'codex-18', 'result': {'outcome': {'outcome': 'selected', 'optionId': 'accept'}}}
        await self.bridge.answer(wrong_phone, answer)
        self.assertEqual(self.bridge.rpc.ws.packets, [])
        await self.bridge.answer(self.phone, answer)
        await self.bridge.answer(self.phone, answer)
        self.assertEqual(self.bridge.rpc.ws.packets, [{'id': 18, 'result': {'decision': 'accept'}}])

    async def test_user_question_translates_to_phone_form(self):
        await self.bridge.event({'id': 19, 'method': 'item/tool/requestUserInput', 'params': {'threadId': 'thread-a', 'turnId': 'turn-a', 'questions': [{'id': 'choice', 'header': 'Choice', 'question': 'Which folder?'}]}})
        self.assertEqual(self.phone.packets[-1]['method'], 'elicitation/create')
        await self.bridge.answer(self.phone, {'id': 'codex-19', 'result': {'action': 'accept', 'content': {'choice': '/tmp/project'}}})
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

    async def test_reconnect_does_not_duplicate_deltas_during_history_read(self):
        self.bridge.turns['thread-a'] = {'id': 'turn-a', 'status': 'inProgress', 'items': [{'id': 'answer', 'type': 'agentMessage', 'text': 'Before'}]}
        async def loaded(identity, include_turns=False):
            await self.bridge.event({'method': 'item/agentMessage/delta', 'params': {'threadId': identity, 'itemId': 'answer', 'delta': ' during'}})
            return {'id': identity, 'cwd': '/tmp', 'updatedAt': 1, 'turns': []}
        self.bridge.ensure_loaded = loaded
        await self.bridge.dispatch(self.phone, 'session/load', {'sessionId': 'thread-a'})
        chunks = [p['params']['update']['content']['text'] for p in self.phone.packets if p.get('params', {}).get('update', {}).get('sessionUpdate') == 'agent_message_chunk']
        self.assertEqual(chunks, ['Before during'])


if __name__ == '__main__':
    unittest.main()
