import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import urllib.error

from model_capabilities import CapabilityUnknown, discover, image_probe, refresh


class CapabilityTests(unittest.TestCase):
    def setUp(self):
        self.model = {'id': 'private/model-without-a-capability-name'}
        self.png, self.expected = image_probe()
        probe = patch('model_capabilities.image_probe', return_value=(self.png, self.expected))
        probe.start()
        self.addCleanup(probe.stop)

    def response(self, text=None, status='completed'):
        events = [
            {'type': 'response.output_text.delta', 'delta': text if text is not None else ' '.join(self.expected)},
            {'type': 'response.completed', 'response': {'id': 'resp_vision', 'status': status}},
        ]
        return io.BytesIO(''.join('data: ' + json.dumps(e) + '\n\n' for e in events).encode())

    def test_unknown_metadata_uses_a_real_responses_image_request(self):
        with patch('model_capabilities.urllib.request.urlopen', return_value=self.response()) as open_url:
            result = discover('http://provider/v1', self.model)
        self.assertEqual(result['input_modalities'], ['text', 'image'])
        self.assertEqual(result['image_probe']['status'], 'verified')
        request = open_url.call_args.args[0]
        self.assertEqual(request.full_url, 'http://provider/v1/responses')
        image = json.loads(request.data)['input'][0]['content'][1]
        self.assertEqual(image['type'], 'input_image')
        self.assertEqual(image['detail'], 'auto')
        self.assertTrue(image['image_url'].startswith('data:image/png;base64,'))

    def test_explicit_metadata_and_overrides_do_not_guess_from_names(self):
        with patch('model_capabilities.urllib.request.urlopen') as open_url:
            for modalities in (['text'], ['text', 'image'], ['text', 'audio']):
                result = discover('http://provider/v1', {**self.model, 'input_modalities': modalities})
                self.assertEqual(result['input_modalities'], modalities)
                self.assertEqual(result['source'], 'models_metadata')
            result = discover('http://provider/v1', self.model, override='text,image')
            self.assertEqual(result['input_modalities'], ['text', 'image'])
            self.assertEqual(result['source'], 'explicit_override')
        open_url.assert_not_called()

    def test_only_explicit_modality_rejection_establishes_text_only(self):
        for message in ('This is not a multimodal model', 'At most 0 image(s) may be provided in one request'):
            with self.subTest(message=message):
                error = urllib.error.HTTPError('http://provider', 400, 'Bad Request', {}, io.BytesIO(message.encode()))
                with patch('model_capabilities.urllib.request.urlopen', side_effect=error):
                    result = discover('http://provider/v1', self.model)
                self.assertEqual(result['input_modalities'], ['text'])
                self.assertEqual(result['image_probe']['status'], 'unsupported')

    def test_outages_schema_errors_and_inconclusive_answers_remain_unknown(self):
        for code in (400, 401, 404, 429, 500):
            with self.subTest(code=code):
                error = urllib.error.HTTPError('http://provider', code, 'Error', {}, io.BytesIO(b'invalid request'))
                with patch('model_capabilities.urllib.request.urlopen', side_effect=error), self.assertRaises(CapabilityUnknown):
                    discover('http://provider/v1', self.model)
        for response in (self.response('I cannot see it'), self.response(status='incomplete'), io.BytesIO(b'not an SSE response')):
            with patch('model_capabilities.urllib.request.urlopen', return_value=response), self.assertRaises(CapabilityUnknown):
                discover('http://provider/v1', self.model)

    def test_refresh_preserves_configuration_and_unrelated_catalog_fields(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            config = 'model="chosen"\nmodel_provider="spark"\nmodel_catalog_json="models.json"\n[model_providers.spark]\nbase_url="http://provider/v1"\n'
            (root / 'config.toml').write_text(config)
            catalog = {'models': [{'slug': 'chosen', 'input_modalities': ['text'], 'tool_mode': 'direct'},
                                  {'slug': 'other', 'input_modalities': ['text'], 'priority': 7}]}
            original = json.dumps(catalog)
            (root / 'models.json').write_text(original)
            def model_list(*args, **kwargs):
                return io.BytesIO(b'{"data":[{"id":"chosen"}]}')
            evidence = {'input_modalities': ['text', 'image'], 'source': 'responses_image_probe'}
            with patch('model_capabilities.urllib.request.urlopen', side_effect=model_list), patch('model_capabilities.discover', side_effect=CapabilityUnknown('unknown')):
                with self.assertRaises(CapabilityUnknown):
                    refresh(root, apply=True)
            self.assertEqual((root / 'models.json').read_text(), original)
            self.assertEqual(list(root.glob('*.before-*')), [])
            with patch('model_capabilities.urllib.request.urlopen', side_effect=model_list), patch('model_capabilities.discover', return_value=evidence):
                dry = refresh(root)
                self.assertFalse(dry['applied'])
                self.assertEqual((root / 'models.json').read_text(), original)
                refresh(root, apply=True)
            expected = json.loads(original)
            expected['models'][0]['input_modalities'] = ['text', 'image']
            self.assertEqual(json.loads((root / 'models.json').read_text()), expected)
            self.assertEqual((root / 'config.toml').read_text(), config)
            self.assertEqual(next(root.glob('models.json.before-*')).read_text(), original)


if __name__ == '__main__':
    unittest.main()
