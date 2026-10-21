#!/usr/bin/env python3
"""Verify reasoning selection through native Codex, with tools disabled in disposable chats."""

import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
import tomllib

from health import ClientSession, RPC
from model_reasoning import refresh
from thread_titles import isolated_config


PROMPT = 'Do not use tools. Compute 17 multiplied by 19. If thinking is enabled, reason before answering. Reply only with the integer.'


async def main(installed):
    home = Path.home()
    production = home / '.codex-spark'
    config_path = production / 'config.toml'
    config_hash = hashlib.sha256(config_path.read_bytes()).hexdigest()
    config = tomllib.loads(config_path.read_text())
    catalog_path = Path(config['model_catalog_json'])
    catalog_hash = hashlib.sha256(catalog_path.read_bytes()).hexdigest()
    parent = home / '.local/state/codex-spark/reasoning-acceptance'
    parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    root = Path(tempfile.mkdtemp(prefix='installed-' if installed else 'candidate-', dir=parent))
    project = root / 'project'
    project.mkdir()
    print(json.dumps({'root': str(root), 'installed': installed}), flush=True)
    if installed:
        catalog = json.loads(catalog_path.read_text())
    else:
        runtime = root / 'runtime'
        runtime.mkdir()
        shutil.copy2(catalog_path, runtime / 'models.json')
        config_text = re.sub(r'^model_catalog_json\s*=.*$', 'model_catalog_json = ' + json.dumps(str(runtime / 'models.json')),
                             config_path.read_text(), count=1, flags=re.M)
        (runtime / 'config.toml').write_text(config_text)
        refresh(runtime, apply=True)
        catalog = json.loads((runtime / 'models.json').read_text())
    model = next(model for model in catalog['models'] if model['slug'] == config['model'])
    levels = [option['effort'] for option in model['supported_reasoning_levels']]
    assert levels == ['none', 'low', 'medium', 'xhigh'], levels
    assert model['default_reasoning_level'] is None
    default_effort = config.get('model_reasoning_effort') or model.get('default_reasoning_level')
    report = {'root': str(root), 'installed': installed, 'levels': levels,
              'defaultEffort': default_effort or 'none', 'cases': {}}
    overrides = isolated_config(config)
    async with ClientSession() as session:
        if installed:
            token = (home / '.config/codex-spark/server.token').read_text().strip()
            async with RPC(session, 'ws://127.0.0.1:4500', token) as rpc:
                listed = (await rpc.call('model/list', {'limit': 100, 'includeHidden': True}))['data']
                entry = next(model for model in listed if model['model'] == config['model'])
                assert [option['reasoningEffort'] for option in entry['supportedReasoningEfforts']] == levels, entry
                assert entry['defaultReasoningEffort'] == 'none', entry
                report['modelList'] = entry
                for level in ['default', *levels]:
                    created = await rpc.call('thread/start', {'cwd': str(project), 'sandbox': 'read-only',
                                            'approvalPolicy': 'never', 'ephemeral': True, 'config': overrides})
                    identity = created['thread']['id']
                    start = len(rpc.events)
                    try:
                        params = {'threadId': identity, 'input': [{'type': 'text', 'text': PROMPT}]}
                        if level != 'default':
                            params['effort'] = level
                        await rpc.call('turn/start', params)
                        async with asyncio.timeout(120):
                            while not any(event.get('method') == 'turn/completed' and event.get('params', {}).get('threadId') == identity for event in rpc.events[start:]):
                                packet = await rpc.ws.receive_json()
                                assert not ('id' in packet and packet.get('method')), packet
                                rpc.events.append(packet)
                        events = rpc.events[start:]
                        (root / (level + '-events.json')).write_text(json.dumps(events, indent=2))
                        turn = next(event['params']['turn'] for event in reversed(events) if event.get('method') == 'turn/completed')
                        assert turn['status'] == 'completed', turn
                        answer = ''.join(item.get('text', '') for item in turn['items'] if item['type'] == 'agentMessage')
                        usage = next(event['params']['tokenUsage']['last'] for event in reversed(events) if event.get('method') == 'thread/tokenUsage/updated')
                        tokens = usage['reasoningOutputTokens']
                        assert answer.strip() == '323', answer
                        selected = (default_effort or 'none') if level == 'default' else level
                        assert (tokens == 0) == (selected == 'none'), usage
                        report['cases'][level] = {'answer': answer.strip(), 'reasoningTokens': tokens, 'status': turn['status']}
                        print(json.dumps({level: report['cases'][level]}), flush=True)
                    finally:
                        await rpc.call('thread/unsubscribe', {'threadId': identity})
        else:
            from aiohttp import web
            captures = {}

            async def handler(request):
                level = request.match_info['level']
                payload = await request.json()
                assert not payload['tools'], payload['tools']
                captures.setdefault(level, []).append(payload)
                assert payload.get('reasoning', {}).get('effort') == (default_effort if level == 'default' else level), payload.get('reasoning')
                async with session.post(config['model_providers']['spark']['base_url'].rstrip('/') + '/responses',
                                        json={**payload, 'max_output_tokens': 2048}) as response:
                    raw = await response.read()
                    events = [json.loads(line[5:].strip()) for line in raw.decode().splitlines()
                              if line.startswith('data:') and line[5:].strip() != '[DONE]'] if response.status == 200 else []
                    assert not any(event.get('item', {}).get('type') in ('function_call', 'custom_tool_call') for event in events), events
                    (root / (level + '-provider-events.json')).write_text(json.dumps(events, indent=2))
                    completed = next((event['response'] for event in events if event.get('type') == 'response.completed'), {})
                    report['cases'][level] = {'wireEffort': payload.get('reasoning', {}).get('effort'),
                                             'httpStatus': response.status, 'status': completed.get('status'),
                                             'usage': completed.get('usage')}
                    return web.Response(body=raw, status=response.status, content_type='text/event-stream' if response.status == 200 else 'application/json')

            app = web.Application()
            app.router.add_post('/{level}/v1/responses', handler)
            runner = web.AppRunner(app, access_log=None)
            await runner.setup()
            await web.TCPSite(runner, '127.0.0.1', 0).start()
            port = runner.addresses[0][1]
            try:
                for level in ['default', *levels]:
                    isolated = root / level
                    isolated.mkdir()
                    shutil.copy2(runtime / 'config.toml', isolated / 'config.toml')
                    args = [str(home / '.local/lib/codex-spark/codex'), 'exec', '--ephemeral', '--json',
                            '--sandbox', 'read-only', '--skip-git-repo-check', '--cd', str(project),
                            '-c', f'model_providers.spark.base_url="http://127.0.0.1:{port}/{level}/v1"']
                    for key, value in overrides.items():
                        if key != 'mcp_servers':
                            args += ['-c', key + '=' + json.dumps(value)]
                    if level != 'default':
                        args += ['-c', 'model_reasoning_effort=' + json.dumps(level)]
                    proc = await asyncio.create_subprocess_exec(*args, PROMPT, env={**os.environ, 'CODEX_HOME': str(isolated)},
                        stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
                    try:
                        stdout, stderr = await asyncio.wait_for(proc.communicate(), 120)
                    except TimeoutError:
                        proc.kill()
                        await proc.wait()
                        raise
                    (root / (level + '-stdout.jsonl')).write_bytes(stdout)
                    (root / (level + '-stderr.txt')).write_bytes(stderr)
                    (root / (level + '-requests.json')).write_text(json.dumps(captures.get(level, []), indent=2))
                    assert proc.returncode == 0, stderr.decode()
                    events = [json.loads(line) for line in stdout.decode().splitlines() if line.startswith('{')]
                    answer = ''.join(event['item']['text'] for event in events if event.get('item', {}).get('type') == 'agent_message')
                    case = report['cases'][level]
                    assert case['status'] == 'completed' and answer.strip() == '323', case
                    tokens = case['usage']['output_tokens_details']['reasoning_tokens']
                    selected = (default_effort or 'none') if level == 'default' else level
                    assert (tokens == 0) == (selected == 'none'), case
                    case['answer'] = answer.strip()
                    print(json.dumps({level: case}), flush=True)
            finally:
                await runner.cleanup()
    assert not list(project.iterdir())
    assert hashlib.sha256(config_path.read_bytes()).hexdigest() == config_hash
    assert hashlib.sha256(catalog_path.read_bytes()).hexdigest() == catalog_hash
    report.update({'passed': True, 'productionProfileUnchangedDuringTest': True, 'projectFiles': []})
    (root / 'report.json').write_text(json.dumps(report, indent=2))
    print(json.dumps(report), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--installed', action='store_true', help='Verify the deployed app-server without catalog or reasoning default overrides')
    asyncio.run(main(parser.parse_args().installed))
