---
name: open
description: Reopen one Claude or Codex session by its session id in a new tab in its recorded worktree. Use when asked to open, reopen, resume or bring back a specific session or conversation.
kind: utility
domain: agent
profile: core
applicability: agent sessions
provenance: house
requires: tj
allowed-tools: Bash(tj reload:*), Bash(tj status:*)
---

# Open a session

Find the `host:session-id` with `tj status`, or for older conversations with `agent:search`. Then reopen it:

```text
tj reload --session codex:019a2f…
```

A live session is left alone, so a second writer never starts.
