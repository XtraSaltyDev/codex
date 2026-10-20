#!/usr/bin/env python3
"""Update the Codex Spark terminal launcher without restarting services."""

import datetime
from pathlib import Path
import shutil
import shlex


CWD_DEFAULT = '''# Use the calling terminal directory unless --cd/-C was supplied.
codex_spark_default_cwd=true
for codex_spark_arg in "$@"; do
  case "$codex_spark_arg" in
    --) break ;;
    --cd|--cd=*|-C|-C?*) codex_spark_default_cwd=false; break ;;
  esac
done
if "$codex_spark_default_cwd"; then
  set -- --cd "$(pwd -P)" "$@"
fi
'''


def with_command_routing(text, direct_cli):
    marker = '# Codex Spark commands that do not use the interactive remote client.'
    if marker in text:
        return text
    routing = marker + '''
if [ "${1-}" = help ] && [ "${2-}" = doctor ]; then
  shift
  set -- "$@" --help
fi
case "${1-}" in
  --help|-h|--version|-V|help|exec|e|review|login|logout|mcp|plugin|app-server|remote-control|completion|update|sandbox|debug|apply|a|queue|archive|delete|unarchive|cloud|cloud-tasks|exec-server|features)
    exec ''' + shlex.quote(str(direct_cli)) + ''' "$@" ;;
esac
'''
    commands = [line for line in text.splitlines(keepends=True)
                if line.startswith('exec ') and ' --remote ' in line]
    if len(commands) == 1:
        # Resume hints can already contain the server and auth environment flag.
        # Preserve them instead of supplying a second --remote option.
        explicit_remote = '''for codex_spark_arg in "$@"; do
  case "$codex_spark_arg" in
    --) break ;;
    --remote|--remote=*) exec ''' + shlex.quote(str(direct_cli)) + ''' "$@" ;;
  esac
done
'''
        text = text.replace(commands[0], explicit_remote + commands[0], 1)
    first, rest = text.split('\n', 1)
    return first + '\n' + routing + rest


def with_default_cwd(text):
    if CWD_DEFAULT in text:
        return text
    commands = [line for line in text.splitlines(keepends=True)
                if line.startswith('exec ') and ' --remote ' in line]
    if len(commands) != 1:
        raise ValueError('Expected one Spark remote-client exec command; launcher was not changed.')
    return text.replace(commands[0], CWD_DEFAULT + commands[0], 1)


def install():
    launcher = Path.home() / '.local/bin/codex-spark'
    current = launcher.read_text()
    updated = with_command_routing(with_default_cwd(current), launcher.with_name('codex-spark-cli'))
    if updated != current:
        stamp = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
        backups = Path.home() / '.local/state/codex-spark/backups/launchers'
        backups.mkdir(parents=True, exist_ok=True, mode=0o700)
        shutil.copy2(launcher, backups / (launcher.name + '.' + stamp))
        temporary = launcher.with_name('codex-spark.new')
        temporary.write_text(updated)
        temporary.chmod(0o755)
        temporary.replace(launcher)


if __name__ == '__main__':
    install()
    print('Updated the Codex Spark launcher; no services restarted.')
