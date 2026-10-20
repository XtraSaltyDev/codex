"""Bounded, read-only diagnostics shared by the CLI and mobile adapter."""
import asyncio
from contextlib import closing
import datetime as dt
import json
import os
from pathlib import Path
import shutil
import sqlite3
import stat
import sys
import time
import tomllib

sys.path.insert(0, str(Path.home() / '.local/share/codex-spark/mobile-python'))
from aiohttp import ClientSession, ClientTimeout, WSMsgType


def check(identity, title, status, detail):
    return {'id': identity, 'title': title, 'status': status, 'detail': detail}


def report(checks):
    failed = [item for item in checks if item['status'] == 'error']
    return {'version': 1, 'checkedAt': dt.datetime.now(dt.timezone.utc).isoformat(),
            'status': 'degraded' if failed else 'ok',
            'summary': failed[0]['detail'] if failed else 'Backend checks passed', 'checks': checks}


class RPC:
    def __init__(self, session, url, token, mobile=False):
        self.session, self.url, self.token, self.mobile = session, url, token, mobile
        self.number = 0
        self.events = []

    async def __aenter__(self):
        headers = {'X-Secret-Key': self.token} if self.mobile else {'Authorization': 'Bearer ' + self.token}
        self.ws = await self.session.ws_connect(self.url, headers=headers, max_msg_size=16 * 1024 * 1024)
        params = {'protocolVersion': 1, 'clientCapabilities': {}} if self.mobile else {
            'clientInfo': {'name': 'codex_spark_doctor', 'version': '1.0.0'}}
        try:
            initialized = await self.call('initialize', params)
            if self.mobile and initialized.get('protocolVersion') != 1:
                raise ValueError('Unsupported mobile protocol')
            if not self.mobile:
                await self.ws.send_json({'method': 'initialized'})
        except BaseException:
            await self.ws.close()
            raise
        return self

    async def __aexit__(self, *args):
        await self.ws.close()

    async def call(self, method, params):
        self.number += 1
        number = self.number
        await self.ws.send_json({'jsonrpc': '2.0', 'id': number, 'method': method, 'params': params})
        while True:
            frame = await self.ws.receive()
            if frame.type != WSMsgType.TEXT:
                raise ConnectionError('Connection closed')
            packet = json.loads(frame.data)
            if packet.get('id') == number and 'method' not in packet:
                if 'error' in packet:
                    raise RuntimeError('RPC request rejected')
                return packet.get('result', {})
            self.events.append(packet)
            if 'id' in packet and 'method' in packet:
                result = {'outcome': {'outcome': 'cancelled'}} if self.mobile else {'decision': 'decline'}
                await self.ws.send_json({'jsonrpc': '2.0', 'id': packet['id'], 'result': result})
                raise RuntimeError('Unexpected approval request')


