import io
import json
import hashlib
from pathlib import Path
import tempfile
import tomllib
import unittest
from unittest.mock import patch

import configure
from tool_instructions import PATCH_GUIDANCE, spark_instructions


class ToolInstructionTests(unittest.TestCase):
    def test_released_prompt_revisions_match_native_upgrade_registry(self):
        repo = Path(__file__).resolve().parents[2]
        base = (repo / 'codex-rs/models-manager/prompt.md').read_text()
        registry = (repo / 'codex-rs/prompts/src/spark_instructions.rs').read_text()
        fixture = (repo / 'codex-rs/prompts/templates/spark/patch-guidance-v2.txt').read_text().rstrip('\n')
        self.assertEqual(fixture, PATCH_GUIDANCE)
        for text in (base, spark_instructions(base)):
            self.assertIn(hashlib.sha256(text.encode()).hexdigest(), registry)

    def test_adaptation_preserves_other_instructions_and_is_idempotent(self):
        base = (Path(__file__).resolve().parents[2] / 'codex-rs/models-manager/prompt.md').read_text()
        old_line = next(line for line in base.splitlines() if line.startswith('- Use the `apply_patch` tool to edit files'))
        updated = spark_instructions(base)
        self.assertEqual(updated.replace(PATCH_GUIDANCE, old_line), base)
        self.assertEqual(spark_instructions(updated), updated)
        self.assertIn('You have access to an `update_plan` tool', updated)
        example = json.loads(PATCH_GUIDANCE.split('For example: ', 1)[1])
        self.assertEqual(set(example), {'cmd'})
        self.assertIn("apply_patch <<'PATCH'\n*** Begin Patch\n", example['cmd'])
        self.assertTrue(example['cmd'].endswith('\n*** End Patch\nPATCH'))

    def test_unknown_or_ambiguous_prompt_fails_before_overwriting_it(self):
        for text in ('Custom instructions without a patch rule.',
                     '- Use the `apply_patch` tool to edit files once.\n- Use the `apply_patch` tool to edit files twice.'):
            with self.subTest(text=text), self.assertRaisesRegex(ValueError, 'review the Spark prompt'):
                spark_instructions(text)

    def test_installer_generates_matching_tool_configuration_and_prompt(self):
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder)
            binary = home / 'candidate-codex'
            binary.write_text('fixture binary')
            model = {'id': 'served/model', 'max_model_len': 262144, 'input_modalities': ['text', 'image']}
            reasoning = {'supported_reasoning_levels': [{'effort': 'none', 'description': 'No thinking'},
                                                        {'effort': 'low', 'description': 'Brief reasoning'},
                                                        {'effort': 'medium', 'description': 'Reasoning'}]}
            with patch('pathlib.Path.home', return_value=home), \
                 patch('sys.argv', ['configure.py', '--binary', str(binary)]), \
                 patch('configure.urllib.request.urlopen', return_value=io.BytesIO(json.dumps({'data': [model]}).encode())), \
                 patch('configure.discover_reasoning', return_value=reasoning), \
                 patch('configure.subprocess.run'), \
                 patch('configure.subprocess.check_output', return_value='fixture-version'), \
                 patch('install_diagnostics.install'), patch('builtins.print'):
                configure.main()
            config = tomllib.loads((home / '.codex-spark/config.toml').read_text())
            self.assertTrue(config['tools']['update_plan']['enabled'])
            self.assertEqual(config['model_reasoning_effort'], 'low')
            catalog = json.loads((home / '.codex-spark/models.json').read_text())['models'][0]
            self.assertIsNone(catalog['apply_patch_tool_type'])
            self.assertEqual(catalog['tool_mode'], 'direct')
            self.assertEqual(catalog['input_modalities'], ['text', 'image'])
            self.assertIsNone(catalog['default_reasoning_level'])
            self.assertEqual(catalog['supported_reasoning_levels'], reasoning['supported_reasoning_levels'])
            self.assertIn(PATCH_GUIDANCE, catalog['model_messages']['instructions_template'])


if __name__ == '__main__':
    unittest.main()
