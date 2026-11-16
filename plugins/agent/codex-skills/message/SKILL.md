---
name: message
description: Message another session with native Claude messaging or tj message/feed when Codex is involved. Use when reporting a final result, an unresolvable blocker or required user approval, when asked to message or correct a session or read its feed, or when a parent waits for a child.
kind: utility
domain: agent
profile: core
applicability: agent sessions
provenance: house
requires: native host, tj for feed transport
allowed-tools: ListAgents, SendMessage, Bash(tj message:*), Bash(tj feed:*)
---

# Message a session

Read and follow the [canonical definition](../../.agents/agent/utility/message/SKILL.md) in full.
This discovery entry is generated; edit the referenced `.agents/` definition.
