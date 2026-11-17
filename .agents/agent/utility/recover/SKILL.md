---
name: recover
description: Use when restoring dead Claude or Codex sessions after a crash, reboot or closed terminal, or previewing recovery.
kind: utility
domain: agent
profile: core
applicability: agent sessions
provenance: house
requires: tj
allowed-tools: Bash(tj status:*), Bash(tj recover:*), Bash(tj receipt:*)
---

# Recover agents

Owns dead Claude and Codex session recovery through `tj recover`.

```text
tj status
tj recover --dry-run
tj recover
tj recover --open
tj recover --older
tj recover --ended
tj recover --session codex:019a2f72-60d1-7be2-9ef1-8af0ce672013
tj recover 124
tj receipt --launch <launch-id>
```

By default, recover every dead, non-ended session whose transcript last changed after the previous boot,
whether or not it links to a work item. Older dead sessions are automatically marked ended. Live sessions
are left alone. `--open` instead reopens exactly the launches whose tab was open, newest per session,
whose agent is not running; it never ends sessions and ignores the previous boot.

Use `--older` to include older dead sessions and `--ended` to include ended sessions. Narrow selection with
`--session <host:session-id>` or a positional work item. `--dry-run` reports would-resume, would-respawn,
would-end and would-fail actions without changing sessions, launch records or terminals.

Claude background sessions respawn in place. Other selected sessions resume in new Windows Terminal or
tmux tabs in their recorded worktrees, using the managed lifecycle behind `tj claude` and `tj codex`.
Recovery records a new launch and retains the previous launch as history. Preserve the native conversation,
title, optional item and recorded parent; an independent session stays independent. Do not infer a parent
from the caller or replace recovery with a fresh handoff.

Use `tj status` to distinguish native liveness from completion state, and `tj receipt --launch <launch-id>`
for the durable result and closeout evidence. Recovery does not request completion or release a keep-open
hold. When the resumed work finishes, the agent uses `tj finish` and writes its native final response;
missing receipts and unsafe worktree cleanup remain incomplete. Follow the
[managed-session contract](https://github.com/tj-agents/cli#managed-sessions) for finish, waits and holds.
