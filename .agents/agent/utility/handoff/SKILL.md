---
name: handoff
description: Start an independent Claude or Codex session in its own tab for a prepared prompt, in an exact repository or worktree, named by its tab title and reporting back to its parent. Use when handing an independent goal to another session.
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

## Interactive handoffs

When the user needs to continue the conversation, open the native Claude or Codex CLI in the named
terminal tab through this launcher. A Codex `create_thread` result starts background work; it does not
establish that the user has a visible tab. Use that capability for requested background work, not as a
silent replacement for an interactive handoff. If the launcher is unavailable, report its concrete
failure and preserve the existing owner while resolving it.

For Codex, the launcher removes inherited `NO_COLOR` and clears the parent's `TERM` on Windows; on POSIX it
preserves the terminal's type. Keep this environment handling when using a supported recovery path.
Do not reconstruct a shell launch that inherits the tool runner's monochrome environment, force a
custom theme, or disable native security controls to make a handoff open.

If a background session already exists, checkpoint its work and use the host's supported operation to
release its active attachment before reopening that same session. An "already open" or active-writer
error means attachment recovery is incomplete. Do not start a second implementation owner or delete
session locks. `workflow:handoff` continues to own responsibility transfer and pickup evidence.

```text
Background task acknowledged; no visible conversation -> interactive handoff still incomplete.
Resume reports another active writer -> release the existing attachment, then reopen the same session.
Native conversation is visible and accepts input -> report the verified tab and how to reach it.
```

Verify the native conversation, not just a launcher exit code, task acknowledgement or running process.
Use the host's supported observation, or ask the user to confirm when the terminal contents are not
exposed. Keep submission, pickup and interactive readiness distinct in the result; never describe an
unobserved terminal as visible or its colours as verified.
