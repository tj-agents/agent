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
python -B "<package-root>/.agents/agent/utility/handoff/scripts/handoff.py" --host codex -- --working-directory "<absolute-checkout>" --prompt-path "<absolute-prompt-file>" --title "agent#461 review" --lane L4 --item 461
```

Write the complete prompt to a UTF-8 file, then run with `python` on Windows or `python3` on Linux.
Choose `--lane L1`–`L6` using `agent:lanes`; the launcher resolves both model and thinking effort.
For L1, use `--host codex` because Claude L1 routes there.

- `--title` is required, short and unique: it becomes the tab title and the session's native name, so other sessions address the child by it.
- `--parent` defaults to the calling session; pass `--parent <host:id>` to override it. The child's prompt gains a standing rule to report to that parent under [agent:message](../message/SKILL.md), which owns transport selection, receipts and any required parent wait. Preserve explicit task standing rules and user transport instructions in the prepared prompt.
- `--item` links the child to an existing work-item URL or reference, so it appears in `tj status` and `tj recover --item`. Bare IDs use the target project's tracker profile. Omit it when the work has no item: the child launches unlinked, the launcher never reads or writes a tracker, and `tj run` closes out without a tracker step.

Handoff never creates items. When the work needs a new item, follow the project's creation workflow and approval policy first, then pass the resulting reference.

Success prints the launch as JSON once `tj run` has recorded it. Exit 3 means the tab may have opened without being confirmed: check the tab before launching again.
