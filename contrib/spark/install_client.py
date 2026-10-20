#!/usr/bin/env python3
"""Update the Spark terminal launcher without changing or restarting services."""

import datetime
from pathlib import Path
import shutil


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
    updated = with_default_cwd(current)
    if updated != current:
        stamp = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
        shutil.copy2(launcher, launcher.with_name(launcher.name + '.before-cwd-' + stamp))
        temporary = launcher.with_name('codex-spark.new')
        temporary.write_text(updated)
        temporary.chmod(0o755)
        temporary.replace(launcher)


if __name__ == '__main__':
    install()
    print('Updated codex-spark to use the terminal working directory; no services restarted.')
