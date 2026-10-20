import asyncio
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from goose_mobile import Bridge, item_update
from mobile_state import MobileState, merge_items
from work_review import capture, finish, visible_text, MAX_FILE


class ReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / 'project'
        self.repo.mkdir()
        self.git('init', '-q')
        (self.repo / 'a.txt').write_text('one\ntwo\n')
        (self.repo / 'deleted.txt').write_text('remove me\n')
        self.git('add', '.')
        self.git('-c', 'user.name=Tests', '-c', 'user.email=tests@example.invalid', 'commit', '-qm', 'baseline')

    def git(self, *args):
        return subprocess.check_output(['git', '-C', str(self.repo), *args], stderr=subprocess.DEVNULL)

    def test_shell_edits_exclude_existing_dirty_and_untracked_content(self):
        (self.repo / 'a.txt').write_text('one\nuser edit\ntwo\n')
        (self.repo / 'existing.txt').write_text('existing untracked\n')
        before = capture(self.repo)
        (self.repo / 'a.txt').write_text('one\nuser edit\nagent edit\ntwo\n')
        (self.repo / 'new.txt').write_text('new\nfile\n')
        result = finish(before)
        files = {f['path']: f for f in result['files']}
        self.assertEqual(set(files), {'a.txt', 'new.txt'})
        self.assertEqual((files['a.txt']['added'], files['a.txt']['removed']), (1, 0))
        self.assertNotIn('+user edit', files['a.txt']['diff'])
        self.assertTrue(files['a.txt']['hadExistingChanges'])
        self.assertEqual(files['new.txt']['kind'], 'added')
        self.assertEqual((self.repo / 'existing.txt').read_text(), 'existing untracked\n')

    def test_deletions_and_changes_committed_during_task_are_retained(self):
        before = capture(self.repo)
        (self.repo / 'deleted.txt').unlink()
        (self.repo / 'a.txt').write_text('changed\n')
        self.git('add', '-A')
        self.git('-c', 'user.name=Tests', '-c', 'user.email=tests@example.invalid', 'commit', '-qm', 'task')
        files = {f['path']: f for f in finish(before)['files']}
        self.assertEqual(files['deleted.txt']['kind'], 'deleted')
        self.assertEqual(files['deleted.txt']['removed'], 1)
        self.assertEqual((files['a.txt']['added'], files['a.txt']['removed']), (1, 2))

    def test_binary_symlink_and_large_file_handling_is_explicit(self):
        outside = self.root / 'outside'
        outside.write_text('private outside contents')
        (self.repo / 'link').symlink_to(outside)
        before = capture(self.repo)
        outside.write_text('changed outside')
        self.assertEqual(finish(before)['files'], [])
        (self.repo / 'binary').write_bytes(b'\0binary')
        (self.repo / 'large').write_bytes(b'x' * (MAX_FILE + 1))
        result = finish(before)
        self.assertEqual(result['status'], 'partial')
        binary = next(f for f in result['files'] if f['path'] == 'binary')
        self.assertTrue(binary['binary'])
        self.assertIsNone(binary['added'])
        self.assertNotIn('changed outside', json.dumps(result))

    def test_ordinary_folder_and_unborn_git_repository_are_supported(self):
        folder = self.root / 'plain'; folder.mkdir()
        (folder / 'existing.py').write_text('old\n')
        before = capture(folder)
        (folder / 'existing.py').write_text('new\n')
        (folder / 'created.py').write_text('created\n')
        (folder / '.env').write_text('SECRET=should-not-be-reviewed')
        self.assertEqual({f['path'] for f in finish(before)['files']}, {'existing.py', 'created.py'})
        self.assertNotIn('should-not-be-reviewed', json.dumps(finish(before)))
        subprocess.run(['git', 'init', '-q', str(folder)], check=True)
        before = capture(folder)
        (folder / 'created.py').write_text('modified\n')
        self.assertEqual([f['path'] for f in finish(before)['files']], ['created.py'])

    def test_no_change_and_interrupted_capture_never_claim_a_clean_complete_review(self):
        before = capture(self.repo)
        self.assertEqual(finish(before)['files'], [])
        self.assertEqual(finish(before)['status'], 'captured')
        self.assertEqual(finish(before, interrupted=True)['status'], 'partial')
        self.assertEqual(finish({'unavailable': True})['status'], 'unavailable')

    def test_truncated_diff_is_labeled_partial(self):
        before = capture(self.repo)
        (self.repo / 'long-line.txt').write_text('x' * 40000 + '\n')
        result = finish(before)
        self.assertEqual(result['status'], 'partial')
        self.assertTrue(result['files'][0]['truncated'])
        self.assertLessEqual(len(result['files'][0]['diff'].encode()), 24 * 1024)

    def test_review_persists_across_restart_and_overlapping_work_is_labeled(self):
        database = self.root / 'journal.sqlite3'
        state = MobileState(database)
        state.prepare('request', 'thread', 'edit')
        state.phase('request', 'accepted', 'turn')
        state.prepare_review('request', 'thread', capture(self.repo))
        state.prepare_review('other', 'other-thread', capture(self.repo))
        state.close()
        (self.repo / 'a.txt').write_text('after restart\n')
        state = MobileState(database)
        try:
            pending = state.pending_review('turn')
            result = finish(pending['baseline'])
            self.assertEqual(result['status'], 'partial')
            self.assertIn('overlapped', result['notice'])
            state.finish_review('request', result)
            self.assertEqual(state.review_result('turn'), result)
            self.assertIsNone(state.pending_review('turn'))
            self.assertIsNone(state.db.execute('SELECT baseline FROM work_reviews WHERE request=?', ('request',)).fetchone()[0])
        finally:
            state.close()

    def test_command_output_is_bounded_redacted_and_exit_code_is_real(self):
        item = {'id': 'tool', 'type': 'commandExecution', 'command': 'run tests', 'status': 'completed', 'exitCode': 1,
                'aggregatedOutput': 'API_KEY=private-value\n\x1b[31mFAILED\x1b[0m\n' + 'x' * 40000}
        update = item_update(item)
        result = update['_meta']['goose']['commandResult']
        self.assertEqual(update['status'], 'failed')
        self.assertEqual(result['exitCode'], 1)
        self.assertTrue(result['truncated'])
        self.assertNotIn('private-value', result['output'])
        self.assertNotIn('\x1b', result['output'])
        self.assertIn('FAILED', result['output'])
        replay = merge_items([item], [{'id': 'tool', 'type': 'commandExecution', 'status': 'completed'}])[0]
        self.assertEqual(replay['aggregatedOutput'], item['aggregatedOutput'])
        private, truncated = visible_text('-----BEGIN PRIVATE KEY-----\n' + 'private' * 40000)
        self.assertIn('[REDACTED PRIVATE KEY]', private)
        self.assertNotIn('privateprivate', private)
        self.assertTrue(truncated)


