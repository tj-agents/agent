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

Owns one independent native session launch for a prepared prompt in an exact repository or worktree.

Write the complete prompt to a UTF-8 file and select the required native host explicitly. Resolve the
exact repository or worktree; do not substitute another checkout.

```sh
python -B '<package-root>/.agents/agent/utility/handoff/scripts/handoff.py' --host codex -- --working-directory '<absolute-checkout-path>' --prompt-path '<absolute-prompt-path>' --title '<short-title>' --lane L4
```

Use `python` on Windows or `python3` on Linux.

`--title` is required and is the session's one name: the launcher stops the host retitling its tab, so the
stored title and the visible tab match, and resume reuses it. Choose a short, descriptive, unique title.

Use `--host claude` or `--host codex`, then `--`. Every option after the separator is passed unchanged to that host's launcher:
Claude requires a lane, frontier tier, or explicit non-Haiku model; Codex may use its configured default.
The selected launcher verifies native SessionStart before reporting success. Codex handoffs use the same
owned local app-server endpoint as managed local starts. Backend selection is separate from startup
readiness: handoffs additionally require every required hook's acknowledgment and an exact binding from
the frontend's native input to its recipient thread. Missing or conflicting identity prevents startup
confirmation. A handoff requiring verified messaging refuses explicit embedded `--no-daemon` and external
`--remote` before launch.

Keep the reported `status_path`. A verified Codex `running` receipt exposes `client_binding` with its
launch `token`, `recipient` (`thread_id`, `cwd`, `frontend_message_id`) and `profile`. For an authorized
follow-up, use `agent:message` with this status path as `--launch-receipt` and the exact recipient identity.
The message helper uses the owned endpoint recorded by this launch.

Exit code 3 means terminal or startup state is uncertain. An `unconfirmed` result retains the existing
launch; inspect its status receipt and do not relaunch or create a replacement session.

Verified startup and follow-up delivery do not prove recipient acknowledgment, pickup or completion.
