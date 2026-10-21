#!/usr/bin/env python3
"""Verify detection, native images, view_image, and durable mobile images on Spark."""

import asyncio
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import signal
import socket
import tempfile
import tomllib
import uuid

from goose_mobile import Bridge, app
from model_capabilities import image_probe, refresh
from smoke_thread_titles import Phone
from aiohttp import ClientSession, web


def colors(text):
    return re.findall(r'\b(?:blue|red|green)\b', text.lower())


async def main():
    home = Path.home()
    parent = home / '.local/state/codex-spark/capability-acceptance'
    parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    root = Path(tempfile.mkdtemp(prefix='vision-', dir=parent))
    runtime = root / 'runtime'
    runtime.mkdir()
    project = root / 'project'
    project.mkdir()
    original = home / '.codex-spark/config.toml'
    config_hash = hashlib.sha256(original.read_bytes()).hexdigest()
    config = tomllib.loads(original.read_text())
    catalog = runtime / 'models.json'
    shutil.copyfile(Path(config['model_catalog_json']).expanduser(), catalog)
    text, count = re.subn(r'^model_catalog_json\s*=.*$', 'model_catalog_json = ' + json.dumps(str(catalog)), original.read_text(), flags=re.M)
    assert count == 1
    (runtime / 'config.toml').write_text(text)
    detected = await asyncio.to_thread(refresh, runtime, True)
    assert detected['input_modalities'] == ['text', 'image'], detected
    png, expected = image_probe()
    image_file = project / 'panels.png'
    image_file.write_bytes(png)
    question = 'Name the three colored panels from left to right. Reply only with the three color names, separated by spaces.'
    binary = home / '.local/lib/codex-spark/codex'
    env = {**os.environ, 'CODEX_HOME': str(runtime)}
    report = {'root': str(root), 'detected': detected, 'expected': expected, 'checks': {}}
    print(json.dumps({'root': str(root)}), flush=True)

    for case in ('attachment', 'view_image'):
        args = [str(binary), 'exec', '--json', '--sandbox', 'read-only', '--skip-git-repo-check', '--cd', str(project),
                '-c', 'features.hooks=false', '-c', 'features.shell_tool=false']
        if case == 'attachment':
            args += ['--ephemeral', '--image', str(image_file), '--', question + ' Do not use tools.']
        else:
            args += ['Use the view_image tool to inspect ' + str(image_file) + '. ' + question]
        proc = await asyncio.create_subprocess_exec(*args, env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, start_new_session=True)
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), 120)
        except TimeoutError:
            os.killpg(proc.pid, signal.SIGKILL)
            await proc.wait()
            raise
        (root / (case + '.jsonl')).write_bytes(stdout)
        (root / (case + '.stderr')).write_bytes(stderr)
        events = [json.loads(line) for line in stdout.decode().splitlines() if line.startswith('{')]
        answer = next((e['item']['text'] for e in reversed(events) if e.get('item', {}).get('type') == 'agent_message'), '')
        assert proc.returncode == 0 and colors(answer) == expected, (case, stdout.decode(), stderr.decode())
        assert 'not allowed because you do not support image inputs' not in stderr.decode()
        if case == 'view_image':
            # Exec JSON omits view_image tool notifications; inspect this isolated rollout.
            thread_id = next(e['thread_id'] for e in events if e['type'] == 'thread.started')
            rollout = next((runtime / 'sessions').rglob('*' + thread_id + '.jsonl'))
            items = [r['payload'] for line in rollout.read_text().splitlines()
                     if (r := json.loads(line)).get('type') == 'response_item']
            call = next(i for i in items if i.get('type') == 'function_call' and i.get('name') == 'view_image')
            assert any(i.get('type') == 'function_call_output' and i.get('call_id') == call['call_id']
                       and 'input_image' in json.dumps(i) for i in items), items
        report['checks'][case] = {'passed': True, 'answer': answer, 'eventTypes': [e['type'] for e in events]}
        print(json.dumps({case: report['checks'][case]}), flush=True)

    token = root / 'token'
    token.write_text(secrets.token_urlsafe(32))
    token.chmod(0o600)
    with socket.socket() as reserve:
        reserve.bind(('127.0.0.1', 0))
        port = reserve.getsockname()[1]
    upstream = f'ws://127.0.0.1:{port}'
    server_log = (root / 'server.stderr').open('wb')
    server = await asyncio.create_subprocess_exec(str(binary), '-c', 'features.hooks=false', '-c', 'features.shell_tool=false', 'app-server', '--strict-config',
        '--listen', upstream, '--ws-auth', 'capability-token', '--ws-token-file', str(token), env=env,
        stdout=asyncio.subprocess.DEVNULL, stderr=server_log, start_new_session=True)
    runner = bridge = None
    try:
        async with ClientSession() as session:
            identity = None
            for attempt in range(2):
                bridge = Bridge(root / 'adapter', token, token, upstream_url=upstream, push_config=root / 'unconfigured-push.json')
                runner = web.AppRunner(app(bridge))
                await runner.setup()
                await web.TCPSite(runner, '127.0.0.1', 0).start()
                url = f'http://127.0.0.1:{runner.addresses[0][1]}/acp'
                await asyncio.wait_for(bridge.rpc.ready.wait(), 30)
                phone = await Phone(session, url, token.read_text()).connect()
                handshake = await phone.call('initialize', {'protocolVersion': 1})
                assert handshake['agentCapabilities']['promptCapabilities'].get('image') is True
                if not attempt:
                    identity = (await phone.call('session/new', {'cwd': str(project), 'mcpServers': []}))['sessionId']
                    bridge.metadata['sessions'][identity]['fullAccess'] = False
                    bridge.save()
                    params = {'sessionId': identity, '_meta': {'goose': {'requestId': 'vision-' + uuid.uuid4().hex}},
                              'prompt': [{'type': 'text', 'text': question + ' Do not use tools.'},
                                         {'type': 'image', 'mimeType': 'image/png', 'data': base64.b64encode(png).decode()}]}
                    result = await phone.call('session/prompt', params)
                    assert result['stopReason'] == 'end_turn', result
                    async with asyncio.timeout(90):
                        while bridge.titles.pending:
                            await asyncio.sleep(.1)
                loaded = await phone.call('session/load', {'sessionId': identity})
                native = (await bridge.rpc.request('thread/read', {'threadId': identity, 'includeTurns': True}))['thread']
                assert len(native['turns']) == 1, native
                answer = next(i['text'] for i in reversed(native['turns'][0]['items']) if i['type'] == 'agentMessage')
                assert colors(answer) == expected, answer
                updates = [p['params']['update'] for p in phone.events if p.get('method') == 'session/update']
                assert any(u.get('content', {}).get('type') == 'image' and u['content']['data'] == base64.b64encode(png).decode() for u in updates), updates
                assert (await phone.call('session/prompt', params))['stopReason'] == 'end_turn'
                again = (await bridge.rpc.request('thread/read', {'threadId': identity, 'includeTurns': True}))['thread']
                assert len(again['turns']) == 1
                report['checks']['mobile' if not attempt else 'mobile_after_adapter_restart'] = {'passed': True, 'answer': answer,
                    'imageAdvertised': True, 'imageReplay': True, 'duplicateReceipt': True, 'visibleTurns': 1}
                print(json.dumps({'mobileAttempt': attempt, 'passed': True, 'answer': answer}), flush=True)
                await phone.close()
                if attempt:
                    await bridge.rpc.request('thread/archive', {'threadId': identity})
                await runner.cleanup()
                runner = None
                bridge.journal.close()
                bridge = None
    finally:
        if runner:
            await runner.cleanup()
        if bridge:
            bridge.journal.close()
        if server.returncode is None:
            os.killpg(server.pid, signal.SIGINT)
            try:
                await asyncio.wait_for(server.wait(), 10)
            except TimeoutError:
                os.killpg(server.pid, signal.SIGKILL)
                await server.wait()
        server_log.close()
    report['productionConfigUnchanged'] = config_hash == hashlib.sha256(original.read_bytes()).hexdigest()
    assert report['productionConfigUnchanged']
    (root / 'report.json').write_text(json.dumps(report, indent=2))
    print(json.dumps({'root': str(root), 'checks': report['checks']}), flush=True)


if __name__ == '__main__':
    asyncio.run(main())
