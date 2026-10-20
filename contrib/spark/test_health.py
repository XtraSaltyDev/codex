import asyncio
import json
from pathlib import Path
import tempfile
import subprocess
import unittest
from unittest.mock import AsyncMock, patch

from aiohttp import ClientSession, web
from aiohttp.test_utils import TestClient, TestServer

from doctor import live_test, verify_command
from goose_mobile import Bridge
from health import SparkHealth, check, report
from mobile_state import MobileState
import install_diagnostics


class HealthTests(unittest.IsolatedAsyncioTestCase):
    async def test_installed_doctor_dispatches_before_token_read_and_preserves_normal_arguments(self):
        launcher = self.home / '.local/bin/codex-spark'
        launcher.parent.mkdir(parents=True)
        launcher.write_text('#!/bin/sh\nset -eu\nTOKEN=$(cat ' + str(self.home / 'missing-token') + ')\nexec /bin/echo normal "$@"\n')
        launcher.chmod(0o755)
        with patch.object(install_diagnostics.Path, 'home', return_value=self.home):
            install_diagnostics.install()
            install_diagnostics.install()
        help_result = subprocess.run([str(launcher), 'doctor', '--help'], capture_output=True, text=True)
        self.assertEqual(help_result.returncode, 0, help_result.stderr)
        self.assertIn('--live', help_result.stdout)
        (self.home / 'missing-token').write_text('test-token')
        ordinary = subprocess.run([str(launcher), '--cd', '/tmp/example folder'], capture_output=True, text=True)
        self.assertEqual(ordinary.returncode, 0, ordinary.stderr)
        self.assertEqual(ordinary.stdout.strip(), 'normal --cd /tmp/example folder')
        self.assertEqual(launcher.read_text().count('if [ "${1-}" = doctor ]; then'), 1)

    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        config = self.home / '.config/codex-spark'
        config.mkdir(parents=True)
        (config / 'server.token').write_text('private-server-token')
        (config / 'mobile.token').write_text('private-mobile-token')
        state = self.home / '.local/state/codex-spark/mobile'
        state.mkdir(parents=True)
        journal = MobileState(state / 'recovery.sqlite3')
        journal.close()
        self.methods = []
        self.model_requests = 0
        self.model_ids = ['test-model']
        self.reject_auth = False
        app = web.Application()
        app.router.add_get('/v1/models', self.models)
        app.router.add_get('/native', self.native)
        self.server = TestServer(app)
        await self.server.start_server()
        self.addAsyncCleanup(self.server.close)
        self.health = SparkHealth(home=self.home, upstream_url=str(self.server.make_url('/native')).replace('http:', 'ws:'))
        runtime = self.home / '.codex-spark'
        runtime.mkdir()
        (runtime / 'config.toml').write_text('model="test-model"\nmodel_provider="spark"\n[model_providers.spark]\nbase_url=' + json.dumps(str(self.server.make_url('/v1'))) + '\n')
        self.health.services = AsyncMock(return_value=[check('service_tunnel', 'Tunnel', 'ok', 'Running')])

    async def models(self, request):
        self.model_requests += 1
        return web.json_response({'data': [{'id': identity} for identity in self.model_ids]})

    async def native(self, request):
        if self.reject_auth or request.headers.get('Authorization') != 'Bearer private-server-token':
            raise web.HTTPUnauthorized()
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        async for frame in ws:
            packet = json.loads(frame.data)
            self.methods.append(packet['method'])
            if 'id' in packet:
                result = {'config': {'model': 'test-model', 'model_provider': 'spark', 'model_providers': {'spark': {
                    'base_url': str(self.server.make_url('/v1')), 'api_key': 'inference-secret'}}}} if packet['method'] == 'config/read' else {}
                await ws.send_json({'id': packet['id'], 'result': result})
        return ws

    async def test_read_only_checks_authenticate_without_running_inference_or_exposing_secrets(self):
        result = await self.health.collect()
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(self.methods, ['initialize', 'initialized', 'config/read'])
        self.assertEqual(self.model_requests, 1)
        self.assertNotIn('private-server-token', json.dumps(result))
        self.assertNotIn('inference-secret', json.dumps(result))
        self.assertNotIn('base_url', json.dumps(result))
        await self.health.collect()
        self.assertEqual(self.model_requests, 1, 'Routine checks should be briefly cached')

    async def test_reachable_server_with_wrong_model_is_degraded(self):
        self.model_ids = ['different-model']
        result = await self.health.collect()
        self.assertEqual(result['status'], 'degraded')
        self.assertIn('not served', result['summary'])

    async def test_server_auth_failure_is_separate_from_model_availability(self):
        self.reject_auth = True
        result = await self.health.collect()
        checks = {item['id']: item for item in result['checks']}
        self.assertEqual(checks['app_server']['status'], 'error')
        self.assertEqual(checks['spark']['status'], 'ok')

    async def test_private_storage_and_journal_integrity_are_required(self):
        database = self.health.state / 'recovery.sqlite3'
        database.chmod(0o644)
        self.assertEqual(self.health.storage()['status'], 'error')
        database.chmod(0o600)
        database.write_bytes(b'broken journal')
        self.assertEqual(self.health.storage()['status'], 'error')
        self.assertEqual(database.read_bytes(), b'broken journal', 'Diagnostics must not repair or replace saved work')

    async def test_outage_probe_is_bounded(self):
        self.health.upstream_url = 'ws://127.0.0.1:1/native'
        self.assertEqual((await asyncio.wait_for(self.health.native(), 7))[0]['status'], 'error')

    async def test_http_health_requires_secret_and_returns_503_with_specific_checks(self):
        token = self.home / '.config/codex-spark/mobile.token'
        bridge = Bridge(self.health.state, token, self.home / '.config/codex-spark/server.token')
        self.addCleanup(bridge.journal.close)
        bridge.health_probe = self.health
        self.model_ids = []
        app = web.Application()
        app.router.add_get('/status', bridge.health)
        async with TestClient(TestServer(app)) as client:
            async with client.get('/status') as response:
                self.assertEqual(response.status, 401)
            self.assertEqual(self.model_requests, 0)
            async with client.get('/status', headers={'X-Secret-Key': 'private-mobile-token'}) as response:
                self.assertEqual(response.status, 503)
                self.assertEqual(response.headers['Cache-Control'], 'no-store')
                self.assertEqual((await response.json())['status'], 'degraded')
        result = await bridge.dispatch(None, '_goose/unstable/health', {})
        self.assertEqual(result['status'], 'degraded')
        self.assertNotIn('turn/start', self.methods)

    async def test_live_timeout_does_not_resubmit_or_archive_unfinished_work(self):
        calls = []
        class FakeRPC:
            def __init__(self, *args, **kwargs): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *args): pass
            async def call(self, method, params):
                calls.append(method)
                if method == 'session/new': return {'sessionId': 'diagnostic-only', '_meta': {'workingDir': '/tmp'}}
                await asyncio.Event().wait()
        with patch('doctor.RPC', FakeRPC):
            result = await live_test(self.home, 'ws://127.0.0.1:3285/acp', 0.05)
        self.assertEqual(result['status'], 'error')
        self.assertEqual(calls, ['session/new', 'session/prompt'])
        self.assertEqual(result['sessionId'], 'diagnostic-only')
        self.assertIn('No prompt or tool action was retried', result['detail'])

    async def test_live_evidence_rejects_duplicate_or_unsuccessful_tool_results(self):
        journal = MobileState(self.health.state / 'recovery.sqlite3')
        self.addCleanup(journal.close)
        command = {'id': 'command', 'type': 'commandExecution', 'exitCode': 0, 'aggregatedOutput': 'MARKER\n'}
        journal.save_turn('diagnostic', {'id': 'turn', 'items': [command]})
        verify_command(self.home, 'diagnostic', 'MARKER', {'command'})
        journal.save_turn('diagnostic', {'id': 'turn', 'items': [command, {**command, 'id': 'duplicate'}]})
        with self.assertRaises(ValueError): verify_command(self.home, 'diagnostic', 'MARKER', {'command', 'duplicate'})
        journal.save_turn('diagnostic', {'id': 'turn', 'items': [{**command, 'exitCode': 1}]})
        with self.assertRaises(ValueError): verify_command(self.home, 'diagnostic', 'MARKER', {'command'})


if __name__ == '__main__':
    unittest.main()
