---
name: search
description: Search local Claude or Codex conversation text. Use when the user asks where a past conversation discussed a subject or worked in a checkout.
kind: utility
domain: agent
profile: core
applicability: agent sessions
provenance: house
requires: python
---

# Search conversation history

Run with `python` on Windows or `python3` on Linux, for the current host unless another is named:

```text
python -B "<package-root>/.agents/agent/runtime/history.py" search --host claude --pattern "agent-store"
```

The search is literal and case-insensitive over message text. `--regex`, `--show-lines`, `--project`, `--cwd`, `--worktree <dir>`, `--count` and `--history-root` narrow it. Summarise hits and their resume commands rather than dumping transcripts.
