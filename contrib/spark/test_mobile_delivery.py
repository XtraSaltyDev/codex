import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from aiohttp import WSMsgType
from goose_mobile import Bridge
from mobile_delivery import MobileDelivery
from test_goose_mobile import Socket


class StalledSocket(Socket):
    def __init__(self):
        super().__init__()
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.attempts = 0

    async def send_str(self, payload):
        self.attempts += 1
        self.started.set()
        await self.release.wait()
        await super().send_str(payload)


class DeliveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        token = root / 'token'; token.write_text('synthetic-token')
        self.bridge = Bridge(root / 'state', token, token, push_config=root / 'no-push')
        self.fast, self.slow = Socket(), StalledSocket()
        self.bridge.clients = {self.fast: {'thread'}, self.slow: {'thread'}}
        self.bridge.complete_review = AsyncMock()

    async def asyncTearDown(self):
        for ws in list(self.bridge.clients):
            self.bridge.disconnect(ws)
        tasks = self.bridge.tasks | self.bridge.delivery_tasks
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.bridge.journal.close()
        self.temp.cleanup()

    async def flush(self, ws):
        delivery = self.bridge.deliveries.get(ws)
        if delivery:
            await asyncio.wait_for(delivery.queue.join(), 1)

    async def test_stalled_subscriber_does_not_block_native_rpc_or_fast_stream(self):
        packets = [{'method': 'item/agentMessage/delta', 'params': {'threadId': 'thread', 'turnId': 'turn', 'itemId': 'answer', 'delta': str(index)}} for index in range(80)]
        packets.append({'id': 99, 'result': {'ok': True}})
        class NativeSocket:
            def __aiter__(self):
                self.packets = iter(packets)
                return self
            async def __anext__(self):
                try:
                    return type('Frame', (), {'type': WSMsgType.TEXT, 'data': json.dumps(next(self.packets))})()
                except StopIteration:
                    raise StopAsyncIteration
        self.bridge.rpc.ws = NativeSocket()
        future = asyncio.get_running_loop().create_future()
        self.bridge.rpc.pending[99] = future
        await asyncio.wait_for(self.bridge.rpc.read(), .5)
        self.assertEqual(future.result(), {'ok': True})
        self.assertTrue(self.slow.started.is_set())
        self.assertFalse(self.slow.release.is_set())
        await self.flush(self.fast)
        self.assertEqual([p['params']['update']['content']['text'] for p in self.fast.packets], [str(i) for i in range(80)])
        self.assertEqual(self.slow.attempts, 1)
        self.assertEqual(self.bridge.turns['thread']['items'][0]['text'], ''.join(str(i) for i in range(80)))

    async def test_timeout_evicts_once_and_reconnect_replays_without_dispatch(self):
        with patch.object(MobileDelivery, 'SEND_TIMEOUT', .02):
            await self.bridge.update('thread', {'sessionUpdate': 'agent_message_chunk', 'content': {'type': 'text', 'text': 'saved'}})
            await asyncio.wait_for(self.slow.started.wait(), .5)
            async with asyncio.timeout(.5):
                while self.slow in self.bridge.clients:
                    await asyncio.sleep(.005)
        self.assertNotIn(self.slow, self.bridge.deliveries)
        self.assertIn(self.fast, self.bridge.clients)
        for _ in range(5):
            await self.bridge.update('thread', {'sessionUpdate': 'session_info_update'})
        self.assertEqual(self.slow.attempts, 1)
        saved = {'id': 'saved', 'status': 'completed', 'items': [{'id': 'answer', 'type': 'agentMessage', 'text': 'Saved result'}]}
        self.bridge.journal.save_turn('thread', saved)
        self.bridge.ensure_loaded = AsyncMock(return_value={'id': 'thread', 'cwd': self.temp.name, 'updatedAt': 1})
        self.bridge.rpc.request = AsyncMock(side_effect=AssertionError('Replay must not dispatch work'))
        restored = Socket(); self.bridge.clients[restored] = set()
        result = await self.bridge.dispatch(restored, 'session/load', {'sessionId': 'thread'})
        await self.flush(restored)
        self.assertEqual(result['_meta']['hostRuns'][0]['state'], 'finished')
        self.assertIn('Saved result', json.dumps(restored.packets))
        self.bridge.rpc.request.assert_not_called()

    async def test_count_and_byte_limits_evict_only_the_lagging_client(self):
        for limit in ('MAX_MESSAGES', 'MAX_BYTES'):
            with self.subTest(limit=limit):
                slow = StalledSocket(); self.bridge.clients[slow] = {'thread'}
                with patch.object(MobileDelivery, limit, 3 if limit == 'MAX_MESSAGES' else 400):
                    await self.bridge.send(slow, {'first': 'x'})
                    await slow.started.wait()
                    delivery = self.bridge.deliveries[slow]
                    for index in range(10):
                        await self.bridge.send(slow, {'index': index, 'data': 'x' * 150})
                    self.assertNotIn(slow, self.bridge.clients)
                    self.assertNotIn(slow, self.bridge.deliveries)
                    await asyncio.sleep(0)
                    self.assertEqual(delivery.messages, 0)
                    self.assertEqual(delivery.bytes, 0)
                    self.assertEqual(slow.attempts, 1)
                    self.assertIn(self.fast, self.bridge.clients)

    async def test_stalled_history_does_not_hold_stream_lock_and_live_data_follows_snapshot(self):
        old = {'id': 'old', 'status': 'completed', 'items': [{'id': f'a-{i}', 'type': 'agentMessage', 'text': str(i)} for i in range(600)]}
        self.bridge.journal.save_turn('thread', old)
        self.bridge.ensure_loaded = AsyncMock(return_value={'id': 'thread', 'cwd': self.temp.name, 'updatedAt': 1})
        await asyncio.wait_for(self.bridge.dispatch(self.slow, 'session/load', {'sessionId': 'thread'}), .5)
        await self.slow.started.wait()
        await asyncio.wait_for(self.bridge.event({'method': 'item/agentMessage/delta', 'params': {'threadId': 'thread', 'turnId': 'new', 'itemId': 'latest', 'delta': 'LIVE'}}), .5)
        self.assertIn('LIVE', json.dumps(self.fast.packets))
        self.slow.release.set()
        await self.flush(self.slow)
        updates = [p['params']['update'] for p in self.slow.packets]
        texts = [u['content']['text'] for u in updates if 'content' in u]
        self.assertEqual(texts, [str(i) for i in range(600)] + ['LIVE'])
        self.assertIn(self.slow, self.bridge.clients)

    async def test_socket_failure_and_hanging_close_do_not_delay_other_clients(self):
        class BrokenSocket(Socket):
            async def send_str(self, payload):
                raise ConnectionError('Synthetic transport failure')
            async def close(self, **kwargs):
                await asyncio.Event().wait()
        broken = BrokenSocket(); self.bridge.clients[broken] = {'thread'}
        await asyncio.wait_for(self.bridge.update('thread', {'sessionUpdate': 'session_info_update'}), .5)
        self.assertNotIn(broken, self.bridge.clients)
        await self.flush(self.fast)
        self.assertEqual(len(self.fast.packets), 1)

    async def test_repeated_history_requests_are_bounded_and_writer_is_cancelled_on_disconnect(self):
        delivery = self.bridge.delivery(self.slow)
        self.assertTrue(delivery.replay(iter(({'id': 'history-one'},))))
        await self.slow.started.wait()
        self.assertTrue(delivery.replay(iter(({'id': 'history-two'},))))
        self.assertFalse(delivery.replay(iter(({'id': 'history-three'},))))
        await asyncio.sleep(0)
        self.assertNotIn(self.slow, self.bridge.clients)
        self.assertTrue(delivery.writer.done())
        self.assertEqual(delivery.replays, 0)

