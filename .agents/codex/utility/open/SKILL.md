---
name: open
description: Reopen one exact native Codex session in its recorded directory with its native TUI.
kind: utility
domain: agent
profile: core
applicability: codex sessions
requires: python, codex
provenance: house
route: infer
---

# Open a Codex session

This is the Codex host adapter behind `agent:open`. Use `agent:open` when the request
names a session, topic or directory; use this skill only to reopen an exact id you already verified.

Search first, then reopen only the chosen exact native id. Verify its recorded cwd and profile before opening;
use `agent:search` against the selected profile when that evidence needs reconciliation. The launcher
verifies native resume support, preserves the recorded cwd and uses the native executable directly after the
tab applies plugin updates published since the last check.
Run the helper with `python` on Windows or `python3` on Linux/macOS:

```sh
python -B '<package-root>/.agents/agent/runtime/agent_recovery.py' open --host codex --id '<native-id>' --codex-history-root '<selected-profile>/sessions' --prompt 'Continue the requested work.'
```

For task recovery, use the packaged Agent task-recovery context. Write its
private UTF-8 instruction file outside version control, then replace the default `--prompt` with
`--prompt-path '<recovery-prompt-file>'`. Do not pass both arguments.

```sh
python -B '<package-root>/.agents/agent/runtime/agent_recovery.py' open --host codex --id '<native-id>' --codex-history-root '<selected-profile>/sessions' --prompt-path '<recovery-prompt-file>'
```

If status is unknown, reconcile it first. `--confirm-closed` is an explicit acknowledgement that no live writer
exists, not evidence that the source owner released its work. A verified live session cannot be resumed. A launched
tab neither delivers a follow-up nor restores transport. Never retry after an ambiguous terminal result.
