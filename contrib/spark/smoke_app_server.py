#!/usr/bin/env python3
"""Exercise real file edits, shell execution, and resume through the app-server."""

import datetime
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, str(Path.home() / '.local/share/codex-spark/python'))
import websocket


class Client:
    def __init__(self, token):
        self.socket = websocket.create_connection(
            'ws://127.0.0.1:4500',
            header=['Authorization: Bearer ' + token],
            suppress_origin=True,
            timeout=180,
        )
        self.number = 0
        self.events = []
        self.call('initialize', {'clientInfo': {'name': 'codex_spark_acceptance', 'title': 'Codex Spark acceptance', 'version': '1.0.0'}})
        self.send({'method': 'initialized'})

    def send(self, message):
        self.socket.send(json.dumps(message))

    def receive(self):
        message = json.loads(self.socket.recv())
        self.events.append(message)
        if 'method' in message and 'id' in message:
            # No approval for leaving the fixture sandbox is granted by this test.
            self.send({'id': message['id'], 'result': {'decision': 'decline'}})
            raise RuntimeError('Unexpected approval/input request: ' + message['method'])
        if message.get('method') == 'error':
            print(json.dumps(message), flush=True)
        return message

    def call(self, method, params):
        self.number += 1
        number = self.number
        self.send({'id': number, 'method': method, 'params': params})
        while True:
            message = self.receive()
            if message.get('id') == number:
                if 'error' in message:
                    raise RuntimeError(json.dumps(message['error']))
                return message['result']

    def turn(self, thread, prompt):
        start = len(self.events)
        self.call('turn/start', {'threadId': thread, 'input': [{'type': 'text', 'text': prompt}]})
        deadline = time.monotonic() + 240
        while time.monotonic() < deadline:
            message = self.receive()
            if message.get('method') == 'item/completed':
                item = message.get('params', {}).get('item', {})
                if item.get('type') in ('commandExecution', 'fileChange', 'agentMessage'):
                    print(json.dumps({'item': item}), flush=True)
            if message.get('method') == 'turn/completed':
                turn = message['params']['turn']
                if turn['status'] != 'completed':
                    raise RuntimeError(json.dumps(turn))
                return self.events[start:]
        raise TimeoutError('Turn did not complete within 240 seconds')


def main():
    state = Path.home() / '.local/state/codex-spark'
    fixtures = state / 'acceptance'
    fixtures.mkdir(parents=True, exist_ok=True)
    fixture = Path(tempfile.mkdtemp(prefix='coding-', dir=fixtures))
    (fixture / 'calculator.py').write_text('def add(a, b):\n    return a - b\n')
    (fixture / 'test_calculator.py').write_text('''import unittest
from calculator import add

class AdditionTests(unittest.TestCase):
    def test_positive(self):
        self.assertEqual(add(2, 3), 5)
    def test_mixed_signs(self):
        self.assertEqual(add(-2, 3), 1)

if __name__ == '__main__':
    unittest.main()
''')
    subprocess.run(['git', 'init', '-q', str(fixture)], check=True)
    original_tests = (fixture / 'test_calculator.py').read_text()
    baseline = subprocess.run(['python3', '-m', 'unittest', '-v'], cwd=fixture, capture_output=True, text=True)
    assert baseline.returncode != 0, 'Fixture must initially fail'
    token = (Path.home() / '.config/codex-spark/server.token').read_text().strip()
    client = Client(token)
    result = client.call('thread/start', {'cwd': str(fixture), 'approvalPolicy': 'on-request', 'sandbox': 'workspace-write'})
    thread = result['thread']['id']
    print(json.dumps({'thread': thread, 'fixture': str(fixture), 'provider': result['thread'].get('modelProvider')}), flush=True)
    events = client.turn(thread, 'Fix the add function in calculator.py. Inspect the files, reproduce the failing tests with python3 -m unittest -v, fix only calculator.py, and rerun the tests. Use the terminal tools to do the work. Report the test result.')
    client.socket.close()
    after = subprocess.run(['python3', '-m', 'unittest', '-v'], cwd=fixture, capture_output=True, text=True)
    assert (fixture / 'test_calculator.py').read_text() == original_tests, 'Tests must not be changed'
    assert after.returncode == 0, after.stdout + after.stderr
    assert any(e.get('method') == 'item/completed' and e.get('params', {}).get('item', {}).get('type') == 'commandExecution' for e in events), 'Expected real shell tool use'
    resumed = Client(token)
    resumed.call('thread/resume', {'threadId': thread})
    followup = resumed.turn(thread, 'Verify your previous fix by running python3 -m unittest -v again. Then say which file and function you fixed. Do not change any files this turn.')
    resumed.socket.close()
    assert any(e.get('method') == 'item/completed' and e.get('params', {}).get('item', {}).get('type') == 'commandExecution' for e in followup), 'Follow-up must run a tool'
    report = {
        'completed_at': datetime.datetime.now(datetime.timezone.utc).isoformat(),
        'fixture': str(fixture), 'thread_id': thread, 'initial_tests_failed': True,
        'corrected_tests_passed': True, 'websocket_reconnect_and_resume_passed': True,
        'first_turn_events': len(events), 'followup_events': len(followup),
        'independent_test_output': after.stdout + after.stderr,
    }
    (fixture / 'events.json').write_text(json.dumps(events + followup, indent=2))
    (fixture / 'result.json').write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2), flush=True)


if __name__ == '__main__':
    main()
