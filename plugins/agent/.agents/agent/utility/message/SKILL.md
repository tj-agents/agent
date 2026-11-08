---
name: message
description: Send a message to another Codex session, addressed by its thread id or tab title. Use when asked to message, tell, correct or follow up with another Codex session.
kind: utility
domain: agent
profile: core
applicability: agent sessions
provenance: house
requires: codex
allowed-tools: Bash(codex queue:*)
---

# Message a Codex session

```text
codex queue --thread "agent#461" --message "CI is green on #102; carry on with the review fixes."
```

`--thread` takes the thread UUID or its exact name, which is the tab title the launcher gave it. Codex delivers the message at the session's next idle point. The command gives no proof of delivery. For a Claude session from Claude, use the native `SendMessage` tool with the session's name.
