#!/usr/bin/env python3
"""Verify live planning, native patch handling, and tests in a disposable Spark project."""

import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import tomllib

from health import ClientSession, RPC
from tool_instructions import PATCH_GUIDANCE, spark_instructions


PROMPT = (
    'Fix the add function in calculator.py. Start by using update_plan to track the task. '
    'Inspect calculator.py and test_calculator.py. Edit only calculator.py using the patch method '
    'specified in your instructions, then run python3 -m unittest -v. '
    'Mark the plan completed after the tests pass and reply TOOL-PROFILE-OK. '
    'Do not change tests, use the network, or edit files outside this project.'
)


def check_project(project):
    result = subprocess.run(['python3', '-m', 'unittest', '-v'], cwd=project, capture_output=True, text=True)
    return result


async def main(installed):
    home = Path.home()
    parent = home / '.local/state/codex-spark/tool-profile-acceptance'
    parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    root = Path(tempfile.mkdtemp(prefix='installed-' if installed else 'candidate-', dir=parent))
    project = root / 'project'
    project.mkdir()
    (project / 'calculator.py').write_text('def add(a, b):\n    return a - b\n')
    test_source = 'import unittest\nfrom calculator import add\n\nclass AdditionTests(unittest.TestCase):\n    def test_positive(self):\n        self.assertEqual(add(2, 3), 5)\n    def test_mixed(self):\n        self.assertEqual(add(-2, 3), 1)\n'
    (project / 'test_calculator.py').write_text(test_source)
    baseline = check_project(project)
    assert baseline.returncode != 0
    (root / 'baseline-tests.txt').write_text(baseline.stdout + baseline.stderr)
    runtime = home / '.codex-spark'
    config_path = runtime / 'config.toml'
    config_hash = hashlib.sha256(config_path.read_bytes()).hexdigest()
    config = tomllib.loads(config_path.read_text())
    catalog = json.loads(Path(config['model_catalog_json']).read_text())
    model = next(m for m in catalog['models'] if m['slug'] == config['model'])
    print(json.dumps({'root': str(root), 'installed': installed}), flush=True)
    if installed:
        assert config['tools']['update_plan']['enabled'] is True
        assert PATCH_GUIDANCE in model['model_messages']['instructions_template']
        token = (home / '.config/codex-spark/server.token').read_text().strip()
        async with ClientSession() as session:
            async with RPC(session, 'ws://127.0.0.1:4500', token) as rpc:
                effective = (await rpc.call('config/read', {'includeLayers': False}))['config']
                assert effective['model'] == config['model']
                assert effective['model_catalog_json'] == config['model_catalog_json']
                created = await rpc.call('thread/start', {'cwd': str(project), 'approvalPolicy': 'never', 'sandbox': 'workspace-write', 'ephemeral': True})
                identity = created['thread']['id']
                try:
                    await rpc.call('turn/start', {'threadId': identity, 'input': [{'type': 'text', 'text': PROMPT}],
                        'sandboxPolicy': {'type': 'workspaceWrite', 'writableRoots': [str(project)], 'networkAccess': False,
                                          'excludeTmpdirEnvVar': True, 'excludeSlashTmp': True}})
                    async with asyncio.timeout(180):
                        while not any(e.get('method') == 'turn/completed' and e.get('params', {}).get('threadId') == identity for e in rpc.events):
                            packet = await rpc.ws.receive_json()
                            if packet.get('method') and 'id' in packet:
                                raise AssertionError('Unexpected approval/input request')
                            rpc.events.append(packet)
                    events = rpc.events
                    terminal = next(e['params']['turn'] for e in reversed(events) if e.get('method') == 'turn/completed')
                    assert terminal['status'] == 'completed', terminal
                    plans = [e['params']['plan'] for e in events if e.get('method') == 'turn/plan/updated']
                    changes = [e['params']['item'] for e in events if e.get('method') == 'item/completed' and e.get('params', {}).get('item', {}).get('type') == 'fileChange']
                    assert changes, events
                    answer = ''.join(i['text'] for i in terminal['items'] if i['type'] == 'agentMessage')
                    assert 'TOOL-PROFILE-OK' in answer, answer
                finally:
                    (root / 'events.json').write_text(json.dumps(rpc.events, indent=2))
                    await rpc.call('thread/unsubscribe', {'threadId': identity})
    else:
        isolated = root / 'runtime'
        isolated.mkdir()
        shutil.copyfile(config_path, isolated / 'config.toml')
        model['model_messages']['instructions_template'] = spark_instructions(model['model_messages']['instructions_template'])
        (isolated / 'models.json').write_text(json.dumps(catalog))
        args = [str(home / '.local/lib/codex-spark/codex'), 'exec', '--json', '--sandbox', 'workspace-write', '--skip-git-repo-check', '--cd', str(project),
                '-c', 'tools.update_plan.enabled=true', '-c', 'model_catalog_json=' + json.dumps(str(isolated / 'models.json')),
                '-c', 'features.hooks=false', '-c', 'sandbox_workspace_write.network_access=false', PROMPT]
        proc = await asyncio.create_subprocess_exec(*args, env={**os.environ, 'CODEX_HOME': str(isolated)},
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), 180)
        except TimeoutError:
            proc.kill()
            await proc.wait()
            raise
        (root / 'stdout.jsonl').write_bytes(stdout)
        (root / 'stderr.txt').write_bytes(stderr)
        assert proc.returncode == 0, stderr.decode()
        events = [json.loads(line) for line in stdout.decode().splitlines() if line.startswith('{')]
        plans = [e['item']['items'] for e in events if e.get('item', {}).get('type') == 'todo_list']
        changes = [e['item'] for e in events if e.get('item', {}).get('type') == 'file_change']
        assert changes, events
        identity = next(e['thread_id'] for e in events if e['type'] == 'thread.started')
        rollout = next((isolated / 'sessions').rglob('*' + identity + '.jsonl'))
        items = [r['payload'] for line in rollout.read_text().splitlines() if (r := json.loads(line)).get('type') == 'response_item']
        calls = [i for i in items if i.get('type') == 'function_call']
        patch_call = next(i for i in calls if i['name'] == 'exec_command' and 'apply_patch' in json.loads(i['arguments']).get('cmd', ''))
        assert any(i.get('type') == 'function_call_output' and i.get('call_id') == patch_call['call_id'] and 'Success' in str(i.get('output')) for i in items), items
        assert all(i['name'] != 'apply_patch' for i in calls)
    assert plans and plans[-1] and all(step.get('completed', step.get('status') == 'completed') for step in plans[-1]), plans
    assert (project / 'test_calculator.py').read_text() == test_source
    final = check_project(project)
    (root / 'final-tests.txt').write_text(final.stdout + final.stderr)
    assert final.returncode == 0, final.stderr
    assert config_hash == hashlib.sha256(config_path.read_bytes()).hexdigest()
    (root / 'events.json').write_text(json.dumps(events, indent=2))
    report = {'root': str(root), 'installed': installed, 'model': config['model'], 'planning': True,
              'finalPlanCompleted': True, 'nativePatchEvents': len(changes), 'fixtureTestsPassed': True,
              'testSourceUnchanged': True, 'productionConfigUnchangedDuringTest': True, 'passed': True}
    (root / 'report.json').write_text(json.dumps(report, indent=2))
    print(json.dumps(report), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--installed', action='store_true', help='Test the deployed app-server without profile overrides')
    asyncio.run(main(parser.parse_args().installed))
