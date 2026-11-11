---
name: handoff
description: Use when launching an independent Claude or Codex goal in a named tab from a prepared prompt and exact checkout.
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

- `--title` is required, short and unique: it becomes the tab title and the session's native name, so other sessions address the child by it.
- `--parent` defaults to the calling session; pass `--parent <host:id>` to override it. The child's prompt then gains a standing rule to message the parent when it finishes, when it is blocked, and when Tommy's approval is needed.
- `--item` links the child to a work item, so it appears in `tj status` and `tj recover`. Without it, `tj item create` makes one titled after `--title`, under the parent's item; pass any fields the profile requires as `--item-field NAME=VALUE` (in Infonetica, `--item-field attribution=<value>`).
- Claude needs `--lane L1`-`L6`, `--frontier` or an explicit non-Haiku `--model`. Codex takes `--lane`, `--model` or `--reasoning-effort`, or its configured default.

Success prints the launch as JSON once `tj run` has recorded it. Exit 3 means the tab may have opened without being confirmed: check the tab before launching again.
