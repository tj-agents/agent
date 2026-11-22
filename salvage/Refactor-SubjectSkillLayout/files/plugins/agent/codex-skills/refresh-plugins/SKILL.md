---
name: refresh-plugins
description: Bring this CLI's tj-agents plugins up to date and into the running session. Run it yourself, without asking, whenever plugins look outdated - a skill or hook is missing or behaves like an older version, or a plugin PR just merged - or when asked to sync, reload, refresh or update the plugins.
metadata:
  tj-agents-kind: operation
  tj-agents-domain: agent
  tj-agents-profile: core
  tj-agents-applicability: native host sessions
  tj-agents-requires: python, native host
  tj-agents-provenance: house
  tj-agents-route: infer
---

# Refresh plugins

Read and follow the [canonical definition](../../.agents/agent/skills/refresh-plugins/SKILL.md) in full.
This discovery entry is generated; edit the referenced `.agents/` definition.

Codex refresh first requests native runtime reload for the running session. If verified hooks need a fresh
client, it closes only the attributable frontend after the current turn and resumes this exact conversation
automatically with its original profile and checkout. A failed trust or recovery check leaves the session
unready; another process is not proof of adoption.
