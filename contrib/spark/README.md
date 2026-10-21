# Codex Spark

This personal fork adds Codex Spark branding and a deployment layer for a Linux development host with a separate model server.

The runtime uses a custom Responses provider, a model catalog matched to the served model, a private loopback app-server with bearer authentication, and separate Codex state. Credentials, inference keys, and generated configuration stay outside this repository.

For the existing Goose iPhone app, see [Goose mobile with Codex Spark](GOOSE_MOBILE.md). Its ACP adapter runs on Linux beside the app-server and uses the existing private WireGuard endpoint.

## Version

The first Codex Spark release is **0.1.0**. The Rust workspace version in
`codex-rs/Cargo.toml` is the release version for the CLI, TUI, and app-server.
`codex-spark --version`, `codex-spark doctor --version`, and the mobile handshake
report the same version. Releases are maintained from this fork;
upstream installer and update promotions are disabled. See [CHANGELOG.md](CHANGELOG.md).

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

Planning is enabled with `[tools.update_plan] enabled = true`. The Spark catalog
adapts the upstream patch instruction to call `exec_command` with a `cmd` string
and a quoted `apply_patch` heredoc. Codex retains its existing patch validation,
sandbox, approvals, and file-change events. The dedicated freeform patch tool stays
disabled so the advertised tools remain ordinary functions. The upstream prompt
file is unchanged; the adaptation replaces only its obsolete patch-call example
and refuses unknown or ambiguous versions of that instruction.

HTTP context-limit errors use Codex's existing `ContextWindowExceeded` handling.
This includes explicit `context_length_exceeded` codes and vLLM's HTTP 400
`BadRequestError` for `input_tokens` with its maximum-context-length message.
Other request errors retain their original classification. Recognized overflow
marks the context full, and local compaction can use its existing history-trimming
retry. A single message larger than the model context still needs to be shortened.

```sh
systemctl --user status codex-spark-tunnel codex-spark-app-server
journalctl --user -u codex-spark-app-server -n 50
~/.local/bin/codex-spark --cd /path/to/linux/project
~/.local/bin/codex-spark-cli --version
```

From a terminal, `codex-spark` starts new chats in the terminal's current working
directory, even though execution uses the persistent app-server. An explicit
`--cd /path/to/project` or `-C /path/to/project` overrides that default. The
application-menu shortcut opens the fork's project directory explicitly.
Help, version, completions, and administrative commands such as `exec` and `mcp`
go directly to the isolated CLI without requiring the app-server token.

To update an existing terminal launcher without changing the model profile or
restarting any services, run:

```sh
python3 contrib/spark/install_client.py
```

Launcher backups are stored under `~/.local/state/codex-spark/backups/launchers/`.

## Model reasoning choices

Setup probes the Responses reasoning levels `none`, `low`, `medium`, `high`, and
`xhigh`. It advertises a level only after a complete answer and provider usage
confirm that thinking is off for `none` and active for the other levels. Explicit
reasoning-level rejections exclude that level; outages, unrelated request errors,
ignored effort settings, and inconclusive responses leave the catalog unchanged.
The current Spark template verifies `none`, `low`, `medium`, and `xhigh`; `high`
is rejected. The Spark profile sets `model_reasoning_effort = "low"` for new chats;
select `none` when no thinking is preferred. Reasoning summaries remain disabled
independently of reasoning effort.

Inspect or refresh an existing catalog:

```sh
python3 contrib/spark/model_reasoning.py
# After running tasks finish:
python3 contrib/spark/model_reasoning.py --refresh
systemctl --user restart codex-spark-app-server.service codex-spark-mobile.service
```

Refresh backs up the catalog and changes only the selected model's reasoning
choices. It preserves defaults, input modalities, tool instructions, and
`config.toml`, and saves probe evidence in `~/.codex-spark/model-reasoning.json`.
In the terminal model picker, select a verified level, or use
`/model --reasoning=medium`. Direct CLI runs also accept
`codex-spark-cli -c model_reasoning_effort='"medium"' exec "Your task"`.
The mobile adapter continues to use the shared backend default; this change does
not add an effort selector to the phone UI.

