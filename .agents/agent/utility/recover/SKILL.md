---
name: recover
description: Bring back the agents of open work items after a crash, reboot or closed terminal. Use when asked to recover, restore, reload or bring back agents, sessions or tabs, or to see which agents are dead.
kind: utility
domain: agent
profile: core
applicability: agent sessions
provenance: house
requires: tj
allowed-tools: Bash(tj status:*), Bash(tj reload:*)
---

# Recover agents

Run `tj status` to see every open item's agents as live, idle, dead or ended, then `tj reload` for every open
item or `tj reload <item>` for one. `tj reload --dry-run` lists what it would restore.

```text
tj status
tj reload 461
```

Reload respawns a dead Claude background session in place and resumes any other dead agent in a new tab in
its recorded worktree. It leaves live agents alone. Sessions that ended cleanly stay ended unless you pass `--ended`.
