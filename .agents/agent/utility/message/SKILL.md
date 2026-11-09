---
name: message
description: Send a message to another Claude or Codex session addressed by its tab title, or get one notice when a Codex child next goes idle or exits. Use when asked to message, tell, update, correct or follow up with another session, when reporting back to the session that started you, or when waiting to hear that a child session finished.
kind: utility
domain: agent
profile: core
applicability: agent sessions
provenance: house
requires: tj
allowed-tools: Bash(tj message:*), Bash(tj subscribe:*)
---

# Message a session

Owns messaging between sessions from either host. `tj message` dispatches on the target's host; `tj subscribe`
asks for one idle-or-exit notice from a Codex child.

```text
tj message "agent#461" "CI is green on #102; carry on with the review fixes."
tj message codex:019a2f0c-0000-7000-8000-000000000461 "Rebase on main before the next push."
tj subscribe "agent#461"
```

The target is the session's tab title or its `host:id` handle, and must match exactly one live session; an
ambiguous title lists the handles to choose from. Act on the result:

- Exit 0 `queued for …`: a Codex target receives it at its next idle point. There is no delivery proof.
- Exit 0 `delivered to …'s inbox`: a Claude target starts a turn if idle, or reads it between tool calls.
- Exit 3 `not sent`: you and the target are both Claude. Send it with the native `SendMessage` tool to the
  printed name.
- Exit 2: nothing was delivered. Report the error; never claim the message arrived.

To hear when a child finishes, subscribe once and keep working; never poll or send "are you done?":

| Parent | Child | Subscribe with |
| --- | --- | --- |
| Claude | Claude | `SendMessage` with `notify_when_idle: true` |
| Claude or Codex | Codex | `tj subscribe <child>`; refused if the child is not loaded in the shared app-server |
| Codex | Claude | none; the child reports to you with `tj message` |
