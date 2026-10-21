import asyncio
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from goose_mobile import Bridge, RPCError, RPCNotSent, RPCRejected
from mobile_state import REVIEW_PROMPT_PREFIX
from test_goose_mobile import Socket


class RecoveryReviewTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.token = self.root / 'token'; self.token.write_text('synthetic-token')
        self.bridge = self.make_bridge()
        self.saved = {'id': 'checkpoint', 'status': 'backendInterrupted', 'items': []}
        self.bridge.journal.save_turn('thread', self.saved)
        self.bridge.turns['thread'] = self.saved
        self.capture = patch('goose_mobile.capture_review', return_value={'unavailable': True})
        self.capture.start()
        self.calls = 0

    def make_bridge(self):
        bridge = Bridge(self.root / 'state', self.token, self.token, push_config=self.root / 'no-push')
        self.phone = Socket(); bridge.clients[self.phone] = {'thread'}
        bridge.ensure_loaded = AsyncMock(return_value={'id': 'thread', 'cwd': str(self.root), 'status': {'type': 'idle'}})
        bridge.complete_review = AsyncMock()
        return bridge

    async def asyncTearDown(self):
        self.capture.stop()
        await self.close_bridge()
        self.temp.cleanup()

    async def close_bridge(self):
        for ws in list(self.bridge.clients):
            self.bridge.disconnect(ws, code=1000)
        tasks = self.bridge.tasks | self.bridge.delivery_tasks
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.bridge.journal.close()

    async def restart(self):
        await self.close_bridge()
        self.bridge = self.make_bridge()

    def can_review(self):
        return self.bridge.status(self.saved)['recovery']['canResume']

    def reviewed(self):
        return next(row['reviewed'] for row in self.bridge.journal.turns('thread') if row['id'] == 'checkpoint')

    async def review(self):
        return await self.bridge.dispatch(self.phone, '_goose/unstable/session/resume_run', {'sessionId': 'thread', 'runId': 'checkpoint'})

    async def accepted(self, method, params):
        self.calls += 1
        self.assertEqual(method, 'turn/start')
        self.assertEqual(params['clientUserMessageId'], 'review-checkpoint')
        self.assertEqual(params['sandboxPolicy'], {'type': 'readOnly'})
        self.assertEqual(params['approvalPolicy'], 'never')
        self.assertFalse(self.reviewed())
        turn = {'id': 'review-turn', 'status': 'inProgress', 'items': []}
        await self.bridge.event({'method': 'turn/started', 'params': {'threadId': 'thread', 'turn': turn}})
        self.assertTrue(self.reviewed())
        self.assertFalse(self.can_review())
        await self.bridge.event({'method': 'turn/completed', 'params': {'threadId': 'thread', 'turn': {**turn, 'status': 'completed'}}})
        return {'turn': turn}

    async def test_load_failure_stays_retryable_through_restart_and_reconciliation(self):
        self.bridge.ensure_loaded.side_effect = RPCError('Synthetic backend unavailable before dispatch')
        with self.assertRaisesRegex(RPCError, 'unavailable'):
            await self.review()
        self.assertTrue(self.can_review())
        self.assertFalse(self.reviewed())
        self.assertEqual(self.bridge.journal.submission('review-checkpoint')['phase'], 'prepared')
        await self.restart()
        self.bridge.rpc.call = AsyncMock(return_value={'thread': {'id': 'thread', 'status': {'type': 'idle'}, 'turns': []}})
        await self.bridge.reconcile()
        self.assertTrue(self.can_review())
        self.assertEqual(self.bridge.journal.submission('review-checkpoint')['phase'], 'prepared')
        self.assertEqual(len(self.bridge.journal.turns('thread')), 1)
        self.bridge.rpc.request = self.accepted
        self.assertEqual((await self.review())['stopReason'], 'end_turn')
        self.assertEqual((await self.review())['stopReason'], 'end_turn')
        self.assertEqual(self.calls, 1)

    async def test_native_rejection_resets_only_unaccepted_review_and_refreshes_baseline(self):
        self.bridge.rpc.request = AsyncMock(side_effect=RPCRejected('Synthetic native rejection', -32602))
        with self.assertRaises(RPCRejected):
            await self.review()
        self.assertTrue(self.can_review())
        self.assertFalse(self.reviewed())
        self.assertEqual(self.bridge.journal.submission('review-checkpoint')['phase'], 'prepared')
        self.assertIsNone(self.bridge.journal.pending_review('review-checkpoint'))
        self.bridge.rpc.request = self.accepted
        self.assertEqual((await self.review())['stopReason'], 'end_turn')
        self.assertEqual(self.calls, 1)

    async def test_closed_socket_is_definitely_unsent_and_can_be_retried(self):
        self.bridge.rpc.ws = Socket(); self.bridge.rpc.ws.closed = True
        self.bridge.rpc.ready.set()
        with self.assertRaises(RPCNotSent):
            await self.review()
        self.assertTrue(self.can_review())
        self.assertEqual(self.bridge.journal.submission('review-checkpoint')['phase'], 'prepared')
        self.assertEqual(self.bridge.rpc.ws.packets, [])

    async def test_readiness_failure_does_not_attempt_a_native_write(self):
        self.bridge.rpc.ws = Socket()
        with patch.object(self.bridge.rpc.ready, 'wait', AsyncMock(side_effect=TimeoutError)):
            with self.assertRaises(RPCNotSent):
                await self.review()
        self.assertTrue(self.can_review())
        self.assertEqual(self.bridge.rpc.ws.packets, [])

    async def test_unknown_dispatch_is_never_resent_even_after_restart(self):
        self.bridge.rpc.request = AsyncMock(side_effect=TimeoutError('Acknowledgement lost'))
        with self.assertRaises(TimeoutError):
            await self.review()
        self.assertFalse(self.can_review())
        self.assertFalse(self.reviewed())
        with self.assertRaisesRegex(RPCError, 'already submitted'):
            await self.review()
        self.bridge.rpc.request.assert_awaited_once()
        await self.restart()
        self.bridge.rpc.call = AsyncMock(return_value={'thread': {'id': 'thread', 'status': {'type': 'idle'}, 'turns': []}})
        await self.bridge.reconcile()
        self.assertEqual(self.bridge.journal.submission('review-checkpoint')['phase'], 'unconfirmed')
        self.assertFalse(self.can_review())
        self.assertFalse(self.reviewed())
        self.assertEqual(len(self.bridge.journal.turns('thread')), 1)
        with self.assertRaisesRegex(RPCError, 'already submitted'):
            await self.review()

    async def test_started_notification_wins_over_lost_acknowledgement(self):
        async def request(method, params):
            self.calls += 1
            await self.bridge.event({'method': 'turn/started', 'params': {'threadId': 'thread', 'turn': {'id': 'review-turn', 'status': 'inProgress', 'items': []}}})
            raise TimeoutError('Acknowledgement lost after acceptance')
        self.bridge.rpc.request = request
        with self.assertRaises(TimeoutError):
            await self.review()
        self.assertTrue(self.reviewed())
        self.assertFalse(self.can_review())
        await self.bridge.event({'method': 'turn/completed', 'params': {'threadId': 'thread', 'turn': {'id': 'review-turn', 'status': 'completed', 'items': []}}})
        self.assertEqual((await self.review())['stopReason'], 'end_turn')
        self.assertEqual(self.calls, 1)

    async def test_reconciliation_consumes_a_review_proven_accepted_by_native_history(self):
        self.bridge.journal.prepare('review-checkpoint', 'thread', REVIEW_PROMPT_PREFIX)
        self.bridge.journal.phase('review-checkpoint', 'dispatching')
        native = {'id': 'accepted-native', 'status': 'completed', 'items': [{'id': 'user', 'type': 'userMessage', 'clientId': 'review-checkpoint', 'content': []}]}
        self.bridge.rpc.call = AsyncMock(return_value={'thread': {'id': 'thread', 'status': {'type': 'idle'}, 'turns': [native]}})
        await self.bridge.reconcile()
        self.assertTrue(self.reviewed())
        self.assertEqual(self.bridge.journal.submission('review-checkpoint')['phase'], 'finished')
        self.bridge.rpc.call.assert_awaited_once_with('thread/resume', {'threadId': 'thread'})

    async def test_legacy_prepared_flag_is_repaired_but_accepted_flag_is_preserved(self):
        self.bridge.journal.prepare('review-checkpoint', 'thread', REVIEW_PROMPT_PREFIX)
        self.bridge.journal.reviewed('checkpoint')
        await self.restart()
        self.assertFalse(self.reviewed())
        self.assertTrue(self.can_review())
        self.bridge.journal.phase('review-checkpoint', 'accepted', 'native-turn')
        await self.restart()
        self.assertTrue(self.reviewed())
        self.assertFalse(self.can_review())

    async def test_acceptance_cannot_consume_a_checkpoint_in_another_thread(self):
        self.bridge.journal.prepare('review-checkpoint', 'other-thread', REVIEW_PROMPT_PREFIX)
        self.bridge.journal.phase('review-checkpoint', 'accepted', 'other-native-turn')
        self.assertFalse(self.reviewed())
        self.assertTrue(self.can_review())
