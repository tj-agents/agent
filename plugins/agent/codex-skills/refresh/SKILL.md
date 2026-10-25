---
name: refresh
description: Bring this CLI's tj-agents plugins up to date and into the running session. Run it yourself, without asking, whenever plugins look outdated - a skill or hook is missing or behaves like an older version, or a plugin PR just merged - or when asked to sync, reload, refresh or update the plugins.
kind: utility
domain: agent
profile: core
applicability: native host sessions
requires: python, native host
provenance: house
route: infer
---

# Sync plugins

Read and follow the [canonical definition](../../.agents/agent/utility/refresh/SKILL.md) in full.
This discovery entry is generated; edit the referenced `.agents/` definition.

Codex refresh requests native runtime reload for the running session. A failed reload leaves the session unready; report that result rather than treating a separate process as proof.
