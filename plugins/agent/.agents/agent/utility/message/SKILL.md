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

Owns messaging between sessions through one command on either host. `tj message` delivers natively into a
Claude session's inbox. Codex has no native messaging, so a Codex session gets the message in its feed and
reads it with `tj feed`.

```text
tj message "agent#461" "<text>"                                   by tab title
tj message codex:019a2f0c-0000-7000-8000-000000000461 "<text>"   by handle
tj feed                                    in Codex: show unread messages once
tj feed --wait --timeout 7200              in Codex: block up to two hours for the next message

Child: PR 102 is open and green           -> send: final result
Child: release is red, outside its scope  -> send: blocker it cannot resolve
Child: pushed a fix, CI is running        -> do not send: progress
```

The target is the session's tab title or its `host:id` handle, and must match exactly one live session; an
ambiguous title lists the handles to choose from. Exit 0 `delivered to …'s inbox` (Claude) or
`posted to …'s feed` (Codex) means it was sent; exit 2 means nothing was sent, so report the error and never
claim it arrived. `workflow:session` owns when a message is worth sending.

## Waiting for a child

A Claude parent needs nothing: the child's message starts a turn in its inbox. A Codex parent runs
`tj feed --wait --timeout <seconds>` as a background command through `agent:listen` and keeps working. The
wait returns with the first unread message from any session and marks it read, so check the sender and wait
again if it is someone else. Exit 1 means it timed out with no message; check the child before assuming it
failed.
