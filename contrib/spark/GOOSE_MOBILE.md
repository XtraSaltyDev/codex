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

Supports streaming text, command/file activity, explicit allow-once/decline approvals,
user-input forms, stop, history replay, saved-message search, rename, archive/restore,
delete, project folders, and fresh projectless workspaces. Mobile-created Codex threads
use legacy history mode for full ACP transcript replay. Model execution remains on
the existing Spark provider. This adapter does not advertise a model picker; change
the backend model through the isolated Codex Spark configuration.

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
