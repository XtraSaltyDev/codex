"""Private APNs registration and a durable notification outbox; never dispatch agent work."""

import asyncio
import hashlib
import json
from pathlib import Path
import re
import time
import uuid


KINDS = {
    'completed': ('Task completed', 'Your Codex task is ready to review.'),
    'failed': ('Task failed', 'Open Goose to review the saved task.'),
    'input': ('Codex needs your input', 'Open Goose to respond.'),
}


class PushNotifications:
    def __init__(self, db, host_id, config_path):
        self.db, self.host_id = db, host_id
        self.config_path = Path(config_path)
        self.wake = asyncio.Event()
        self.last_error = None
        self._jwt = None
        self._jwt_time = 0
        self._jwt_identity = None
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS push_devices (
                installation TEXT PRIMARY KEY, token TEXT NOT NULL, enabled INTEGER NOT NULL,
                registered INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS push_events (
                id TEXT PRIMARY KEY, thread TEXT NOT NULL, turn TEXT NOT NULL, kind TEXT NOT NULL,
                expires INTEGER NOT NULL, payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS push_deliveries (
                event TEXT NOT NULL, installation TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending',
                attempts INTEGER NOT NULL DEFAULT 0, retry_at INTEGER NOT NULL DEFAULT 0,
                reason TEXT, PRIMARY KEY(event,installation)
            );
            CREATE TABLE IF NOT EXISTS push_runs (turn TEXT PRIMARY KEY, thread TEXT NOT NULL);
        ''')
        # Upstream request IDs belong to the previous connection after a restart.
        with self.db:
            self.db.execute("UPDATE push_deliveries SET state='obsolete' WHERE state='pending' AND event IN (SELECT id FROM push_events WHERE kind='input')")

    def config(self):
        try:
            value = json.loads(self.config_path.read_text())
            if value['environment'] not in ('sandbox', 'production'):
                raise ValueError('Invalid APNs environment')
            if not all(re.fullmatch(r'[A-Z0-9]{10}', value[k]) for k in ('teamId', 'keyId')):
                raise ValueError('Invalid APNs identity')
            if not re.fullmatch(r'[A-Za-z0-9.-]{3,200}', value['topic']):
                raise ValueError('Invalid APNs topic')
            key = Path(value['keyPath']).expanduser()
            if self.config_path.stat().st_mode & 0o077 or key.stat().st_mode & 0o077:
                raise ValueError('APNs files must be private')
            return value
        except FileNotFoundError:
            return None
        except (KeyError, TypeError, ValueError, OSError):
            self.last_error = 'Invalid or unreadable private APNs configuration.'
            return None

    def descriptor(self):
        config = self.config()
        return {'hostId': self.host_id, 'available': config is not None,
                'environment': config['environment'] if config else None,
                'topic': config['topic'] if config else None}

    def register(self, params):
        installation = params.get('installationId', '')
        if not isinstance(installation, str) or not re.fullmatch(r'[a-fA-F0-9-]{36}', installation):
            raise ValueError('Invalid notification installation ID.')
        uuid.UUID(installation)
        if params.get('hostId') != self.host_id:
            raise ValueError('This notification registration belongs to another host.')
        if type(params.get('enabled')) is not bool:
            raise ValueError('Choose whether task notifications are enabled.')
        enabled = params['enabled']
        token = params.get('deviceToken', '')
        if enabled and (not isinstance(token, str) or not re.fullmatch(r'[0-9a-fA-F]{64,512}', token)):
            raise ValueError('Invalid Apple notification token.')
        if enabled and not self.config():
            raise ValueError('Task notifications are not configured on this host.')
        now = int(time.time())
        with self.db:
            if enabled:
                self.db.execute('INSERT INTO push_devices VALUES(?,?,1,?) ON CONFLICT(installation) DO UPDATE SET token=excluded.token,enabled=1,registered=excluded.registered', (installation, token.lower(), now))
            else:
                self.db.execute('UPDATE push_devices SET enabled=0 WHERE installation=?', (installation,))
                self.db.execute("UPDATE push_deliveries SET state='disabled' WHERE installation=? AND state='pending'", (installation,))
        self.wake.set()
        return {'registered': enabled, **self.descriptor()}

    def watch(self, thread, turn):
        with self.db:
            self.db.execute('INSERT OR IGNORE INTO push_runs VALUES(?,?)', (turn, thread))

    def enqueue(self, thread, turn, kind, request=None, test=False, installation=None):
        if kind not in KINDS:
            raise ValueError('Unknown task notification type.')
        event = hashlib.sha256(f'{self.host_id}:{turn}:{kind}:{request or ""}'.encode()).hexdigest()
        title, body = KINDS[kind]
        if test:
            title, body = 'Test: ' + title, 'Goose notification test. Tap to open the selected chat.'
        payload = {'aps': {'alert': {'title': title, 'body': body}, 'sound': 'default',
                           'thread-id': hashlib.sha256(f'{self.host_id}:{thread}'.encode()).hexdigest()},
                   'goose': {'version': 1, 'hostId': self.host_id, 'sessionId': thread,
                             'eventId': event, 'kind': kind, 'test': test}}
        expires = int(time.time()) + (600 if kind == 'input' else 86400)
        with self.db:
            created = self.db.execute('INSERT OR IGNORE INTO push_events VALUES(?,?,?,?,?,?)',
                                      (event, thread, turn, kind, expires, json.dumps(payload))).rowcount
            if created:
                self.db.execute('INSERT INTO push_deliveries(event,installation,retry_at) SELECT ?,installation,? FROM push_devices WHERE enabled=1' + (' AND installation=?' if installation else ''),
                                (event, int(time.time()) + (10 if test else 0), *([installation] if installation else [])))
        self.wake.set()
        return event

    def cancel_input(self, thread, turn=None):
        with self.db:
            sql = "UPDATE push_deliveries SET state='obsolete' WHERE state='pending' AND event IN (SELECT id FROM push_events WHERE kind='input' AND thread=?"
            args = [thread]
            if turn:
                sql += ' AND turn=?'; args.append(turn)
            self.db.execute(sql + ')', args)

    def finish(self, thread, turn):
        self.cancel_input(thread, turn['id'])
        kind = {'completed': 'completed', 'failed': 'failed', 'backendInterrupted': 'failed'}.get(turn['status'])
        if kind:
            self.enqueue(thread, turn['id'], kind)

    def reconcile(self, turns):
        watched = {r[0] for r in self.db.execute('SELECT turn FROM push_runs')}
        for record in turns:
            if record['id'] in watched and record['snapshot']['status'] != 'inProgress':
                self.finish(record['thread'], record['snapshot'])

    def forget(self, thread):
        self.cancel_input(thread)
        with self.db:
            self.db.execute("UPDATE push_deliveries SET state='deleted' WHERE state='pending' AND event IN (SELECT id FROM push_events WHERE thread=?)", (thread,))

    def status(self):
        return {**self.descriptor(), 'lastError': self.last_error,
                'pending': self.db.execute("SELECT count(*) FROM push_deliveries WHERE state='pending'").fetchone()[0]}

    def provider_token(self, config):
        now = int(time.time())
        identity = (config['teamId'], config['keyId'], config['keyPath'])
        if not self._jwt or self._jwt_identity != identity or now - self._jwt_time > 2700:
            import jwt
            self._jwt = jwt.encode({'iss': config['teamId'], 'iat': now},
                                   Path(config['keyPath']).expanduser().read_text(), algorithm='ES256',
                                   headers={'kid': config['keyId']})
            self._jwt_time = now
            self._jwt_identity = identity
        return self._jwt

    async def deliver_one(self, client, config):
        now = int(time.time())
        row = self.db.execute('''SELECT e.*,d.installation,d.attempts,v.token FROM push_deliveries d
            JOIN push_events e ON e.id=d.event JOIN push_devices v ON v.installation=d.installation
            WHERE d.state='pending' AND d.retry_at<=? AND v.enabled=1 ORDER BY e.rowid LIMIT 1''', (now,)).fetchone()
        if not row:
            return False
        if row['expires'] <= now:
            with self.db:
                self.db.execute("UPDATE push_deliveries SET state='expired' WHERE event=? AND installation=?", (row['id'], row['installation']))
            return True
        attempt = row['attempts'] + 1
        delay = min(300, 5 * 2 ** min(attempt, 6))
        with self.db:
            self.db.execute('UPDATE push_deliveries SET attempts=?,retry_at=? WHERE event=? AND installation=?', (attempt, now + delay, row['id'], row['installation']))
        endpoint = 'api.sandbox.push.apple.com' if config['environment'] == 'sandbox' else 'api.push.apple.com'
        reason, state = None, 'pending'
        try:
            headers = {'authorization': 'bearer ' + self.provider_token(config), 'apns-topic': config['topic'],
                       'content-type': 'application/json', 'apns-push-type': 'alert', 'apns-priority': '10',
                       'apns-expiration': str(row['expires']), 'apns-collapse-id': row['id'],
                       'apns-id': str(uuid.uuid5(uuid.NAMESPACE_URL, row['id'] + row['installation']))}
            response = await client.post(f'https://{endpoint}/3/device/{row["token"]}', headers=headers, content=row['payload'])
            reason = response.json().get('reason', 'APNs rejected the notification') if response.status_code != 200 else None
            if response.status_code == 200:
                state = 'sent'; self.last_error = None
            elif response.status_code not in (429, 500, 503):
                state = 'rejected'
                if response.status_code == 410 or reason in ('BadDeviceToken', 'DeviceTokenNotForTopic'):
                    with self.db:
                        self.db.execute('UPDATE push_devices SET enabled=0 WHERE installation=? AND token=?', (row['installation'], row['token']))
        except Exception:
            reason = 'APNs connection failed; notification delivery will retry.'
        if state == 'pending' and attempt >= 6:
            state = 'exhausted'
        if reason:
            self.last_error = str(reason)[:120]
        with self.db:
            self.db.execute('UPDATE push_deliveries SET state=?,reason=? WHERE event=? AND installation=?', (state, str(reason)[:120] if reason else None, row['id'], row['installation']))
        return True

    async def run(self):
        import httpx
        async with httpx.AsyncClient(http2=True, timeout=15) as client:
            while True:
                self.wake.clear()
                config = self.config()
                try:
                    if config and await self.deliver_one(client, config):
                        continue
                except Exception:
                    self.last_error = 'Could not prepare an Apple notification. Check the host push configuration.'
                try:
                    await asyncio.wait_for(self.wake.wait(), timeout=5)
                except TimeoutError:
                    pass
