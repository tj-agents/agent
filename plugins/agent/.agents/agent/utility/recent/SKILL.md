---
name: recent
description: List recent Claude or Codex conversations with their exact resume commands. Use when asked which conversation to resume, what the last session covered, or which conversation owns a worktree.
kind: utility
domain: agent
profile: core
applicability: agent sessions
provenance: house
requires: python
---

# Recent conversations

Run with `python` on Windows or `python3` on Linux, for the current host unless another is named:

```text
python -B "<package-root>/.agents/agent/runtime/history.py" recent --host codex --count 5
```

Options: `--cwd <dir>`, `--project <substring>`, `--all-projects`, `--worktree <dir>` (conversations that worked in that checkout, from any directory), `--history-root <dir>`. Report the session and its returned `resume` command. The read is read-only.