class SparkHealth:
    def __init__(self, home=None, state=None, upstream_url=None, mobile_url='ws://127.0.0.1:3285/acp'):
        self.home = Path(home or Path.home())
        self.state = Path(state or self.home / '.local/state/codex-spark/mobile')
        self.runtime = self.home / '.codex-spark'
        self.upstream_url = upstream_url
        self.mobile_url = mobile_url
        self.lock = asyncio.Lock()
        self.cached = None
        self.cached_at = 0

    async def collect(self, fresh=False):
        async with self.lock:
            if not fresh and self.cached and time.monotonic() - self.cached_at < 5:
                return self.cached
            checks = []
            try:
                config = tomllib.loads((self.runtime / 'config.toml').read_text())
                checks.append(check('configuration', 'Configuration', 'ok', 'Spark profile is readable'))
            except (OSError, ValueError):
                config = {}
                checks.append(check('configuration', 'Configuration', 'error', 'Spark profile is missing or invalid'))
            services, native, storage, mobile = await asyncio.gather(self.services(), self.native(), asyncio.to_thread(self.storage), self.mobile())
            checks.extend(services)
            checks.append(native[0])
            # Inspect the effective running provider when Codex can answer.
            checks.append(await self.model(native[1] or config))
            checks.append(mobile)
            checks.append(storage)
            self.cached, self.cached_at = report(checks), time.monotonic()
            return self.cached

    async def services(self):
        async def probe(unit, title):
            try:
                process = await asyncio.create_subprocess_exec('systemctl', '--user', 'is-active', unit,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
                try:
                    stdout, _ = await asyncio.wait_for(process.communicate(), 3)
                except BaseException:
                    process.kill()
                    await process.wait()
                    raise
                active = process.returncode == 0 and stdout.decode().strip() == 'active'
                return check('service_' + unit.removeprefix('codex-spark-'), title, 'ok' if active else 'error',
                             title + (' is running' if active else ' is not running; check its user service'))
            except (OSError, TimeoutError):
                return check('service_' + unit.removeprefix('codex-spark-'), title, 'error', 'Cannot inspect ' + title.lower())
        return await asyncio.gather(*(probe('codex-spark-' + name, title) for name, title in (
            ('tunnel', 'Spark tunnel'), ('app-server', 'Codex service'), ('mobile', 'Mobile service'))))

    async def native(self):
        try:
            token = (self.home / '.config/codex-spark/server.token').read_text().strip()
            if not token:
                raise ValueError('Empty token')
            url = self.upstream_url
            if not url:
                install = json.loads((self.home / '.local/state/codex-spark/installation.json').read_text())
                url = install['app_server']
            async with asyncio.timeout(5):
                async with ClientSession(timeout=ClientTimeout(total=5)) as session:
                    async with RPC(session, url, token) as rpc:
                        config = (await rpc.call('config/read', {'includeLayers': False}))['config']
            return check('app_server', 'Codex connection', 'ok', 'Codex authenticated and answered'), config
        except Exception:
            return check('app_server', 'Codex connection', 'error', 'Codex is unavailable or its server token was rejected'), None

    async def model(self, config):
        try:
            provider = config['model_providers'][config['model_provider']]
            headers = dict(provider.get('http_headers') or {})
            for key, variable in (provider.get('env_http_headers') or {}).items():
                headers[key] = os.environ[variable]
            if provider.get('env_key'):
                headers['Authorization'] = 'Bearer ' + os.environ[provider['env_key']]
            async with ClientSession(timeout=ClientTimeout(total=5)) as session:
                async with session.get(provider['base_url'].rstrip('/') + '/models', headers=headers, allow_redirects=False) as response:
                    if response.status in (401, 403):
                        return check('spark', 'Spark model', 'error', 'Spark rejected its inference credentials')
                    if response.status != 200:
                        raise ConnectionError('Model endpoint unavailable')
                    body = await response.json()
            if not any(model.get('id') == config['model'] for model in body['data']):
                return check('spark', 'Spark model', 'error', 'Spark is reachable but the configured model is not served')
            return check('spark', 'Spark model', 'ok', 'Configured model is available; inference has not been tested')
        except Exception:
            return check('spark', 'Spark model', 'error', 'Spark is unavailable; check the model server and private tunnel')

    async def mobile(self):
        try:
            token = (self.home / '.config/codex-spark/mobile.token').read_text().strip()
            if not token:
                raise ValueError('Empty mobile token')
            async with asyncio.timeout(5):
                async with ClientSession(timeout=ClientTimeout(total=5)) as session:
                    async with RPC(session, self.mobile_url, token, mobile=True):
                        pass
            return check('mobile_connection', 'Mobile connection', 'ok', 'Mobile credentials accepted and the adapter answered')
        except Exception:
            return check('mobile_connection', 'Mobile connection', 'error', 'Mobile endpoint is unavailable or its secret was rejected')

    def storage(self):
        try:
            database = self.state / 'recovery.sqlite3'
            if stat.S_IMODE(database.stat().st_mode) & 0o077:
                return check('storage', 'Saved work', 'error', 'Recovery journal permissions must be private (0600)')
            if not os.access(database, os.R_OK | os.W_OK) or not os.access(self.state, os.W_OK):
                raise PermissionError('Journal is not writable')
            with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True, timeout=1)) as db:
                deadline = time.monotonic() + 2
                db.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
                if db.execute('PRAGMA quick_check(1)').fetchone()[0] != 'ok':
                    raise ValueError('Journal integrity check failed')
                tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if not {'submissions', 'turns', 'events'} <= tables:
                    raise ValueError('Missing recovery tables')
            free = shutil.disk_usage(self.state).free
            if free < 100 * 1024 * 1024:
                raise OSError('Insufficient disk space')
            return check('storage', 'Saved work', 'ok', 'Recovery journal is readable, private, and writable; disk space is available')
        except Exception:
            return check('storage', 'Saved work', 'error', 'Saved-work storage needs attention; check disk space, permissions, and journal integrity')