class ReviewStreamingTests(unittest.IsolatedAsyncioTestCase):
    async def test_completed_review_and_tool_output_survive_history_replay(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); token = root / 'token'; token.write_text('private-test-token')
            bridge = Bridge(root / 'state', token, token)
            class Socket:
                closed = False
                def __init__(self): self.packets = []
                async def send_json(self, packet): self.packets.append(packet)
            phone = Socket(); bridge.clients[phone] = {'thread'}
            bridge.journal.prepare('request', 'thread', 'do work')
            bridge.journal.phase('request', 'accepted', 'turn')
            bridge.journal.prepare_review('request', 'thread', capture(root))
            (root / 'result.txt').write_text('actual edit\n')
            try:
                await bridge.event({'method': 'turn/started', 'params': {'threadId': 'thread', 'turn': {'id': 'turn', 'status': 'inProgress', 'items': []}}})
                tool = {'id': 'tool', 'type': 'commandExecution', 'command': 'test', 'exitCode': 0, 'aggregatedOutput': 'PASS actual output', 'status': 'completed'}
                await bridge.event({'method': 'item/completed', 'params': {'threadId': 'thread', 'turnId': 'turn', 'item': tool}})
                await bridge.event({'method': 'turn/completed', 'params': {'threadId': 'thread', 'turn': {'id': 'turn', 'status': 'completed', 'items': [{'id': 'answer', 'type': 'agentMessage', 'text': 'Done'}]}}})
                self.assertEqual(bridge.status(bridge.turns['thread'])['workReview']['files'][0]['path'], 'result.txt')
                async def loaded(identity, include_turns=False): return {'id': 'thread', 'cwd': str(root), 'updatedAt': 1, 'turns': []}
                bridge.ensure_loaded = loaded
                phone.packets.clear()
                result = await bridge.dispatch(phone, 'session/load', {'sessionId': 'thread'})
                self.assertEqual(result['_meta']['hostRuns'][0]['workReview']['files'][0]['added'], 1)
                updates = [p['params']['update'] for p in phone.packets]
                command = next(u for u in updates if u.get('toolCallId') == 'tool')
                self.assertEqual(command['_meta']['goose']['commandResult']['output'], 'PASS actual output')
            finally:
                bridge.journal.close()
