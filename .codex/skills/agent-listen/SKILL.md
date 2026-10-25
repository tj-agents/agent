---
name: listen
description: Run one deterministic command in the background and return its result through the current host completion notification. Use when waiting for a build, test, CI monitor or other deferred result while continuing work or parking the turn.
kind: utility
domain: agent
profile: core
applicability: agent sessions
requires: native host
provenance: house
---

# Background listener

Read and follow the [canonical definition](../../../.agents/agent/utility/listen/SKILL.md) in full.
This discovery entry is generated; edit the referenced `.agents/` definition.

Codex uses its native background execution and completion notification for this listener. Retain the returned task identifier and consume that exact task's result once.
