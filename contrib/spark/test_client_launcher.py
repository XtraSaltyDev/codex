import json
from pathlib import Path
import shlex
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import install_client


class ClientLauncherTests(unittest.TestCase):
    def test_terminal_cwd_explicit_overrides_and_literal_arguments(self):
        with tempfile.TemporaryDirectory(prefix='spark launcher ') as directory:
            root = Path(directory)
            home = root / 'home'
            launcher = home / '.local/bin/codex-spark'
            launcher.parent.mkdir(parents=True)
            client = root / 'record arguments.py'
            client.write_text('#!/usr/bin/env python3\nimport json, os, sys\nprint(json.dumps({"args": sys.argv[1:], "cwd": os.getcwd()}))\n')
            client.chmod(0o755)
            launcher.with_name('codex-spark-cli').symlink_to(client)
            launcher.write_text('#!/bin/sh\nif [ "${1-}" = doctor ]; then\n  shift\n  printf "doctor %s\\n" "$*"\n  exit\nfi\nset -eu\nexec ' + shlex.quote(str(client)) + ' --remote ws://127.0.0.1:4500 --remote-auth-token-env TEST_TOKEN "$@"\n')
            launcher.chmod(0o755)
            with patch.object(install_client.Path, 'home', return_value=home):
                install_client.install()
                installed = launcher.read_text()
                install_client.install()
                self.assertEqual(launcher.read_text(), installed)
                self.assertEqual(len(list((home / '.local/state/codex-spark/backups/launchers').glob('codex-spark.*'))), 1)
                self.assertEqual(list(launcher.parent.glob('codex-spark.before-*')), [])
            cwd = root / 'project with spaces'
            cwd.mkdir()
            fixed = ['--remote', 'ws://127.0.0.1:4500', '--remote-auth-token-env', 'TEST_TOKEN']
            for args in [[], ['a prompt with spaces'], ['--', '--cd=literal prompt']]:
                with self.subTest(args=args):
                    result = subprocess.run([str(launcher), *args], cwd=cwd, capture_output=True, text=True, check=True)
                    record = json.loads(result.stdout)
                    self.assertEqual(record, {'args': fixed + ['--cd', str(cwd.resolve())] + args, 'cwd': str(cwd.resolve())})
            for args in [['--cd', '/explicit project'], ['--cd=/explicit project'], ['-C', '/explicit project'], ['-C/explicit project']]:
                with self.subTest(args=args):
                    result = subprocess.run([str(launcher), *args], cwd=cwd, capture_output=True, text=True, check=True)
                    self.assertEqual(json.loads(result.stdout)['args'], fixed + args)
            result = subprocess.run([str(launcher), 'doctor', '--help'], cwd=cwd, capture_output=True, text=True, check=True)
            self.assertEqual(result.stdout, 'doctor --help\n')
            result = subprocess.run([str(launcher), 'help', 'doctor'], cwd=cwd, capture_output=True, text=True, check=True)
            self.assertEqual(result.stdout, 'doctor --help\n')
            args = ['--remote', 'ws://example.invalid:4500', '--remote-auth-token-env', 'TEST_TOKEN', 'resume', 'saved-thread']
            result = subprocess.run([str(launcher), *args], cwd=cwd, capture_output=True, text=True, check=True)
            self.assertEqual(json.loads(result.stdout)['args'], ['--cd', str(cwd.resolve())] + args)

    def test_help_version_and_admin_commands_bypass_remote_credentials(self):
        with tempfile.TemporaryDirectory(prefix='spark routing ') as directory:
            root = Path(directory)
            direct = root / 'direct cli'
            direct.write_text('#!/usr/bin/env python3\nimport json, sys\nprint(json.dumps(sys.argv[1:]))\n')
            direct.chmod(0o755)
            original = '#!/bin/sh\nset -eu\necho "remote credentials must not be read" >&2\nexit 99\n'
            routed = install_client.with_command_routing(original, direct)
            self.assertEqual(install_client.with_command_routing(routed, direct), routed)
            launcher = root / 'codex-spark'
            launcher.write_text(routed)
            launcher.chmod(0o755)
            for args in [['--help'], ['--version'], ['help', 'exec'], ['exec', 'a literal $(prompt)'], ['completion', 'bash'], ['update'], ['mcp', 'list']]:
                with self.subTest(args=args):
                    result = subprocess.run([str(launcher), *args], capture_output=True, text=True, check=True)
                    self.assertEqual(json.loads(result.stdout), args)
                    self.assertEqual(result.stderr, '')

    def test_unrecognized_launcher_is_not_modified(self):
        with self.assertRaisesRegex(ValueError, 'was not changed'):
            install_client.with_default_cwd('#!/bin/sh\nexec custom-client "$@"\n')


if __name__ == '__main__':
    unittest.main()
