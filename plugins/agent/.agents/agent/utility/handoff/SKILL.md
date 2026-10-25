---
name: handoff
description: Start one independent Claude or Codex session for a prepared prompt in an exact repository or worktree. Use when handing bounded work to a specified native host.

kind: utility
domain: agent
profile: core
applicability: agent sessions
requires: python, native host
provenance: house
route: infer
---

# Handoff an agent session

Write the complete prompt to a UTF-8 file and select the required native host explicitly. Resolve the
exact repository or worktree; do not substitute another checkout.

```sh
python -B '<package-root>/.agents/agent/utility/handoff/scripts/handoff.py' --host claude -- --working-directory '<absolute-checkout-path>' --prompt-path '<absolute-prompt-path>' --title '<short-title>' --lane L4
```

Use `--host claude` or `--host codex`, then `--`. Every option after the separator is passed unchanged to that host's launcher:
Claude requires a lane, frontier tier, or explicit non-Haiku model; Codex may use its configured default.
The selected launcher verifies native SessionStart before reporting success. Exit code 3 means terminal or
startup state is uncertain; inspect its reported status receipt and do not retry automatically.

This only submits the handoff. The receiving session's pickup and completion remain separate evidence.