Run `python3 contrib/spark/smoke_model_reasoning.py` to verify native request
serialization against Spark with a disposable catalog, or add `--installed` to
check the running app-server's advertised choices and actual per-turn reasoning
usage. These checks disable tools and keep the production profile unchanged.

## Model input capabilities

The installer uses explicit `input_modalities` metadata when the provider supplies
it. Otherwise it sends a small generated image through the configured Responses
endpoint and verifies the model's answer. Model names and missing metadata do not
establish text-only support. An explicit image rejection disables image input;
network errors, authentication failures, and inconclusive answers leave capability
detection unresolved. The fallback probe makes no claim about audio support.

Inspect the configured model without changing the profile:

```sh
python3 contrib/spark/model_capabilities.py
```

To refresh an existing catalog, wait for running tasks to finish, then run:

```sh
python3 contrib/spark/model_capabilities.py --refresh
systemctl --user restart codex-spark-app-server.service codex-spark-mobile.service
```

The refresh backs up the catalog and updates only the selected model's modalities.
It preserves `config.toml`, credentials, and other model settings, and saves detection
evidence in `~/.codex-spark/model-capabilities.json`. An inconclusive probe leaves the
catalog unchanged. For a separately verified deployment, both the installer and
refresh command accept an explicit override such as `--input-modalities text,image`.

Verified image support enables native attachments and `view_image`. The mobile
adapter reads the native model catalog and advertises the image prompt capability
for the configured model. Provider-declared modalities are preserved in the catalog;
the fallback probe verifies images, and the mobile adapter forwards text and images.

## Update an existing host

After building the reviewed fork revision, wait for running tasks to finish and
back up the installed binary and configuration under `~/.local/state/codex-spark/`.
Replace only the runtime and helper files; the initial profile installer is for
setting up a new host. From the repository root:

```sh
install -m 755 codex-rs/target/release/codex ~/.local/lib/codex-spark/codex.new
strip --strip-debug ~/.local/lib/codex-spark/codex.new
mv ~/.local/lib/codex-spark/codex.new ~/.local/lib/codex-spark/codex
python3 contrib/spark/install_client.py
python3 contrib/spark/install_diagnostics.py
systemctl --user restart codex-spark-app-server.service codex-spark-mobile.service
codex-spark --version
codex-spark doctor --live
```

This preserves the existing provider, credentials, networking, and chat databases.
The Spark tunnel and other host services continue running during the update.

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

Existing chats receive newer approved Spark stock prompts when resumed and when
building a model request. The native guard requires provider `spark`, model-owned
provenance for the same model, and exact SHA-256 matches in the ordered revision
registry in `codex-rs/prompts/src/spark_instructions.rs`. Custom or modified prompts,
unknown provenance, other providers/models, and downgrades are preserved. The
upgrade changes the model request, without rewriting saved history, developer
instructions, or AGENTS.md content. Add each released stock prompt fingerprint as
a new revision before shipping future prompt changes; keep earlier fingerprints.

`smoke_prompt_upgrade.py --report-dir /tmp/prompt-upgrade-check` creates disposable
legacy/custom chats and archives them before seeding only the legacy fixture's
saved prompt metadata. It waits for a `deployed` file before forwarding actual
requests to Spark and checking their model-visible instructions. Its `ready.json`
supplies chat IDs for the focused iPhone UI test. After that test, create `release`
to verify saved history prefixes and fixture tests, delete both chats and folders,
remove automatic trust entries, and check unchanged production inventory/profile.

