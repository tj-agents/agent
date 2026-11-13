---
name: message
description: Post a message to another Claude or Codex session's feed, read your own feed, or wait for a child session's result. Use when you must tell another session your final result, a blocker you cannot resolve or an approval Tommy must give, when asked to message, tell or correct another session, or when waiting to hear from a child session.
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
