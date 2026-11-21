---
name: handoff
description: Launch an independent Claude or Codex goal in a named tab from a prepared prompt and exact checkout. Use when starting a separate session; messaging an existing session uses agent:message.
kind: utility
domain: agent
profile: core
applicability: agent sessions
requires: python, tj, native host
provenance: house
route: infer
---

# Handoff an agent session

Owns launching an independent Claude or Codex session from a prepared prompt.

```text
tj handoff --host codex -- --working-directory "<absolute-checkout>" --prompt-path "<absolute-prompt-file>" --title "agent#461 review" --lane L4 --item 461
```

Write the complete prompt to a UTF-8 file before launching.
Choose `--lane L1`–`L6` using `agent:lanes`; the launcher resolves both model and thinking effort.
For L1, use `--host codex` because Claude L1 routes there.

- `--title` is required, short and unique: it becomes the tab title and the session's native name, so other sessions address the child by it.
- A handoff opens through `tj claude` or `tj codex` with its own session identity. Supply `--parent <host:id>` only when that session should receive the managed final response and necessary blocker or approval requests. [Message a session](../message/SKILL.md) describes delivery.
- Supply `--item` to link an existing work item. Bare IDs use the target project's tracker profile. Omitting it starts an unlinked session.

The last output line is a JSON receipt containing the launch ID, title, checkout and prompt path. A parented handoff also records the explicit parent. Exit 3 means the tab may have opened. Check its terminal before retrying.
