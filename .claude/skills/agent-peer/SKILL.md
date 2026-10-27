---
name: peer
description: List and inspect agent peers through the current host, or resolve registered Claude and Codex sessions and their visible terminal tabs. Use when asked which agents or CLIs are running, what a peer is doing, or to inspect or close one exact authorized terminal target.

kind: utility
domain: agent
profile: core
applicability: agent sessions
requires: python, native host
provenance: house
route: infer
---

# Peer CLI sessions

Read and follow the [canonical definition](../../../.agents/agent/utility/peer/SKILL.md) in full.
This discovery entry is generated; edit the referenced `.agents/` definition.

## Claude peers

Discover native `ListAgents` and its available inspection tools, then use their exposed schemas to list
and inspect eligible peers. Preserve native handles and available checkout/profile evidence. Distinguish
children in the current conversation from independent sessions according to the native tool's supported
recipient types.

```text
Request: inspect the API repair peer
Action: select its exact native ListAgents handle and inspect the available native session evidence
```

Use `agent:message` for an authorized follow-up. For cross-host inventory or an exact visible terminal
request, use the canonical registered-session and terminal operations. A native handle does not identify
a physical terminal tab or authorize closing one.
