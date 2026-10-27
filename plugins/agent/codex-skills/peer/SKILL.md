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

Read and follow the [canonical definition](../../.agents/agent/utility/peer/SKILL.md) in full.
This discovery entry is generated; edit the referenced `.agents/` definition.

## Codex peers

Use `codex agents` for native listing and inspection on the current shared app server. Preserve each
thread UUID and its reported checkout/profile evidence. For children in this conversation, use the native
collaboration tools exposed here and their actual schemas.

```text
codex agents
codex agents --remote <verified-app-server-endpoint>
```

The native picker does not support `--no-daemon`; absence there does not prove a private session is dead.
For cross-host inventory or an exact visible terminal request, use the canonical registered-session and
terminal operations. A native thread UUID selects a session; it does not identify a physical terminal tab
or authorize closing one. Send an authorized follow-up through `agent:message`.
