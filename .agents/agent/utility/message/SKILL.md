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

Owns messaging between sessions on either host. A message goes to the recipient's feed and never enters its
conversation; the recipient reads it when it chooses, so a message costs the recipient context only when it
is worth reading.

```text
tj message "agent#461" "<text>"                     post to the session titled agent#461
tj message codex:019a2f0c-0000-7000-8000-000000000461 "<text>"   post by handle
tj feed                                             show your unread messages once
tj feed --wait --timeout 7200                       block up to two hours for the next message

Child: PR 102 is open and green           -> send: final result
Child: release is red, outside its scope  -> send: blocker it cannot resolve
Child: pushed a fix, CI is running        -> do not send: progress
```

The target is the session's tab title or its `host:id` handle, and must match exactly one live session; an
ambiguous title lists the handles to choose from. Exit 0 `posted to …'s feed` means it is stored; exit 2 means
nothing was posted, so report the error and never claim it arrived.

## When to send

Send only what changes the recipient's next decision: your final result, a blocker you cannot resolve, or an
approval Tommy must give. Every message is read into the recipient's context, so progress reports, "still
working" notes and "are you done?" questions only spend it. Never use the native `SendMessage` tool for another
session; it injects the text into that conversation.

## Reading and waiting

`tj feed` shows your unread messages once, each with its sender and the `tj message` command to reply. Read it
when you start work that another session may have answered, and before reporting that you are waiting.

A parent that needs a child's result runs `tj feed --wait --timeout <seconds>` as a background command through
`agent:listen`, then keeps working; the child's final message returns as that task's completion. A timeout
means no message arrived, so check the child before assuming it failed.
