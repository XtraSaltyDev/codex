#!/usr/bin/env python3
"""Install diagnostics without changing model configuration or restarting services."""
from pathlib import Path
import shlex
import sys
import subprocess


def install():
    home = Path.home()
    source = Path(__file__).resolve().parent
    dependencies = home / '.local/share/codex-spark/mobile-python'
    sys.path.insert(0, str(dependencies))
    try:
        import aiohttp
    except ImportError:
        subprocess.run([sys.executable, '-m', 'pip', 'install', '--disable-pip-version-check', '--target', str(dependencies), '-r', str(source / 'requirements-mobile.txt')], check=True)
    target = home / '.local/share/codex-spark/diagnostics'
    target.mkdir(parents=True, exist_ok=True, mode=0o700)
    for name in ('health.py', 'doctor.py'):
        temporary = target / (name + '.new')
        temporary.write_bytes((source / name).read_bytes())
        temporary.chmod(0o600)
        temporary.replace(target / name)
    launcher = home / '.local/bin/codex-spark'
    text = launcher.read_text()
    marker = 'if [ "${1-}" = doctor ]; then'
    if marker not in text:
        first, rest = text.split('\n', 1)
        text = first + '\n' + marker + '\n  shift\n  exec ' + shlex.quote(sys.executable) + ' ' + shlex.quote(str(target / 'doctor.py')) + ' "$@"\nfi\n' + rest
        temporary = launcher.with_name('codex-spark.new')
        temporary.write_text(text)
        temporary.chmod(0o755)
        temporary.replace(launcher)


if __name__ == '__main__':
    install()
    print('Installed codex-spark doctor; no services restarted.')
