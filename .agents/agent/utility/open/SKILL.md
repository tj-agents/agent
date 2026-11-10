---
name: open
description: Reopen one Claude or Codex session by its session id in a new tab in its recorded worktree. Use when asked to open, reopen, resume or bring back a specific session or conversation.
kind: utility
domain: agent
profile: core
applicability: agent sessions
provenance: house
requires: tj
allowed-tools: Bash(tj recover:*), Bash(tj status:*)
---

# Open a session

Owns reopening one recorded Claude or Codex session.

```text
tj recover --session codex:019a2f72-60d1-7be2-9ef1-8af0ce672013 --older --ended
```

Find the `host:session-id` with `tj status`, or for older conversations with `agent:search`.
`--older` includes history before the previous boot; `--ended` includes deliberately ended sessions.
A live session is left alone, so a second writer never starts.
