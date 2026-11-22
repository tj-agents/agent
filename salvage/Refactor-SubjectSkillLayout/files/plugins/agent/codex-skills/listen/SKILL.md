---
name: listen
description: Run one deterministic command in the background and return its result through the current host completion notification. Use when waiting for a build, test, CI monitor or other deferred result while continuing work or parking the turn.
metadata:
  tj-agents-kind: operation
  tj-agents-domain: agent
  tj-agents-profile: core
  tj-agents-applicability: agent sessions
  tj-agents-requires: native host
  tj-agents-provenance: house
---

# Background listener

Read and follow the [canonical definition](../../.agents/agent/skills/listen/SKILL.md) in full.
This discovery entry is generated; edit the referenced `.agents/` definition.

Codex uses its native background execution and completion notification for this listener. Retain the returned task identifier and consume that exact task's result once.
