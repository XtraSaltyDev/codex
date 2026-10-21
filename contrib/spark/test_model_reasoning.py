import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import urllib.error

from model_capabilities import CapabilityUnknown
from model_reasoning import discover, refresh


class ReasoningTests(unittest.TestCase):
    def response(self, level, status='completed', answer='323', tokens=None):
        events = [{'type': 'response.output_text.delta', 'delta': answer},
                  {'type': 'response.completed', 'response': {'id': 'resp_test', 'status': status,
                   'usage': {'output_tokens_details': {'reasoning_tokens': tokens if tokens is not None else 0 if level == 'none' else 42}}}}]
        return io.BytesIO(''.join('data: ' + json.dumps(event) + '\n\n' for event in events).encode())

    def provider(self, request, **kwargs):
        level = json.loads(request.data)['reasoning']['effort']
        if level == 'high':
            raise urllib.error.HTTPError(request.full_url, 400, 'Bad Request', {},
                                         io.BytesIO(b'Unexpected reasoning effort high. Supported types are xhigh, medium, and low.'))
        return self.response(level)

    def test_only_verified_modes_are_advertised_without_name_guessing(self):
        with patch('model_reasoning.urllib.request.urlopen', side_effect=self.provider) as request:
            result = discover('http://provider/v1', {'id': 'arbitrary/model'})
        self.assertEqual([level['effort'] for level in result['supported_reasoning_levels']], ['none', 'low', 'medium', 'xhigh'])
        self.assertEqual(result['probes']['high']['status'], 'unsupported')
        self.assertEqual(result['probes']['none']['reasoning_tokens'], 0)
        self.assertEqual(len(request.call_args_list), 5)
        for call in request.call_args_list:
            payload = json.loads(call.args[0].data)
            self.assertFalse(payload['store'])
            self.assertNotIn('tools', payload)
            self.assertEqual(payload['max_output_tokens'], 2048)

    def test_outages_and_unrelated_schema_errors_are_not_unsupported_modes(self):
        for code in (400, 401, 404, 422, 429, 500):
            with self.subTest(code=code):
                error = urllib.error.HTTPError('http://provider', code, 'Error', {}, io.BytesIO(b'invalid image detail'))
                with patch('model_reasoning.urllib.request.urlopen', side_effect=error), self.assertRaises(CapabilityUnknown):
                    discover('http://provider/v1', {'id': 'model'})
        with patch('model_reasoning.urllib.request.urlopen', side_effect=TimeoutError), self.assertRaises(CapabilityUnknown):
            discover('http://provider/v1', {'id': 'model'})

    def test_incomplete_wrong_missing_usage_and_ignored_effort_are_inconclusive(self):
        for response in (self.response('none', status='incomplete'), self.response('none', answer='wrong'),
                         self.response('none', tokens=12), io.BytesIO(b'not SSE'),
                         io.BytesIO(b'data: {"type":"response.completed","response":{"status":"completed"}}\n\n')):
            with patch('model_reasoning.urllib.request.urlopen', return_value=response), self.assertRaises(CapabilityUnknown):
                discover('http://provider/v1', {'id': 'model'})
        responses = [self.response('none'), self.response('low', tokens=0)]
        with patch('model_reasoning.urllib.request.urlopen', side_effect=responses), self.assertRaises(CapabilityUnknown):
            discover('http://provider/v1', {'id': 'model'})

    def test_refresh_changes_only_choices_and_keeps_default_config_and_other_models(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            config = 'model="chosen"\nmodel_provider="spark"\nmodel_catalog_json="models.json"\n[model_providers.spark]\nbase_url="http://provider/v1"\n'
            (root / 'config.toml').write_text(config)
            catalog = {'models': [{'slug': 'chosen', 'default_reasoning_level': None, 'supported_reasoning_levels': [],
                                   'input_modalities': ['text', 'image'], 'model_messages': {'instructions_template': 'unchanged'}},
                                  {'slug': 'other', 'priority': 7}]}
            original = json.dumps(catalog)
            (root / 'models.json').write_text(original)
            evidence = {'supported_reasoning_levels': [{'effort': 'none', 'description': 'No thinking'},
                                                       {'effort': 'medium', 'description': 'Reasoning'}]}
            def served(*args, **kwargs):
                return io.BytesIO(b'{"data":[{"id":"chosen"}]}')
            with patch('model_reasoning.urllib.request.urlopen', side_effect=served), \
                 patch('model_reasoning.discover', side_effect=CapabilityUnknown('unknown')):
                with self.assertRaises(CapabilityUnknown):
                    refresh(root, apply=True)
            self.assertEqual((root / 'models.json').read_text(), original)
            self.assertEqual(list(root.glob('*.before-*')), [])
            with patch('model_reasoning.urllib.request.urlopen', side_effect=served), \
                 patch('model_reasoning.discover', return_value=evidence):
                self.assertFalse(refresh(root)['applied'])
                self.assertEqual((root / 'models.json').read_text(), original)
                refresh(root, apply=True)
            catalog['models'][0]['supported_reasoning_levels'] = evidence['supported_reasoning_levels']
            self.assertEqual(json.loads((root / 'models.json').read_text()), catalog)
            self.assertEqual((root / 'config.toml').read_text(), config)
            self.assertEqual(next(root.glob('models.json.before-*')).read_text(), original)
            def changed(*args):
                (root / 'models.json').write_text(original)
                return evidence
            with patch('model_reasoning.urllib.request.urlopen', side_effect=served), \
                 patch('model_reasoning.discover', side_effect=changed), self.assertRaisesRegex(ValueError, 'profile changed'):
                refresh(root, apply=True)


if __name__ == '__main__':
    unittest.main()
