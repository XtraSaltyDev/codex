# Goose mobile with Codex Spark

The existing Swift Goose app speaks ACP. `goose_mobile.py` translates that protocol
to the already-running, capability-token-authenticated Codex app-server. Codex owns
the agent, sandbox, approvals, local files, saved threads, and model requests to Spark.
The Mac and Goose desktop are not in the phone's execution path.

## Install on ai-research

After the main Spark setup, run:

```sh
python3 contrib/spark/configure_goose_mobile.py
python3 -m unittest discover -s contrib/spark -p test_goose_mobile.py -v
python3 contrib/spark/smoke_goose_mobile.py
```

The user service is `codex-spark-mobile.service`, listening on loopback port 3285.
Its independent mobile secret is `~/.config/codex-spark/mobile.token`, mode 0600.
The upstream Codex token is never sent to the phone. Access logs are disabled to
avoid retaining transport credentials. Both credentials stay outside this repository.

The existing WireGuard-only socket `10.66.66.1:3284` forwards to `127.0.0.1:3285`
through a drop-in at `/etc/systemd/system/goose-wireguard.service.d/codex.conf`:

```ini
[Service]
ExecStart=
ExecStart=/usr/lib/systemd/systemd-socket-proxyd 127.0.0.1:3285
```

This changes only the Goose forwarding service. The WireGuard interface, DNS,
AdGuard, Caddy, original Mac host, and original reverse tunnel are preserved.
Never expose either app-server or adapter directly to the public Internet.

The phone's saved host is **Codex Spark on ai-research**:

- Address: `http://10.66.66.1:3284`, using the existing encrypted WireGuard tunnel.
- Secret: the independent mobile token above, saved to device-only Keychain.
- Working directory: `/home/xtrasalty/Projects/codex-spark`.
- Local HTTP option: enabled because WireGuard provides transport encryption.

WireGuard must be connected on the phone. Original Mac conversations and drafts
stay on their original host; they are not converted into Codex conversations.

## Behavior and limits

Deleting an already-missing backend chat also clears its stale phone draft. A missing-rollout
error is accepted only after confirming the ID is neither loaded nor indexed in any page
of active or archived chats, across all source kinds and providers. Indexed work, transport
failures and permission errors preserve the local draft and recovery data. Successful deletion
also removes that chat's saved turns, submissions, events and file-review records; project
files are untouched.

The authenticated workspaces RPC advertises `workspaceBrowsing` and `workspaceCreation`.
`browse` lists host directories (200 per page), with canonical current path, parent,
home, and optional hidden entries. `createFolder` creates one child of the selected
directory with a validated name and exclusive mkdir; existing targets are never
overwritten. Creation is explicit and is not automatically retried after a lost reply.
Browsing never creates a Codex thread or invokes the model. Only choosing a folder
adds it to saved projects. Goose's checkmark selects the current directory; cancelling
leaves the compose workspace and draft intact. These operations use the Linux user's
filesystem permissions and the existing private mobile authentication.

Completed mobile runs include an inline changed-files summary, per-file added/removed
line counts and saved unified diffs. Tap a file to inspect its captured diff. The
baseline is the working tree at task start, including pre-existing dirty/untracked
files, so unchanged earlier edits are excluded. Shell edits and changes committed
during the task are captured too. Renames appear as delete/add entries. Git projects
include tracked and non-ignored files; ordinary folders use bounded snapshots and
exclude generated folders and credential files. Edits outside the project and
external-service changes are outside this file review's scope. Overlapping work,
interrupted captures and size limits are labeled as partial rather than attributed
with false certainty. Snapshot files are never applied or restored to the project.

Command activity now includes the recorded output and exit code. Nonzero exits are
shown as failed commands even when the surrounding turn succeeds. Output is capped
at 24 KiB, common credential forms and terminal control sequences are filtered, and
truncation or unconfirmed outcomes are explicit. This does not infer that an
unexecuted test passed. Existing stored command results can be replayed; old runs
without before-task snapshots do not gain invented file diffs.

Private baseline/result records live in the existing recovery SQLite database's
`work_reviews` table. Baseline contents are removed after saving the bounded result.
The `workReview` field on host-run metadata and `_meta.goose.commandResult` on tool
updates survive normal reconnects and adapter restarts. A recovery capture after an
interruption is marked partial, and never repeats a prompt, tool action or file edit.

The adapter advertises `healthStatus: true`. Goose polls the authenticated `/status`
endpoint while foregrounded and shows the same service, authenticated Codex, model
availability and saved-work checks as `codex-spark doctor`. Tap the library health row
or open Backend details to see individual results and refresh them. HTTP 503 retains
the structured failure report; missing/wrong mobile credentials return 401. Status
responses contain no inference URLs or credentials, use `Cache-Control: no-store`,
and are briefly cached on the host to coalesce simultaneous readers. The ACP method
`_goose/unstable/health` exposes the same read-only report to authenticated clients.
Health never runs a live test, resends a message, or changes draft/submission state.
The phone checks HTTP independently of its ACP connection so a running adapter can
explain a Codex outage. An unreachable host is reported with a private/VPN connection
hint; the app does not pretend it can determine that the VPN itself is disconnected.
Older hosts without this capability retain the existing connection behavior.

