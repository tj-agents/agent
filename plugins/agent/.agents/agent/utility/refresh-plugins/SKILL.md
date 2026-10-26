---
name: refresh-plugins
description: Bring this CLI's tj-agents plugins up to date and into the running session. Run it yourself, without asking, whenever plugins look outdated - a skill or hook is missing or behaves like an older version, or a plugin PR just merged - or when asked to sync, reload, refresh or update the plugins.
kind: utility
domain: agent
profile: core
applicability: native host sessions
requires: python, native host
provenance: house
route: infer
---

# Refresh plugins

Every `claude`, `codex`, handoff and open launch already applies plugin updates published since the last
check. This runs the same refresh for a running session, including installation and native readback of newly declared plugins. Run it on your own judgement; never ask first. On Windows:

```powershell
python -B '<skill-directory>/scripts/refresh_plugins.py' --project '<absolute-checkout>'
```

On Linux/macOS use `python3`. It updates only installs whose plugin files differ from the published head, on both hosts. If it
reports `current` but a plugin is still stale, rerun with `--all` (optionally `--host claude|codex`).
`--verify` also checks native installations against remote main; `--verify-only` changes nothing.

It fails loudly on a native update that did not persist and a plugin the host lists with errors; report
those.

Treat missing, failed, timed-out or untrusted required hooks as a stop on ordinary work. A refresh must
verify installed publisher bytes, trust only the verified current hook hashes through Codex's native
configuration API, and read back hook readiness before reporting success. Never bypass hook trust or
disable a required hook to complete a refresh. Run the affected hooks and verify their successful
execution before resuming; hash trust establishes permission to run, not execution. If recovery cannot
be verified, retain the current owner and report the failed hook and exact recovery action.

The host entry point names how this session adopts the result.

Keep the same conversation through recovery. If a verified Claude or Codex session needs a fresh client,
Agent arms its exact attributable frontend close and resumes that same session with its original profile,
directory and safe native options. The watcher requires the immutable close request digest and its matching
closed result before resuming. Checkpoint before the turn ends; the resumed client must verify required hook
execution before ordinary work. `--no-relaunch` reports incomplete adoption and never arms a close. Trust
failures remain failures; refresh never bypasses trust or creates another conversation.