Recovery review is consumed atomically with native acceptance, rather than when
the phone taps Review. Preflight failures and explicit native rejections leave a
prepared receipt available for manual retry, including after reconnect/restart.
An uncertain dispatch remains protected against resubmission. Native started
events, responses and reconciled user-message receipts can confirm acceptance.
Older prematurely consumed checkpoints are repaired when their prepared receipt
proves dispatch never began. Review always uses read-only access and never grants
permission to repeat interrupted commands.

```sh
python3 contrib/spark/smoke_recovery_review.py --report-dir /tmp/recovery-review-check
```

This creates one temporary folder and one disposable chat without adding a mobile
project. Real Spark appends one fixture marker, then native interruption creates a
saved checkpoint.
Temporarily archiving only that chat forces a real launch failure. After restoring
it, Review must succeed exactly once without appending the marker again. The check
deletes the chat and folder, removes its automatic native trust entry, and verifies
unchanged chat/project inventory and profile/binary hashes. For physical-phone
acceptance, `--hold 900` waits for `arm`, `restore` and `release` files in the report
directory at the UI test's three stages.

Each mobile connection has one ordered sender, independent of the native reader.
Failed or backlogged senders are disconnected without interrupting their runs.
The live queue is capped at 256 messages or 16 MiB, including the in-flight message;
each frame has a ten-second send deadline and socket closure is bounded to one
second. History is sent lazily from at most two queued snapshots, followed by live
events in order. Existing history replay restores an evicted connection without
automatically resending a prompt or tool action.

On the Linux deployment, verify real TCP backpressure alongside real Spark work:

```sh
python3 contrib/spark/smoke_mobile_delivery.py --report-dir /tmp/mobile-delivery-check
```

This uses two disposable chats in the existing project. One retains an oversized
synthetic user message for a stalled reader; the other receives normal Spark
answers. The check measures the kernel's blocked send queue, native RPC latency,
healthy streamed chunks, eviction, and unchanged inventory after deleting both
chats. `--hold 900` cycles stalled peers during physical-phone verification until
a `release` file appears in the report directory. No provider/profile change or
native restart is required.

Mobile failed turns carry a bounded, credential-redacted `error` in the live
`hostRun` and saved `hostRuns`, plus the prompt RPC error. The error includes a
category, message and output-limit flag; private `additionalDetails` stay on the
host. Nonterminal retry errors and cancellations do not become failed responses.
Duplicate receipts return the saved failure without dispatching another turn.

Verify the installed adapter with a real Spark context-limit rejection and a
successful follow-up:

```sh
python3 contrib/spark/smoke_mobile_errors.py --report-dir /tmp/mobile-error-check
```

This creates one disposable chat in the existing Codex project. A loopback relay
used only by that chat sends synthetic oversized input to Spark, forwarding its
actual rejection through the native runtime. It deletes the chat in `finally` and
checks the complete chat/project inventory and unchanged profile/binary hashes.
For physical-phone verification, `--hold 900` waits for a `release` file in the
report directory; send the failure prompt and a follow-up from that owned chat.

Verify planning and patch handling against the live Spark model in a disposable
project:

```sh
python3 contrib/spark/smoke_tool_profile.py
python3 contrib/spark/smoke_tool_profile.py --installed
```

The first command uses an isolated profile. The second uses the deployed shared
app-server without tool or prompt overrides. Both require a plan, a native patch
event, passing fixture tests, and an unchanged test file. Reports stay under
`~/.local/state/codex-spark/tool-profile-acceptance/`.

Install the test client's dependency into an isolated directory, then run:

```sh
python3 -m pip install --target "$HOME/.local/share/codex-spark/python" websocket-client
python3 contrib/spark/smoke_app_server.py
```

This creates a disposable Git fixture under `~/.local/state/codex-spark/acceptance/`. It requires a real model to reproduce a failing test, edit the file, rerun tests, complete the turn, reconnect to the app-server, resume the same conversation, and execute a follow-up tool call. Results and protocol events stay in that fixture. A health probe or a model-list response alone does not pass this check.