Supports streaming text, command/file activity, explicit allow-once/decline approvals,
user-input forms, stop, history replay, saved-message search, rename, archive/restore,
delete, project folders, and fresh projectless workspaces. Mobile-created Codex threads
use legacy history mode for full ACP transcript replay. Model execution remains on
the existing Spark provider. This adapter does not advertise a model picker; change
the backend model through the isolated Codex Spark configuration.

New-chat composition shows the execution host and a folder menu above the composer.
Choose a saved project, add an existing host folder, or choose **Don't work in a
project**. Folder selection and the unsent draft are saved per host on the phone;
the backend conversation is created only when the first message is sent. New
projectless chats get separate folders under `~/Codex` on ai-research (override
with the adapter's `--projectless-root` option). Earlier chats retain their original
working folders and files. This selector does not move an existing conversation.

New mobile chats default to full access with approvals disabled, matching the direct
CLI profile. The adapter saves that choice per chat so ordinary turns retain it
after reconnects, restarts, and recovery reviews. Existing workspace chats keep their
workspace policy and outbound networking. Full-access CLI chats loaded on the phone
retain full access. Current information can be retrieved with terminal tools (`curl`
or Python); the Spark provider does not implement OpenAI-hosted web search. Recovery
review turns keep their read-only, network-disabled policy.

The initialize response includes a whitelisted `agentCapabilities._meta.goose.backend`
descriptor: Codex identity, execution hostname, configured model/provider, and new-chat
access defaults. Per-chat `_meta.goose.sessionSettings` accompanies new/load/list and
submission-receipt updates, preserving actual model IDs and ordinary-turn access.
Recovery reviews retain their separate restricted policy. No provider credentials or
inference URLs are included. `workspaceAssignment: false` lets clients hide project
move controls while still offering project discovery and new chats in project folders.

Disconnecting or closing the phone does not cancel host work. Reopening a running
thread replays its current output and pending approvals; prompts are never retried
automatically. An adapter restart rejoins the existing Codex turn. A backend crash
preserves the message, partial output, and tool activity in a SQLite WAL journal with
full synchronous commits at `~/.local/state/codex-spark/mobile/recovery.sqlite3`.
Submission IDs are recorded before dispatch and reused by the phone until receipt;
repeated IDs can observe existing work but cannot dispatch it again. A message stays
in the phone's protected draft storage until the host confirms acceptance. Foreground
reconnect retries continue at a capped interval through extended VPN outages and stop
when the app backgrounds or the user explicitly disconnects.

Interrupted work offers **Review**. This is a read-only, approval-disabled turn that
inspects the saved checkpoint and actual files. It does not automatically resume the
original mutation. A process can change a file before emitting its tool-start event,
so every unfinished dispatched turn is conservatively treated as having at least one
uncertain action. After review, the user gives a new instruction for further work.
There is no general exactly-once transaction guarantee for arbitrary shell commands;
the enforced contract is durable state, at-most-once submission dispatch, no automatic
tool replay, and read-only recovery review. Explicit Stop intents are durable and
reapplied only to the same still-running turn when reconnecting. Approval IDs are
scoped to the upstream connection to reject stale responses after a restart.

Incremental replay is not advertised. Moving an existing conversation
to a different folder is not supported; start a new chat there instead. Attachments
are rejected by this text-model route rather than silently discarded.

Adapter metadata lives in `~/.local/state/codex-spark/mobile/metadata.json`; native
Codex transcripts remain in `~/.codex-spark`. Logs are in
`journalctl --user -u codex-spark-mobile.service`. For source updates, restart only
this user service, after allowing active work to finish.

## Rollback

Remove the `codex.conf` forwarding drop-in, run `sudo systemctl daemon-reload`, and
restart only `goose-wireguard.service`. It will again forward to the original Mac
reverse-tunnel port 53284. Restore the phone's original host name, Mac directory,
and original Goose secret. A timestamped root-only backup of the forwarding unit
and original phone-profile metadata is kept with the installation audit.

## Acceptance

`smoke_goose_mobile.py` exercises the actual model and sandbox in a temporary test
folder: edits a faulty function, independently checks its tests, reconnects and
recalls context, searches saved messages, rejoins work after disconnecting, and
checks a file was written only once. Unit tests exercise approval rejection and
explicit acceptance, session-scoped replies, user-input forms, duplicate prompt
rejection, exact final-message IDs, and replay/delta ordering. Physical phone
acceptance uses the existing GooseMobileHostUITests against the installed pairing.
`test_mobile_restarts.py` launches an isolated instance of the real Codex binary and
uses Spark for inference. It drops the client transport, restarts the adapter, and
hard-kills/restarts the backend after a command has already appended a marker. Each
marker must remain present exactly once through rejoin, duplicate submissions, and
the recovery review. The physical iPhone checks additionally cover recovery review,
preserved drafts, relaunch, and the AdGuard maintenance project.
