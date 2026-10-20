# Codex Spark

Codex Spark is a personal fork of [OpenAI Codex](https://github.com/openai/codex)
for a Linux development host connected to a separate DGX Spark model server.
The agent runs commands and edits files on the development host; inference runs
on Spark. Goose mobile connects to the same persistent backend.

Current release: **0.1.0**.

See the [Codex Spark setup and maintenance guide](contrib/spark/README.md)
for building the fork, installing the isolated profile, and updating an existing host.
The [release notes](contrib/spark/CHANGELOG.md) describe fork changes.

```sh
codex-spark                    # Start in your current directory
codex-spark --version
codex-spark doctor             # Check the backend
codex-spark doctor --live      # Also test inference, a tool, and reconnect
```

The [Goose mobile guide](contrib/spark/GOOSE_MOBILE.md) covers the private phone
connection, recovery, workspace browsing, work review, and notifications.

The fork retains upstream protocol and configuration identifiers for compatibility.
Original project documentation is preserved in [README.upstream.md](README.upstream.md).
The upstream [Apache-2.0 license](LICENSE) and [notices](NOTICE) are retained.
