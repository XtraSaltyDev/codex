"""Durable checkpoints and at-most-once submission records for the mobile adapter."""

import hashlib
import json
import os
import sqlite3


def merge_items(native, saved):
    items = {}
    for item in native + saved:
        key = 'user:' + item['clientId'] if item['type'] == 'userMessage' and item.get('clientId') else item['id']
        previous = items.get(key, {})
        merged = {**previous, **item}
        if len(previous.get('text', '')) > len(item.get('text', '')):
            merged['text'] = previous['text']
        if len(previous.get('aggregatedOutput') or '') > len(item.get('aggregatedOutput') or ''):
            merged['aggregatedOutput'] = previous['aggregatedOutput']
        if previous.get('status') == 'completed' and item.get('status') == 'inProgress':
            merged.update({k: v for k, v in previous.items() if k != 'id'})
        items[key] = merged
    return list(items.values())


class MobileState:
    def __init__(self, path):
        self.db = sqlite3.connect(path)
        os.chmod(path, 0o600)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS submissions (
                id TEXT PRIMARY KEY, thread TEXT NOT NULL, text TEXT NOT NULL,
                digest TEXT NOT NULL, phase TEXT NOT NULL, turn TEXT,
                created INTEGER NOT NULL DEFAULT (unixepoch())
            );
            CREATE TABLE IF NOT EXISTS turns (
                id TEXT PRIMARY KEY, thread TEXT NOT NULL, snapshot TEXT NOT NULL,
                stopped INTEGER NOT NULL DEFAULT 0, reviewed INTEGER NOT NULL DEFAULT 0,
                created INTEGER NOT NULL DEFAULT (unixepoch())
            );
            CREATE TABLE IF NOT EXISTS events (
                seq INTEGER PRIMARY KEY, thread TEXT NOT NULL, packet TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS work_reviews (
                request TEXT PRIMARY KEY, thread TEXT NOT NULL, baseline TEXT, result TEXT
            );
        ''')

    def prepare(self, identity, thread, text):
        digest = hashlib.sha256(text.encode()).hexdigest()
        existing = self.submission(identity)
        if existing:
            if existing['thread'] != thread or existing['digest'] != digest:
                raise ValueError('A submission ID cannot be reused for a different message.')
            return existing, False
        with self.db:
            self.db.execute('INSERT INTO submissions(id, thread, text, digest, phase) VALUES(?,?,?,?,?)',
                            (identity, thread, text, digest, 'prepared'))
        return self.submission(identity), True

    def submission(self, identity):
        row = self.db.execute('SELECT * FROM submissions WHERE id=?', (identity,)).fetchone()
        return dict(row) if row else None

    def phase(self, identity, phase, turn=None):
        with self.db:
            self.db.execute('UPDATE submissions SET phase=?, turn=COALESCE(?,turn) WHERE id=?', (phase, turn, identity))

    def for_thread(self, thread):
        return [dict(r) for r in self.db.execute('SELECT * FROM submissions WHERE thread=? ORDER BY created,rowid', (thread,))]

    def save_turn(self, thread, turn):
        with self.db:
            self.db.execute('INSERT INTO turns(id,thread,snapshot) VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET snapshot=excluded.snapshot',
                            (turn['id'], thread, json.dumps(turn)))

    def record_event(self, thread, packet):
        method = packet['method']; params = packet.get('params', {})
        if method not in ('turn/started', 'turn/completed', 'item/started', 'item/completed', 'item/agentMessage/delta'):
            return None
        turn_id = params.get('turnId') or params.get('turn', {}).get('id')
        row = self.db.execute('SELECT snapshot FROM turns WHERE id=?', (turn_id,)).fetchone() if turn_id else None
        if not row:
            row = self.db.execute('SELECT snapshot FROM turns WHERE thread=? ORDER BY created DESC,rowid DESC LIMIT 1', (thread,)).fetchone()
        turn = json.loads(row['snapshot']) if row else None
        if not turn or (turn_id and turn['id'] != turn_id):
            turn = {'id': turn_id, 'status': 'inProgress', 'items': []}
        if not turn['id']:
            return None
        items = {i['id']: i for i in turn.get('items', [])}
        if method.startswith('turn/'):
            incoming = params['turn']
            for item in incoming.get('items', []):
                items[item['id']] = merge_items([items[item['id']]], [item])[0] if item['id'] in items else item
            turn.update({k: v for k, v in incoming.items() if k != 'items'})
        elif method == 'item/agentMessage/delta':
            item = items.setdefault(params['itemId'], {'id': params['itemId'], 'type': 'agentMessage', 'text': ''})
            item['text'] += params['delta']
        else:
            item = params['item']
            items.setdefault(item['id'], {}).update(item)
        turn['items'] = list(items.values())
        with self.db:
            self.db.execute('INSERT INTO events(thread,packet) VALUES(?,?)', (thread, json.dumps(packet)))
            self.db.execute('INSERT INTO turns(id,thread,snapshot) VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET snapshot=excluded.snapshot',
                            (turn['id'], thread, json.dumps(turn)))
        return turn

    def turns(self, thread=None):
        query = 'SELECT * FROM turns' + (' WHERE thread=?' if thread else '') + ' ORDER BY created,rowid'
        return [{**dict(row), 'snapshot': json.loads(row['snapshot'])} for row in self.db.execute(query, (thread,) if thread else ())]

    def stopped(self, turn):
        with self.db:
            self.db.execute('UPDATE turns SET stopped=1 WHERE id=?', (turn,))

    def reviewed(self, turn):
        with self.db:
            self.db.execute('UPDATE turns SET reviewed=1 WHERE id=?', (turn,))

    def close(self):
        self.db.close()

    def prepare_review(self, request, thread, baseline):
        with self.db:
            for row in self.db.execute('SELECT request,baseline FROM work_reviews WHERE result IS NULL AND baseline IS NOT NULL').fetchall():
                other = json.loads(row['baseline'])
                if other.get('root') and other.get('root') == baseline.get('root'):
                    baseline['overlap'] = True
                    other['overlap'] = True
                    self.db.execute('UPDATE work_reviews SET baseline=? WHERE request=?', (json.dumps(other), row['request']))
            self.db.execute('INSERT OR IGNORE INTO work_reviews(request,thread,baseline) VALUES(?,?,?)', (request, thread, json.dumps(baseline)))

    def pending_review(self, turn):
        row = self.db.execute('SELECT w.* FROM work_reviews w JOIN submissions s ON s.id=w.request WHERE (s.turn=? OR (s.turn IS NULL AND s.id=?)) AND w.result IS NULL', (turn, turn)).fetchone()
        return {**dict(row), 'baseline': json.loads(row['baseline'])} if row else None

    def finish_review(self, request, result):
        with self.db:
            self.db.execute('UPDATE work_reviews SET result=?,baseline=NULL WHERE request=?', (json.dumps(result), request))

    def review_result(self, turn):
        row = self.db.execute('SELECT w.result FROM work_reviews w JOIN submissions s ON s.id=w.request WHERE s.turn=?', (turn,)).fetchone()
        return json.loads(row['result']) if row and row['result'] else None
