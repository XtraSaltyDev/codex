#!/usr/bin/env python3
"""Install the mobile adapter after configure.py. Does not alter network routing."""
import pathlib
import secrets
import subprocess
import sys

home = pathlib.Path.home()
source = pathlib.Path(__file__).resolve().parent
token = home / '.config/codex-spark/mobile.token'
if not (home / '.config/codex-spark/server.token').is_file():
    raise SystemExit('Configure the Codex Spark app-server first.')
subprocess.run([sys.executable, '-m', 'pip', 'install', '--disable-pip-version-check', '--target', str(home / '.local/share/codex-spark/mobile-python'), '-r', str(source / 'requirements-mobile.txt')], check=True)
if not token.exists():
    token.write_text(secrets.token_urlsafe(32) + '\n')
token.chmod(0o600)
unit = home / '.config/systemd/user/codex-spark-mobile.service'
unit.write_text(f'''[Unit]
Description=Goose mobile ACP adapter for the personal Codex Spark backend
Wants=codex-spark-app-server.service
After=codex-spark-app-server.service

[Service]
ExecStart=/usr/bin/python3 "{source / 'goose_mobile.py'}"
WorkingDirectory=%h/Projects/codex-spark
Restart=on-failure
RestartSec=3
UMask=0077

[Install]
WantedBy=default.target
''')
subprocess.run(['systemctl', '--user', 'daemon-reload'], check=True)
subprocess.run(['systemctl', '--user', 'enable', '--now', unit.name], check=True)
print('Mobile adapter enabled on 127.0.0.1:3285. Pairing secret remains in ~/.config/codex-spark/mobile.token.')
