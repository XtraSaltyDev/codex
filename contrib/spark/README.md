# Personal Codex / Spark setup

This branch keeps the upstream Rust agent intact and adds a small deployment layer for a Linux development host with a separate model server.

The runtime uses a custom Responses provider, a model catalog matched to the served model, a private loopback app-server with bearer authentication, and separate Codex state. Credentials, inference keys, and generated configuration stay outside this repository.

For the existing Goose iPhone app, see [Goose mobile with Codex Spark](GOOSE_MOBILE.md). Its ACP adapter runs on Linux beside the app-server and uses the existing private WireGuard endpoint.

## Build

Use the Rust version in `codex-rs/rust-toolchain.toml` and the Linux build prerequisites in the upstream CI workflow. From `codex-rs`:

```sh
CARGO_BUILD_JOBS=8 cargo build --release --locked -p codex-cli --bin codex
```

Keep the checkout on `personal/spark`; `upstream` points to OpenAI and `origin` points to the personal fork. Fetch and review upstream changes before merging them into this branch, rebuilding, and rerunning acceptance.

## Private model connection

Run a user service called `codex-spark-tunnel.service` that forwards a local port to the inference server's loopback port over authenticated SSH. Use a dedicated forwarding-only key, pin the server's host key, and keep the inference server's existing listener private. The default profile below expects `http://127.0.0.1:18384/v1`.

On an always-on host, enable user-service persistence with `sudo loginctl enable-linger "$USER"`. This keeps the services available after a desktop logout.

## Install the profile and app-server

```sh
python3 contrib/spark/configure.py --base-url http://127.0.0.1:18384/v1
```

The setup discovers the served model and context limit through `/v1/models`. It installs:

- `~/.local/bin/codex-spark`: authenticated terminal client for the persistent app-server.
- `~/.local/bin/codex-spark-cli`: direct CLI with the isolated local-model profile, including `exec` and administrative commands.
- `~/.local/bin/codex-spark-server`: app-server launcher.
- `~/.config/systemd/user/codex-spark-app-server.service`: loopback listener at port 4500, with a private capability token.
- `~/.codex-spark/`: configuration, model metadata, sessions, and history separate from `~/.codex`.
- A **Codex Spark** application-menu launcher.

New threads default to full access: `sandbox_mode = "danger-full-access"` and `approval_policy = "never"`. Commands run with the Linux user's filesystem and network permissions, without Codex approval prompts. Existing loaded threads keep their current policy; recovery reviews remain read-only with networking disabled.

The model catalog selects plain function tools and terminal-based file editing. Code Mode, hosted web search, reasoning summaries, automatic child agents, ChatGPT app integrations, and plugin marketplace auto-loading are disabled in this baseline. The model uses terminal tools such as `curl` or Python to retrieve current sources and cite their URLs; OpenAI-hosted web search is not implemented by the Spark provider. `[sandbox_workspace_write] network_access = true` also preserves networking when workspace mode is explicitly selected. These choices are editable in the isolated runtime's configuration. This config does not grant a local model capabilities it cannot implement.

```sh
systemctl --user status codex-spark-tunnel codex-spark-app-server
journalctl --user -u codex-spark-app-server -n 50
~/.local/bin/codex-spark --cd /path/to/linux/project
~/.local/bin/codex-spark-cli --version
```

Reach the terminal client over SSH when using another computer. The model runs on Spark; shell commands and repository edits run on the Linux development host. Files on a Mac need a Mac runtime or an explicit copy/checkout on Linux.

## Health diagnostics

```sh
codex-spark doctor
codex-spark doctor --json
codex-spark doctor --live
codex-spark doctor --live --mobile-url ws://10.66.66.1:3284/acp
```

`doctor` checks the three user services, authenticates to the app-server and mobile adapter, queries the
effective provider's model catalog, and checks journal integrity, private permissions,
write access and free disk space. Routine checks do not start turns or run inference.
`--live` adds one read-only terminal marker command in a new diagnostic chat, checks
the successful recorded tool result and durable submission receipt, disconnects,
reloads saved history and archives the finished fixture. The total live deadline is
90 seconds by default (`--timeout 10` through `--timeout 180`). A failure returns the
diagnostic session ID; it never resends the prompt or retries a tool action. Unfinished
fixtures remain available for inspection. Exit status is 0 for passing checks and 1
when attention is needed. A successful catalog check alone does not prove inference.

Install diagnostics on an existing host without rewriting the profile or restarting
the native runtime:

```sh
python3 contrib/spark/install_diagnostics.py
```

This installs the shared Python 3.11+ helper and its isolated aiohttp dependency and
adds the `doctor` subcommand before the launcher's normal token read, so it can diagnose
missing credentials. Normal terminal arguments continue to go to the remote client.
New installations include it through `configure.py` and `configure_goose_mobile.py`.

## Coding acceptance

Install the test client's dependency into an isolated directory, then run:

```sh
python3 -m pip install --target "$HOME/.local/share/codex-spark/python" websocket-client
python3 contrib/spark/smoke_app_server.py
```

This creates a disposable Git fixture under `~/.local/state/codex-spark/acceptance/`. It requires a real model to reproduce a failing test, edit the file, rerun tests, complete the turn, reconnect to the app-server, resume the same conversation, and execute a follow-up tool call. Results and protocol events stay in that fixture. A health probe or a model-list response alone does not pass this check.
