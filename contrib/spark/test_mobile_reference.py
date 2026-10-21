import asyncio
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from mobile_options import selection, validate
from mobile_workspace import browse, changes, read
import test_goose_mobile as mobile_tests
from goose_mobile import RPCError


CATALOG = {'local': {'displayName': 'Local model', 'defaultReasoningEffort': 'low',
                    'supportedReasoningEfforts': [{'reasoningEffort': effort, 'description': effort} for effort in ('none', 'low', 'medium', 'xhigh')],
                    'serviceTiers': []}}


class ReferenceOptionsTests(unittest.TestCase):
    def test_only_native_reasoning_and_speed_choices_are_exposed(self):
        options = selection(CATALOG, {'modelId': 'local', 'reasoningEffort': 'low'}, {})
        self.assertEqual([o['id'] for o in options['reasoningOptions']], ['none', 'low', 'medium', 'xhigh'])
        self.assertEqual(options['speedOptions'], [])
        for invalid in ({'reasoning': 'ultra'}, {'speed': 'priority'}, {'model': 'unserved'}, {'access': 'custom'}, {'arbitrary': 'x'}):
            with self.assertRaises(ValueError): validate(CATALOG, {'modelId': 'local'}, {}, invalid)
        values = validate(CATALOG, {'modelId': 'local'}, {}, {'reasoning': 'medium', 'access': 'readOnly'})
        self.assertEqual(values['reasoningEffort'], 'medium')
        self.assertFalse(values['fullAccess'])
        self.assertEqual(values['access'], 'readOnly')


class ProjectBrowserTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'project'; self.root.mkdir()
        (self.root / 'docs').mkdir(); (self.root / 'docs/readme.md').write_text('# Example\n\nA **preview**.\n')
        (self.root / 'notes.txt').write_text('before\n'); (self.root / '.env').write_text('SECRET=do-not-read')
        self.git('init', '-q'); self.git('add', 'docs/readme.md', 'notes.txt')
        self.git('-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid', 'commit', '-qm', 'Initial')

    def git(self, *args):
        return subprocess.run(['git', '-C', str(self.root), *args], check=True, capture_output=True)

    def test_browse_preview_and_path_boundaries(self):
        listed = browse(self.root, {})
        self.assertEqual(listed['entries'][0]['kind'], 'directory')
        self.assertNotIn('.env', [e['name'] for e in listed['entries']])
        self.assertTrue(read(self.root, 'docs/readme.md')['markdown'])
        (self.root / 'outside').symlink_to(self.root.parent)
        for path in ('../outside.txt', '/etc/passwd', 'outside/anything', '.env', '.git/config'):
            with self.assertRaises((ValueError, OSError)): read(self.root, path)
        (self.root / 'binary').write_bytes(b'a\0b')
        self.assertEqual(read(self.root, 'binary')['kind'], 'binary')
        (self.root / 'large').write_bytes(b'x' * (130 * 1024))
        self.assertEqual(read(self.root, 'large')['kind'], 'omitted')
        (self.root / 'config.txt').write_text('api_key=private-value\n')
        redacted = read(self.root, 'config.txt')
        self.assertTrue(redacted['redacted']); self.assertNotIn('private-value', redacted['text'])

    def test_staged_and_unstaged_diffs_include_untracked_files_without_mutating_index(self):
        (self.root / 'notes.txt').write_text('staged\n'); self.git('add', 'notes.txt')
        (self.root / 'notes.txt').write_text('working\n')
        (self.root / 'new.txt').write_text('new\n')
        before = self.git('status', '--porcelain=v1', '-z').stdout
        unstaged = changes(self.root, {})
        self.assertEqual({f['path'] for f in unstaged['files']}, {'notes.txt', 'new.txt'})
        staged = changes(self.root, {'scope': 'staged'})
        self.assertEqual([f['path'] for f in staged['files']], ['notes.txt'])
        diff = changes(self.root, {'scope': 'unstaged', 'path': 'notes.txt'})['file']['diff']
        self.assertIn('-staged', diff); self.assertIn('+working', diff)
        self.assertIn('+staged', changes(self.root, {'scope': 'staged', 'path': 'notes.txt'})['file']['diff'])
        self.assertEqual(self.git('status', '--porcelain=v1', '-z').stdout, before)
        with self.assertRaises(ValueError): changes(self.root, {'path': '../anything'})


class BridgeReferenceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        mobile_tests.MobileTests.setUp(self)

    async def test_recovery_turn_policy_does_not_replace_saved_ordinary_access(self):
        self.bridge.metadata['sessions']['thread-a'] = {'access': 'full', 'fullAccess': True}
        self.bridge.remember_settings({'thread': {'id': 'thread-a'}, 'sandbox': {'type': 'readOnly'}})
        self.assertEqual(self.bridge.session_settings('thread-a')['access'], 'full')
        self.bridge.waiters['thread-a'] = asyncio.get_running_loop().create_future()
        await self.bridge.apply_event({'method': 'thread/settings/updated', 'params': {
            'threadId': 'thread-a', 'threadSettings': {'sandboxPolicy': {'type': 'readOnly'}, 'effort': 'low'}}})
        self.assertEqual(self.bridge.session_settings('thread-a')['access'], 'full')
        self.bridge.waiters.clear()

    async def test_native_rejection_keeps_previous_saved_settings(self):
        self.bridge.loaded.add('thread-a')
        self.bridge.metadata['sessions']['thread-a'] = {'access': 'workspace', 'reasoningEffort': 'low'}
        self.bridge.option_defaults = {'modelId': 'local'}
        async def request(method, params):
            if method == 'thread/read': return {'thread': {'id': 'thread-a', 'cwd': '/tmp/project'}}
            if method == 'model/list': return {'data': [{'model': 'local', **CATALOG['local']}]}
            raise RPCError('Native settings rejected')
        self.bridge.rpc.request = request
        with self.assertRaises(RPCError):
            await self.bridge.dispatch(self.phone, 'session/set_config_option', {'sessionId': 'thread-a', 'configId': 'access', 'value': 'full'})
        self.assertEqual(self.bridge.metadata['sessions']['thread-a'], {'access': 'workspace', 'reasoningEffort': 'low'})

    async def test_settings_validate_before_native_mutation_and_persist_only_after_success(self):
        self.bridge.loaded.add('thread-a')
        self.bridge.option_defaults = {'modelId': 'local', 'reasoningEffort': 'low'}
        self.bridge.model_catalog = CATALOG
        calls = []
        async def request(method, params):
            calls.append((method, params))
            if method == 'model/list': return {'data': [{'model': 'local', **CATALOG['local']}]}
            if method == 'thread/read': return {'thread': {'id': 'thread-a', 'cwd': '/tmp/project'}}
            if method == 'thread/settings/update': return {}
            self.fail(method)
        self.bridge.rpc.request = request
        result = await self.bridge.dispatch(self.phone, 'session/set_config_option', {'sessionId': 'thread-a', 'configId': 'reasoning', 'value': 'medium'})
        self.assertEqual(result['_meta']['goose']['modelSelection']['reasoningEffort'], 'medium')
        self.assertIn(('thread/settings/update', {'threadId': 'thread-a', 'model': 'local', 'effort': 'medium'}), calls)
        calls.clear()
        with self.assertRaises(RPCError):
            await self.bridge.dispatch(self.phone, 'session/set_config_option', {'sessionId': 'thread-a', 'configId': 'reasoning', 'value': 'ultra'})
        self.assertNotIn('thread/settings/update', [m for m,p in calls])
        self.bridge.turns['thread-a'] = {'status': 'inProgress'}
        with self.assertRaises(RPCError):
            await self.bridge.dispatch(self.phone, 'session/set_config_option', {'sessionId': 'thread-a', 'configId': 'access', 'value': 'full'})
