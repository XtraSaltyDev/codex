# Codex Spark releases

## Unreleased

- Expose native model, reasoning, and access choices to Goose, with bounded project browsing, staged/unstaged diffs, and Markdown/raw previews.
- Forward mobile plan checklists with durable per-turn replay, and show compact tool descriptors with original details available on tap in Goose.
- Classify explicit HTTP context-limit codes and vLLM input-token overflow errors as context exhaustion, preserving Codex's existing accounting and local-compaction recovery.
- Default new Spark chats to `low` reasoning while preserving explicit per-chat choices.
- Advertise live-verified Spark reasoning choices (`none`, `low`, `medium`, `xhigh`) while preserving the default; verify native selection and actual reasoning usage, and exclude rejected or inconclusive levels.
- Enable `update_plan` and align Spark patch instructions with the existing `exec_command` patch helper; verify planning and real patch edits against Spark.
- Detect served input modalities from provider metadata or a verified Responses image probe; preserve the catalog when detection is inconclusive.
- Enable verified native image support and mobile image prompts, durable receipts, and image replay across reconnects and adapter restarts.
- Report truncated vLLM responses as incomplete even when their terminal event is named `response.completed`.
- Automatically name phone-created chats using the configured Spark model after a successful response.
- Share saved names with the terminal, preserve manual renames, and refresh mobile titles when loading a chat.
- Keep background naming turns out of conversation history and task notifications.

## 0.1.0

- Identify the CLI and terminal interface as Codex Spark.
- Use `codex-spark` in help, shell completions, and resume commands.
- Report a shared release version for the CLI, TUI, and app-server.
- Match the mobile handshake, health diagnostics, and notifications to the fork identity.
- Keep updates on the personal fork instead of promoting upstream installers.
- Store launcher backups outside the executable search path.
- Preserve existing Spark configuration, credentials, chat history, and mobile protocol.
