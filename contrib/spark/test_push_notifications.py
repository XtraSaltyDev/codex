import json
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
import uuid
from unittest.mock import patch

import httpx
import jwt
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat

from push_notifications import PushNotifications


class PushTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = sqlite3.connect(self.root / 'journal.sqlite3')
        self.db.row_factory = sqlite3.Row
        self.addCleanup(self.db.close)
        self.key = ec.generate_private_key(ec.SECP256R1())
        key_path = self.root / 'test.p8'
        key_path.write_bytes(self.key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()))
        key_path.chmod(0o600)
        self.config = self.root / 'push.json'
        self.config.write_text(json.dumps({'teamId': 'TESTTEAM12', 'keyId': 'TESTKEY123', 'keyPath': str(key_path),
                                           'topic': 'computer.goose.mobile.dev', 'environment': 'sandbox'}))
        self.config.chmod(0o600)
        self.host = str(uuid.uuid4())
        self.push = PushNotifications(self.db, self.host, self.config)
        self.installation = str(uuid.uuid4())
        self.registration = {'installationId': self.installation, 'hostId': self.host,
                             'deviceToken': 'ab' * 32, 'enabled': True}
        self.push.register(self.registration)

    async def deliver(self, response=200, reason=None, inspect=None):
        def handler(request):
            if inspect:
                inspect(request)
            return httpx.Response(response, json={'reason': reason} if reason else {})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await self.push.deliver_one(client, self.push.config())

    def rows(self):
        return [dict(row) for row in self.db.execute('SELECT * FROM push_deliveries')]

    def test_registration_is_host_scoped_and_rejects_untrusted_token_and_environment(self):
        for extra in ({'hostId': str(uuid.uuid4())}, {'deviceToken': 'https://attacker.invalid'}, {'installationId': 'x' * 36}, {'enabled': 1}):
            with self.assertRaises(ValueError):
                self.push.register({**self.registration, **extra})
        self.assertEqual(self.push.descriptor()['environment'], 'sandbox')
        self.config.chmod(0o644)
        self.assertFalse(self.push.descriptor()['available'])

    def test_payload_contains_only_generic_alert_and_opaque_route_not_prompt_or_commands(self):
        event = self.push.enqueue('thread-a', 'turn-a', 'failed')
        payload = json.loads(self.db.execute('SELECT payload FROM push_events').fetchone()[0])
        self.assertEqual(payload['goose']['sessionId'], 'thread-a')
        self.assertEqual(payload['goose']['eventId'], event)
        self.assertEqual(payload['aps']['alert']['title'], 'Task failed')
        self.assertEqual(set(payload), {'aps', 'goose'})
        self.assertEqual(self.push.enqueue('thread-a', 'turn-a', 'failed'), event)
        self.assertEqual(len(self.rows()), 1)

    async def test_successful_delivery_is_not_repeated_after_restart_and_signs_correct_topic(self):
        self.push.enqueue('thread-a', 'turn-a', 'completed')
        def inspect(request):
            self.assertEqual(request.url.host, 'api.sandbox.push.apple.com')
            self.assertEqual(request.headers['apns-topic'], 'computer.goose.mobile.dev')
            self.assertEqual(request.headers['apns-push-type'], 'alert')
            token = request.headers['authorization'].split(' ', 1)[1]
            claims = jwt.decode(token, self.key.public_key(), algorithms=['ES256'])
            self.assertEqual(claims['iss'], 'TESTTEAM12')
            self.assertEqual(jwt.get_unverified_header(token)['kid'], 'TESTKEY123')
        self.assertTrue(await self.deliver(inspect=inspect))
        self.assertEqual(self.rows()[0]['state'], 'sent')
        self.push = PushNotifications(self.db, self.host, self.config)
        self.push.enqueue('thread-a', 'turn-a', 'completed')
        self.assertFalse(await self.deliver())
        self.assertEqual(self.rows()[0]['attempts'], 1)

    async def test_transient_failure_retries_only_when_due_and_keeps_collapse_identity(self):
        self.push.enqueue('thread-a', 'turn-a', 'completed')
        identities = []
        inspect = lambda request: identities.append((request.headers['apns-id'], request.headers['apns-collapse-id']))
        await self.deliver(503, 'ServiceUnavailable', inspect)
        self.assertEqual(self.rows()[0]['state'], 'pending')
        self.assertFalse(await self.deliver())
        with patch('push_notifications.time.time', return_value=time.time() + 30):
            await self.deliver(inspect=inspect)
        self.assertEqual(identities[0], identities[1])
        self.assertEqual(self.rows()[0]['state'], 'sent')

    async def test_expired_apple_token_is_disabled_and_does_not_affect_task_state(self):
        self.push.enqueue('thread-a', 'turn-a', 'completed')
        await self.deliver(410, 'Unregistered')
        self.assertEqual(self.db.execute('SELECT enabled FROM push_devices').fetchone()[0], 0)
        self.assertEqual(self.rows()[0]['state'], 'rejected')
        self.assertFalse(await self.deliver())

    async def test_old_token_rejection_does_not_disable_a_rotated_token(self):
        self.push.enqueue('thread-a', 'turn-a', 'completed')
        def rotate(request):
            self.push.register({**self.registration, 'deviceToken': 'cd' * 32})
        await self.deliver(410, 'Unregistered', rotate)
        self.assertEqual(self.db.execute('SELECT enabled FROM push_devices').fetchone()[0], 1)

    async def test_input_reply_or_turn_completion_cancels_queued_input_alerts(self):
        self.push.enqueue('thread-a', 'turn-a', 'input', request='request-a')
        self.push.cancel_input('thread-a', 'turn-a')
        self.assertFalse(await self.deliver())
        self.assertEqual(self.rows()[0]['state'], 'obsolete')
        self.push.enqueue('thread-a', 'turn-b', 'input', request='request-b')
        self.push.finish('thread-a', {'id': 'turn-b', 'status': 'completed'})
        self.assertEqual([r['state'] for r in self.rows()], ['obsolete', 'obsolete', 'pending'])

    async def test_expired_alerts_and_disabled_devices_are_not_sent(self):
        self.push.enqueue('thread-a', 'turn-a', 'input')
        with patch('push_notifications.time.time', return_value=time.time() + 601):
            await self.deliver()
        self.assertEqual(self.rows()[0]['state'], 'expired')
        self.push.enqueue('thread-a', 'turn-b', 'completed')
        self.push.register({**self.registration, 'enabled': False})
        self.assertFalse(await self.deliver())
        self.assertEqual(self.rows()[1]['state'], 'disabled')

    def test_new_devices_do_not_receive_historical_alerts_and_reconcile_deduplicates(self):
        self.push.watch('thread-a', 'turn-a')
        records = [{'id': 'old', 'thread': 'thread-a', 'snapshot': {'id': 'old', 'status': 'completed'}},
                   {'id': 'turn-a', 'thread': 'thread-a', 'snapshot': {'id': 'turn-a', 'status': 'backendInterrupted'}}]
        self.push.reconcile(records)
        self.push.register({**self.registration, 'installationId': str(uuid.uuid4())})
        self.push.reconcile(records)
        self.assertEqual(len(self.rows()), 1)
        self.push.finish('thread-a', {'id': 'cancelled', 'status': 'interrupted'})
        self.assertEqual(len(self.rows()), 1)

    def test_chat_deletion_cancels_queued_notifications(self):
        self.push.enqueue('thread-a', 'turn-a', 'completed')
        self.push.forget('thread-a')
        self.assertEqual(self.rows()[0]['state'], 'deleted')

    def test_restart_expires_old_input_requests_but_preserves_completion_delivery(self):
        self.push.enqueue('thread-a', 'turn-a', 'input', request='old-request')
        self.push.enqueue('thread-b', 'turn-b', 'completed')
        self.push = PushNotifications(self.db, self.host, self.config)
        self.assertEqual([row['state'] for row in self.rows()], ['obsolete', 'pending'])

    async def test_diagnostic_notification_is_delayed_and_only_targets_requesting_device(self):
        second = str(uuid.uuid4())
        self.push.register({**self.registration, 'installationId': second})
        self.push.enqueue('thread-a', 'test-turn', 'input', test=True, installation=self.installation)
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(self.rows()[0]['installation'], self.installation)
        self.assertFalse(await self.deliver())
        with patch('push_notifications.time.time', return_value=time.time() + 11):
            self.assertTrue(await self.deliver())


if __name__ == '__main__':
    unittest.main()
