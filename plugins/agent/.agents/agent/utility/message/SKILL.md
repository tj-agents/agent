---
name: message
description: Send a requested session message through tj message on Claude or Codex, read the session feed, or wait for an expected reply.
kind: utility
domain: agent
profile: core
applicability: agent sessions
provenance: house
requires: tj
allowed-tools: Bash(tj message:*), Bash(tj feed:*)
---

# Message a session

Owns sending a message to a specific session and interpreting the transport receipt.

```text
tj message --request-id review-blocker-1 codex:019a2f0c-0000-7000-8000-000000000461 "<text>"
tj feed
tj feed --wait --timeout 7200
```

Run `tj message` from the actual sender. Address the recipient by exact handle or unique live tab title. Reuse a request ID only for the same sender, target and content. Follow the [session reporting rules](https://github.com/tj-agents/core/blob/main/.agents/workflow/policy/session/SKILL.md). Managed completion sends a parented session's final response once.

For Claude to Claude, the CLI uses the sender's native adapter. Report its submitted, undelivered or unknown result. A missing adapter, refused delivery or ambiguous target needs resolution before another send.

For the other host pairs, a successful post confirms storage in the recipient's feed. A read acknowledgment requires evidence from the recipient. Run `tj feed` to consume unread messages once. For an expected feed reply, use one bounded wait through [listen](../listen/SKILL.md) and check the sender's handle before acting. A timeout means the wait expired.
