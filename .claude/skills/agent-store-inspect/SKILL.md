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

Read and follow the [canonical definition](../../../.agents/agent-store/utility/inspect/SKILL.md) in full.
This discovery entry is generated; edit the referenced `.agents/` definition.
