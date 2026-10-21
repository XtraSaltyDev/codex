import base64
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from mobile_content import prompt_input, user_content_blocks
from mobile_state import MobileState


class MobileContentTests(unittest.TestCase):
    def setUp(self):
        self.image = {'type': 'image', 'mimeType': 'image/png', 'data': base64.b64encode(b'image fixture').decode()}

    def test_image_only_and_mixed_inputs_keep_order_and_replay(self):
        text, native = prompt_input([self.image], ['text', 'image'])
        self.assertEqual(text, '[Image]')
        self.assertEqual(native[0]['detail'], 'auto')
        self.assertEqual(list(user_content_blocks(native)), [self.image])
        prompt = [{'type': 'text', 'text': 'Before'}, self.image, {'type': 'text', 'text': 'After'}]
        text, native = prompt_input(prompt, ['text', 'image'])
        self.assertEqual(text, 'Before\nAfter')
        self.assertEqual(list(user_content_blocks(native)), prompt)
        self.assertEqual(prompt_input([{'type': 'text', 'text': 'Same text'}], ['text']), ('Same text', None))

    def test_invalid_unsupported_and_oversized_attachments_are_rejected(self):
        with self.assertRaisesRegex(ValueError, 'not enabled'):
            prompt_input([self.image], ['text'])
        for prompt in ([{**self.image, 'data': 'not base64!'}], [{**self.image, 'data': ''}],
                       [{**self.image, 'mimeType': 'text/html'}], [{'type': 'audio'}], []):
            with self.subTest(prompt=prompt), self.assertRaises(ValueError):
                prompt_input(prompt, ['text', 'image'])
        with patch('mobile_content.MAX_IMAGE_BYTES', 15), self.assertRaisesRegex(ValueError, 'total at most'):
            prompt_input([self.image, self.image], ['text', 'image'])

    def test_receipts_migrate_without_changing_existing_text_digests(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'journal.sqlite3'
            with sqlite3.connect(path) as db:
                db.execute('CREATE TABLE submissions(id TEXT PRIMARY KEY, thread TEXT, text TEXT, digest TEXT, phase TEXT, turn TEXT, created INTEGER)')
                db.execute('INSERT INTO submissions VALUES(?,?,?,?,?,?,?)', ('old', 'thread', 'same', hashlib.sha256(b'same').hexdigest(), 'finished', 'turn', 1))
            state = MobileState(path)
            try:
                record, fresh = state.prepare('old', 'thread', 'same')
                self.assertFalse(fresh)
                self.assertEqual(record['phase'], 'finished')
                self.assertIsNone(record['content'])
                text, native = prompt_input([self.image], ['text', 'image'])
                state.prepare('image', 'thread', text, native)
                self.assertFalse(state.prepare('image', 'thread', text, native)[1])
                self.assertEqual(json.loads(state.submission('image')['content']), native)
                _, other = prompt_input([{**self.image, 'data': base64.b64encode(b'other image').decode()}], ['text', 'image'])
                with self.assertRaisesRegex(ValueError, 'different message'):
                    state.prepare('image', 'thread', text, other)
            finally:
                state.close()


if __name__ == '__main__':
    unittest.main()
