---
name: recover
description: Recover dead Claude and Codex sessions with tj recover after a crash, reboot or closed terminal. Use when asked to recover or restore agents, sessions or tabs, or preview which dead sessions would return.
kind: utility
domain: agent
profile: core
applicability: agent sessions
provenance: house
requires: tj
allowed-tools: Bash(tj status:*), Bash(tj recover:*)
---

# Recover agents

Owns dead Claude and Codex session recovery through `tj recover`.

```text
tj status
tj recover --dry-run
tj recover
tj recover --older
tj recover --ended
tj recover --session codex:019a2f72-60d1-7be2-9ef1-8af0ce672013
tj recover 124
```

By default, recover every dead, non-ended session whose transcript last changed after the previous boot,
whether or not it links to a work item. Older dead sessions are automatically marked ended. Live sessions
are left alone.

Use `--older` to include older dead sessions and `--ended` to include ended sessions. Narrow selection with
`--session <host:session-id>` or a positional work item. `--dry-run` reports would-resume, would-respawn,
would-end and would-fail actions without changing sessions, launch records or terminals.

Claude background sessions respawn in place. Other selected sessions resume through `tj run` in new
Windows Terminal or tmux tabs in their recorded worktrees. Preserve each session's title, item link,
parent and launch row.
