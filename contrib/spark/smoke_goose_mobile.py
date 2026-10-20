#!/usr/bin/env python3
"""Bounded live acceptance for Goose mobile -> Codex -> Spark."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, str(Path.home() / '.local/share/codex-spark/python'))
import websocket


class Client:
    def __init__(self):
        token = (Path.home() / '.config/codex-spark/mobile.token').read_text().strip()
        self.ws = websocket.create_connection('ws://127.0.0.1:3285/acp', header=['X-Secret-Key: ' + token], suppress_origin=True, timeout=180)
        self.number = 0
        self.events = []
        result = self.call('initialize', {'protocolVersion': 1, 'clientCapabilities': {}})
        assert result['agentInfo']['name'] == 'Codex Spark'

    def start(self, method, params):
        self.number += 1
        self.ws.send(json.dumps({'jsonrpc': '2.0', 'id': self.number, 'method': method, 'params': params}))
        return self.number

    def receive(self):
        packet = json.loads(self.ws.recv()); self.events.append(packet)
        if 'method' in packet and 'id' in packet:
            self.ws.send(json.dumps({'jsonrpc': '2.0', 'id': packet['id'], 'result': {'outcome': {'outcome': 'cancelled'}}}))
            raise AssertionError('Unexpected permission request in acceptance sandbox')
        return packet

    def finish(self, number):
        while True:
            packet = self.receive()
            if packet.get('id') == number:
                if 'error' in packet:
                    raise RuntimeError(str(packet['error']))
                return packet['result']

    def call(self, method, params):
        return self.finish(self.start(method, params))

    def prompt(self, identity, text):
        return self.call('session/prompt', {'sessionId': identity, 'prompt': [{'type': 'text', 'text': text}]})


def main():
    root = Path.home() / '.local/state/codex-spark/mobile-acceptance'
    root.mkdir(exist_ok=True)
    fixture = Path(tempfile.mkdtemp(prefix='coding-', dir=root))
    (fixture / 'calculator.py').write_text('def add(a, b):\n    return a - b\n')
    test = 'import unittest\nfrom calculator import add\nclass Tests(unittest.TestCase):\n def test_add(self): self.assertEqual(add(2, 3), 5)\n def test_negative(self): self.assertEqual(add(-2, 3), 1)\n'
    (fixture / 'test_calculator.py').write_text(test)
    try:
        unauthorized = websocket.create_connection('ws://127.0.0.1:3285/acp', suppress_origin=True, timeout=5)
    except websocket.WebSocketBadStatusException as error:
        assert error.status_code == 401
    else:
        unauthorized.close(); raise AssertionError('Missing secret accepted')
    client = Client()
    identity = client.call('session/new', {'cwd': str(fixture), 'mcpServers': [], '_meta': {'workspace': {'kind': 'project', 'path': str(fixture)}}})['sessionId']
    print(json.dumps({'fixture': str(fixture), 'thread': identity}), flush=True)
    result = client.prompt(identity, 'Fix calculator.py add. Inspect the files, run python3 -m unittest -v to reproduce the failure, change only calculator.py, and rerun the tests using the terminal. Report results. Remember MOBILE-CODEX-583 for a follow-up.')
    assert result['stopReason'] == 'end_turn', result
    output = subprocess.run(['python3', '-m', 'unittest', '-v'], cwd=fixture, capture_output=True, text=True)
    assert output.returncode == 0, output.stderr
    assert (fixture / 'test_calculator.py').read_text() == test
    assert any(e.get('params', {}).get('update', {}).get('sessionUpdate') == 'tool_call' for e in client.events)
    print('PASS actual coding task, tool streaming and independent tests', flush=True)
    client.call('_goose/unstable/session/rename', {'sessionId': identity, 'title': 'Codex Spark mobile acceptance'})
    client.ws.close()
    client = Client()
    loaded = client.call('session/load', {'sessionId': identity, 'cwd': str(fixture), 'mcpServers': []})
    assert any(r['state'] == 'finished' and r['finalMessageId'] for r in loaded['_meta']['hostRuns'])
    assert any(e.get('params', {}).get('update', {}).get('content', {}).get('text') for e in client.events)
    result = client.prompt(identity, 'What code did I ask you to remember and what function did you fix? Do not use tools.')
    assert result['stopReason'] == 'end_turn'
    assert any('MOBILE-CODEX-583' in e.get('params', {}).get('update', {}).get('content', {}).get('text', '') for e in client.events)
    print('PASS reconnect, replay, model continuity and final-answer metadata', flush=True)
    query = client.call('session/list', {'_meta': {'query': 'MOBILE-CODEX-583'}})
    assert any(s['sessionId'] == identity for s in query['sessions']), query
    print('PASS saved-message search', flush=True)
    number = client.start('session/prompt', {'sessionId': identity, 'prompt': [{'type': 'text', 'text': "Use the terminal to run this exact Python command once: python3 -c \"import time; time.sleep(10); open('continuity.txt', 'a').write('mobile-once\\n')\". Then reply MOBILE-HOST-COMPLETE. This is a bounded disconnect test."}]})
    while True:
        packet = client.receive()
        if packet.get('params', {}).get('update', {}).get('sessionUpdate') == 'tool_call':
            break
        if packet.get('id') == number:
            raise AssertionError('No tool started for disconnect check')
    client.ws.close()
    second = Client()
    replay = second.call('session/load', {'sessionId': identity, 'cwd': str(fixture), 'mcpServers': []})
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        states = replay['_meta']['hostRuns']
        if states and states[-1]['state'] == 'finished':
            break
        time.sleep(1)
        replay = second.call('session/load', {'sessionId': identity, 'cwd': str(fixture), 'mcpServers': []})
    else:
        raise AssertionError('Disconnected task did not finish')
    assert (fixture / 'continuity.txt').read_text() == 'mobile-once\n'
    print('PASS phone disconnect leaves host task running with one tool execution', flush=True)
    second.call('_goose/unstable/session/archive', {'sessionId': identity})
    second.call('_goose/unstable/session/unarchive', {'sessionId': identity})
    second.ws.close()
    report = {'thread': identity, 'fixture': str(fixture), 'authentication': True, 'coding_tests': True, 'tool_streaming': True, 'reconnect_replay': True, 'continuity': True, 'message_search': True, 'disconnect_host_work': True, 'archive_restore': True}
    (root / 'result.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report, indent=2), flush=True)


if __name__ == '__main__':
    main()
