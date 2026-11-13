---
name: message
description: Send another Claude or Codex session a message with tj message, or, in a Codex session, read or wait for your own messages with tj feed. Use when you must tell another session your final result, a blocker you cannot resolve or an approval Tommy must give, when asked to message, tell or correct another session, or when a Codex parent waits for a child.
kind: utility
domain: agent
profile: core
applicability: agent sessions
provenance: house
requires: tj
allowed-tools: Bash(tj message:*), Bash(tj feed:*)
---

# Message a session

Read and follow the [canonical definition](../../.agents/agent/utility/message/SKILL.md) in full.
This discovery entry is generated; edit the referenced `.agents/` definition.
