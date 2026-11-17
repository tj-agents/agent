---
name: handoff
description: Launch a parented or independent Claude or Codex goal in a named tab from a prepared prompt and exact checkout. Use when starting a separate session; messaging an existing session uses agent:message.
kind: utility
domain: agent
profile: core
applicability: agent sessions
requires: python, tj, native host
provenance: house
route: infer
---

# Handoff an agent session

Owns launching a Claude or Codex session from a prepared prompt with an explicit relationship to other sessions.

```text
python -B "<package-root>/.agents/agent/utility/handoff/scripts/handoff.py" --host codex -- --working-directory "<absolute-checkout>" --prompt-path "<absolute-prompt-file>" --title "agent#461 review" --lane L4 --item 461 --parent "<host:id>"
```

Write the complete prompt to a UTF-8 file, then run with `python` on Windows or `python3` on Linux.
Choose `--lane L1`–`L6` using `agent:lanes`; the launcher resolves both model and thinking effort.
For L1, use `--host codex` because Claude L1 routes there.

- `--title` is required, short and unique: it becomes the tab title and the session's native name, so other sessions address the child by it.
- Choose exactly one relationship: `--parent <host:id>` or `--independent`. Both hosts support either parent host. Parent identity is never inferred from the calling environment.
- A parented prompt gains a standing rule to report to that exact parent under [agent:message](../message/SKILL.md), which owns transport selection, receipts and any required parent wait. Preserve explicit task standing rules and user transport instructions in the prepared prompt.
- An independent session has no parent or inherited reporting obligation. Prepare its complete goal directly for the user; omit parent history and reporting instructions. Independence separates conversation and ownership, not operating-system access.
- `--item` links the session to an explicitly chosen work item, so it appears in `tj status` and `tj recover`. Without it, `tj item create` makes one titled after `--title`: under the named parent's item for a parented launch, or a root item for an independent launch. Pass profile-required fields with `--item-field NAME=VALUE`.

Success prints the launch as JSON once `tj run` has recorded it. Exit 3 means the tab may have opened without being confirmed: check the tab before launching again.
