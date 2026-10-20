#!/usr/bin/env python3
"""Install this fork's isolated Spark profile, launchers, and user service."""

import argparse
import datetime
import json
from pathlib import Path
import secrets
import shlex
import shutil
import subprocess
import urllib.request


NETWORK_INSTRUCTIONS = (
    'For ordinary workspace-write tasks, outbound Internet access is enabled. '
    'For recent events and other time-sensitive facts, use terminal tools such as '
    'curl or Python urllib to retrieve current sources, inspect publication dates, '
    'and cite the source URLs. News RSS feeds can help discover current reporting. '
    'The Spark provider does not supply OpenAI-hosted web_search; its absence does '
    'not mean terminal network access is unavailable. Attempt a relevant fetch '
    'before reporting a connectivity limitation, and describe the actual failure. '
    'Always obey the effective turn sandbox: recovery reviews remain read-only '
    'with network disabled and must not repeat previous actions.'
)


def write(path, contents, mode=0o600):
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.read_text() != contents:
        stamp = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
        shutil.copy2(path, path.with_name(path.name + '.before-' + stamp))
    path.write_text(contents)
    path.chmod(mode)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-url', default='http://127.0.0.1:18384/v1')
    parser.add_argument('--model')
    parser.add_argument('--port', type=int, default=4500)
    parser.add_argument('--binary', type=Path)
    args = parser.parse_args()
    repo = Path(__file__).resolve().parents[2]
    home = Path.home()
    state = home / '.local/state/codex-spark'
    runtime = home / '.codex-spark'
    config = home / '.config/codex-spark'
    launchers = home / '.local/bin'
    install = home / '.local/lib/codex-spark'
    for directory in (state, runtime, config, launchers, install):
        directory.mkdir(parents=True, exist_ok=True)
    for directory in (state, runtime, config):
        directory.chmod(0o700)

    binary = args.binary or repo / 'codex-rs/target/release/codex'
    if not binary.is_file():
        raise SystemExit(f'Build the fork first: missing {binary}')
    with urllib.request.urlopen(args.base_url.rstrip('/') + '/models', timeout=10) as response:
        models = json.load(response)['data']
    model = next((m for m in models if not args.model or m['id'] == args.model), None)
    if model is None:
        raise SystemExit('The requested model is not served by this endpoint')
    name = model['id']
    context = int(model.get('max_model_len', 131072))
    catalog = {
        'models': [{
            'slug': name,
            'display_name': 'Spark / ' + name.rsplit('/', 1)[-1],
            'description': 'Self-hosted model reached through the private Spark tunnel.',
            'default_reasoning_level': None,
            'supported_reasoning_levels': [],
            'shell_type': 'unified_exec',
            'visibility': 'list',
            'supported_in_api': True,
            'priority': 0,
            'availability_nux': None,
            'upgrade': None,
            'model_messages': {'instructions_template': (repo / 'codex-rs/models-manager/prompt.md').read_text()},
            'include_skills_usage_instructions': False,
            'include_plugin_usage_instructions': False,
            'include_apps_usage_instructions': False,
            'supports_reasoning_summary_parameter': False,
            'default_reasoning_summary': 'none',
            'support_verbosity': False,
            'default_verbosity': None,
            'apply_patch_tool_type': None,
            'truncation_policy': {'mode': 'tokens', 'limit': 10000},
            'context_window': context,
            'max_context_window': context,
            'auto_compact_token_limit': min(200000, int(context * 0.80)),
            'effective_context_window_percent': 95,
            'experimental_supported_tools': [],
            'input_modalities': ['text'],
            'node_repl_disabled': True,
            'tool_mode': 'direct',
        }]
    }
    write(runtime / 'models.json', json.dumps(catalog, indent=2) + '\n')
    toml = f'''# Personal runtime; separate from ~/.codex.
model = {json.dumps(name)}
model_provider = "spark"
model_catalog_json = {json.dumps(str(runtime / 'models.json'))}
model_context_window = {context}
model_auto_compact_token_limit = {min(200000, int(context * 0.80))}
approval_policy = "on-request"
sandbox_mode = "workspace-write"
web_search = "disabled"
developer_instructions = {json.dumps(NETWORK_INSTRUCTIONS)}

[sandbox_workspace_write]
network_access = true

[model_providers.spark]
name = "DGX Spark"
base_url = {json.dumps(args.base_url.rstrip('/'))}
wire_api = "responses"
requires_openai_auth = false
supports_websockets = false
request_max_retries = 2
stream_max_retries = 2
stream_idle_timeout_ms = 180000

[model_providers.spark.capabilities]
remote_compaction = "unsupported"

[features]
multi_agent = false
code_mode = false
code_mode_host = false
apps = false
plugins = false
tool_suggest = false

[analytics]
enabled = false
'''
    write(runtime / 'config.toml', toml)
    destination = install / 'codex'
    temporary = install / 'codex.new'
    shutil.copy2(binary, temporary)
    subprocess.run(['strip', '--strip-debug', str(temporary)], check=True)
    temporary.chmod(0o755)
    temporary.replace(destination)
    token = config / 'server.token'
    if not token.exists():
        write(token, secrets.token_urlsafe(48) + '\n')
    token.chmod(0o600)
    quote = shlex.quote
    raw = launchers / 'codex-spark-cli'
    write(raw, '#!/bin/sh\nexec env CODEX_HOME=' + quote(str(runtime)) + ' ' + quote(str(destination)) + ' "$@"\n', 0o755)
    server = launchers / 'codex-spark-server'
    write(server, '#!/bin/sh\nexec ' + quote(str(raw)) + f' app-server --strict-config --listen ws://127.0.0.1:{args.port} --ws-auth capability-token --ws-token-file ' + quote(str(token)) + ' "$@"\n', 0o755)
    client = launchers / 'codex-spark'
    write(client, '#!/bin/sh\nset -eu\nCODEX_SPARK_REMOTE_TOKEN=$(cat ' + quote(str(token)) + ')\nexport CODEX_SPARK_REMOTE_TOKEN\nexec ' + quote(str(raw)) + f' --remote ws://127.0.0.1:{args.port} --remote-auth-token-env CODEX_SPARK_REMOTE_TOKEN "$@"\n', 0o755)
    unit = home / '.config/systemd/user/codex-spark-app-server.service'
    write(unit, f'''[Unit]
Description=Personal Codex fork app-server using DGX Spark inference
Wants=codex-spark-tunnel.service
After=codex-spark-tunnel.service

[Service]
Type=simple
ExecStart={server}
WorkingDirectory={home / 'Projects'}
Environment=RUST_LOG=warn
Restart=on-failure
RestartSec=3
KillSignal=SIGINT
UMask=0077

[Install]
WantedBy=default.target
''', 0o644)
    write(home / '.local/share/applications/codex-spark.desktop', f'''[Desktop Entry]
Type=Application
Name=Codex Spark
Comment=Personal Codex fork using the DGX Spark model
Exec="{client}" --cd "{repo}"
Path={repo}
Terminal=true
Icon=utilities-terminal
Categories=Development;
''', 0o644)
    metadata = {
        'source_commit': subprocess.check_output(['git', '-C', str(repo), 'rev-parse', 'HEAD'], text=True).strip(),
        'binary_version': subprocess.check_output([str(destination), '--version'], text=True).strip(),
        'model': name, 'base_url': args.base_url, 'context_window': context,
        'runtime': str(runtime), 'app_server': f'ws://127.0.0.1:{args.port}',
    }
    write(state / 'installation.json', json.dumps(metadata, indent=2) + '\n')
    subprocess.run(['systemctl', '--user', 'daemon-reload'], check=True)
    subprocess.run(['systemctl', '--user', 'enable', 'codex-spark-app-server.service'], check=True)
    subprocess.run(['systemctl', '--user', 'restart', 'codex-spark-app-server.service'], check=True)
    print(json.dumps(metadata, indent=2))


if __name__ == '__main__':
    main()
