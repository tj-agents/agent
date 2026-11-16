---
name: message
description: Send another Claude or Codex session a message with tj message, or read or wait for your own messages with tj feed on either host. Use when you must tell another session your final result, a blocker you cannot resolve or an approval Tommy must give, when asked to message, tell or correct another session, or when a parent waits for a child.
kind: utility
domain: agent
profile: core
applicability: agent sessions
provenance: house
requires: tj
allowed-tools: Bash(tj message:*), Bash(tj feed:*)
---

# Message a session

Owns messaging between Claude and Codex sessions through `tj message` and `tj feed`. Messages append to
the target's host/session feed with sender identity preserved. Both hosts read their feed with `tj feed`.

```text
tj message "agent#461" "<text>"                                   by tab title
tj message codex:019a2f0c-0000-7000-8000-000000000461 "<text>"   by handle
tj feed                                    either host: show unread messages once
tj feed --wait --timeout 7200              either host: block up to two hours for the next message

Child: PR 102 is open and green           -> send: final result
Child: release is red, outside its scope  -> send: blocker it cannot resolve
Child: pushed a fix, CI is running        -> do not send: progress
```

The target is the session's tab title or its `host:id` handle, and must match exactly one live session; an
ambiguous title lists the handles to choose from. Exit 0 `posted to …'s feed` confirms storage, not recipient
wake or rendering; exit 2 means nothing was sent, so report the error and never claim it arrived. No automatic
idle prompts are sent. `workflow:session` owns when a message is worth sending.

## Waiting for a child

A Claude or Codex parent runs `tj feed --wait --timeout <seconds>` as a background command through
`agent:listen` and keeps working. The requested wait returns through that host's native background command
completion. It reads and marks unread messages from any session, so check the sender and wait again if the
child has not replied. Exit 1 means it timed out with no message; check the child before assuming it failed.
