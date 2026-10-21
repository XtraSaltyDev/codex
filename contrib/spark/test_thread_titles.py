import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from goose_mobile import Bridge
from test_goose_mobile import Socket
from thread_titles import MAX_PROMPT_BYTES, parse_title, title_prompt


class TitleTextTests(unittest.TestCase):
    def test_title_metadata_is_bounded_and_validated(self):
        for text in ('🚀' * 1000, '漢' * 1000, 'x' * 2000):
            self.assertLessEqual(len(title_prompt(text).encode()), MAX_PROMPT_BYTES)
        self.assertEqual(parse_title('{"title":"  `Fix login?!`  "}'), 'Fix login')
        self.assertEqual(parse_title(json.dumps({'title': '🚀' * 40})), '🚀' * 36)
        for text in ('plain text', '[]', '{"title":2}', '{"title":" "}', '{"title":"yes","extra":true}', None):
            self.assertIsNone(parse_title(text))


class ThreadTitleTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        token = root / 'token'; token.write_text('test-only-token')
        self.bridge = Bridge(root / 'state', token, token, push_config=root / 'unconfigured.json')
        self.addCleanup(self.bridge.journal.close)
        self.bridge.rpc.ready.set()
        self.bridge.rpc.ws = Socket()
        self.phone = Socket()
        self.bridge.clients[self.phone] = {'thread-a'}
        self.calls = []
        self.name = None
        self.generated = '{"title":"Fix login timeout"}'
        self.started = asyncio.Event()
        self.release = asyncio.Event(); self.release.set()
        self.bridge.metadata['sessions']['thread-a'] = {'modelId': 'spark-model'}
        self.bridge.rpc.request = self.request

    async def asyncTearDown(self):
        self.release.set()
        self.bridge.titles.cancel_all()
        await asyncio.gather(*self.bridge.tasks)

    async def request(self, method, params):
        self.calls.append((method, params))
        if method == 'thread/read':
            return {'thread': {'id': 'thread-a', 'cwd': '/tmp/project', 'name': self.name, 'modelProvider': 'spark'}}
        if method == 'config/read':
            return {'config': {'model': 'host-default', 'model_provider': 'spark', 'mcp_servers': {'private': {}}}}
        if method == 'thread/start':
            return {'thread': {'id': 'hidden'}, 'sandbox': {'type': 'readOnly'}}
        if method == 'turn/start':
            self.started.set()
            self.bridge.spawn(self.reply())
            return {'turn': {'id': 'title-turn'}}
        if method == 'thread/name/set':
            self.name = params['name']
            await self.bridge.event({'method': 'thread/name/updated', 'params': {'threadId': 'thread-a', 'threadName': self.name}})
        return {}

    async def reply(self):
        await self.release.wait()
        await self.bridge.event({'method': 'turn/started', 'params': {
            'threadId': 'hidden', 'turn': {'id': 'title-turn', 'status': 'inProgress', 'items': []}}})
        await self.bridge.event({'method': 'item/completed', 'params': {'threadId': 'hidden', 'turnId': 'title-turn',
            'item': {'id': 'title', 'type': 'agentMessage', 'text': self.generated}}})
        await self.bridge.event({'method': 'turn/completed', 'params': {
            'threadId': 'hidden', 'turn': {'id': 'title-turn', 'status': 'completed', 'items': []}}})

    async def settle(self):
        while self.bridge.tasks:
            await asyncio.gather(*self.bridge.tasks)

    async def test_automatic_title_uses_spark_and_stays_out_of_chat_history(self):
        self.bridge.titles.schedule('thread-a', 'Fix the login timeout')
        self.bridge.titles.schedule('thread-a', 'Duplicate waiters must not generate twice')
        await self.settle()
        self.assertEqual(self.name, 'Fix login timeout')
        methods = [m for m, _ in self.calls]
        self.assertEqual(methods.count('turn/start'), 1)
        self.assertEqual(methods[-1], 'thread/unsubscribe')
        start = next(p for m, p in self.calls if m == 'thread/start')
        self.assertEqual(start['model'], 'spark-model')
        self.assertEqual(start['modelProvider'], 'spark')
        self.assertTrue(start['ephemeral'])
        self.assertEqual(start['threadSource'], 'thread_title')
        self.assertEqual(start['sandbox'], 'read-only')
        self.assertEqual(start['config']['mcp_servers'], {'private': {'enabled': False}})
        self.assertFalse(start['config']['features.shell_tool'])
        self.assertFalse(start['config']['features.hooks'])
        self.assertEqual(self.bridge.journal.turns(), [])
        self.assertEqual(self.bridge.journal.db.execute('SELECT count(*) FROM push_events').fetchone()[0], 0)
        self.assertEqual([p['params']['update']['title'] for p in self.phone.packets], ['Fix login timeout'])
        self.assertFalse(self.bridge.titles.pending)
        self.assertFalse(self.bridge.titles.hidden)

    async def test_manual_name_is_preserved_before_or_during_generation(self):
        self.name = 'Keep my name'
        self.bridge.titles.schedule('thread-a', 'Fix login')
        await self.settle()
        self.assertEqual([m for m, _ in self.calls], ['thread/read'])
        self.name = None; self.calls.clear(); self.release.clear()
        self.bridge.titles.schedule('thread-a', 'Fix login')
        await self.started.wait()
        await self.bridge.dispatch(self.phone, '_goose/unstable/session/rename', {'sessionId': 'thread-a', 'title': 'My phone name'})
        self.release.set()
        await self.settle()
        self.assertEqual(self.name, 'My phone name')
        self.assertEqual([p['name'] for m, p in self.calls if m == 'thread/name/set'], ['My phone name'])

    async def test_native_client_rename_wins_even_without_a_notification(self):
        self.release.clear()
        self.bridge.titles.schedule('thread-a', 'Fix login')
        await self.started.wait()
        self.name = 'Terminal rename'
        self.release.set()
        await self.settle()
        self.assertEqual(self.name, 'Terminal rename')
        self.assertNotIn('thread/name/set', [m for m, _ in self.calls])

    async def test_generation_survives_phone_disconnect(self):
        self.release.clear()
        self.bridge.titles.schedule('thread-a', 'Fix login')
        await self.started.wait()
        self.bridge.clients.clear()
        self.release.set()
        await self.settle()
        self.assertEqual(self.name, 'Fix login timeout')

    async def test_mobile_prompt_schedules_naming_without_repeating_user_work(self):
        async def loaded(identity, include_turns=False):
            return {'cwd': '/tmp/project', 'status': {'type': 'idle'}}
        async def request(method, params):
            if method == 'turn/start' and params['threadId'] == 'thread-a':
                self.calls.append((method, params))
                turn = {'id': 'user-turn', 'status': 'inProgress', 'items': []}
                await self.bridge.event({'method': 'turn/started', 'params': {'threadId': 'thread-a', 'turn': turn}})
                await self.bridge.event({'method': 'turn/completed', 'params': {
                    'threadId': 'thread-a', 'turn': {**turn, 'status': 'completed'}}})
                return {'turn': turn}
            return await self.request(method, params)
        self.bridge.ensure_loaded = loaded
        self.bridge.rpc.request = request
        with patch('goose_mobile.capture_review', return_value={'unavailable': True}):
            result = await self.bridge.start_prompt(self.phone, 'thread-a', 'Fix login', 'request-a')
        self.assertEqual(result['stopReason'], 'end_turn')
        await self.settle()
        await self.bridge.start_prompt(self.phone, 'thread-a', 'Fix login', 'request-a')
        self.assertEqual(self.name, 'Fix login timeout')
        self.assertEqual(len([p for m, p in self.calls if m == 'turn/start' and p['threadId'] == 'thread-a']), 1)
        self.assertEqual([t['id'] for t in self.bridge.journal.turns()], ['user-turn'])

    async def test_timeout_interrupts_hidden_turn_and_releases_request(self):
        self.release.clear()
        with patch('thread_titles.TITLE_TIMEOUT', 0.01):
            self.bridge.titles.schedule('thread-a', 'Fix login')
            await self.started.wait()
            jobs = [t for t in self.bridge.tasks if t.get_coro().__name__ == 'generate']
            await asyncio.gather(*jobs)
        self.release.set()
        await self.settle()
        self.assertIsNone(self.name)
        self.assertEqual([m for m, _ in self.calls][-2:], ['turn/interrupt', 'thread/unsubscribe'])
        self.assertFalse(self.bridge.titles.pending)

    async def test_invalid_output_never_renames_and_tool_requests_are_rejected(self):
        self.generated = 'Not valid JSON'
        self.release.clear()
        self.bridge.titles.schedule('thread-a', 'Fix login')
        await self.started.wait()
        await self.bridge.event({'id': 88, 'method': 'item/tool/requestUserInput', 'params': {'threadId': 'hidden'}})
        self.assertEqual(self.bridge.rpc.ws.packets[-1]['error']['code'], -32601)
        self.assertFalse(self.bridge.permissions)
        self.release.set()
        await self.settle()
        self.assertIsNone(self.name)

    async def test_load_replays_authoritative_title_to_phone(self):
        async def loaded(identity, include_turns=False):
            return {'id': identity, 'cwd': '/tmp/project', 'updatedAt': 1, 'name': 'Renamed while offline', 'turns': []}
        self.bridge.ensure_loaded = loaded
        await self.bridge.dispatch(self.phone, 'session/load', {'sessionId': 'thread-a'})
        self.assertEqual(self.phone.packets[-1]['params']['update']['title'], 'Renamed while offline')
