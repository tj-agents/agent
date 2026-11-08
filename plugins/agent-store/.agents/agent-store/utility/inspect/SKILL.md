---
name: inspect
description: Read which agent sessions are linked to which work items, and where each runs, from the agent-store link store. Use when asked which sessions work on an item, what a session is linked to, or to diagnose recorded links.
kind: utility
domain: agent
profile: core
applicability: agent sessions
requires: agent-store
provenance: house
lane: L7
allowed-tools: Bash(agent-store items:*), Bash(agent-store sessions:*), Bash(agent-store show:*), Bash(agent-store launches:*)
---

# Inspect the agent store

```text
agent-store items
agent-store sessions --item github:github.com/tj-agents/agent:98
agent-store sessions --title "agent#98"
agent-store show claude:0f3c2a…
agent-store launches --item 98
```

Each command prints one JSON document. The store holds links only. Whether a session is alive comes from
the host, so use `tj status` for liveness.
