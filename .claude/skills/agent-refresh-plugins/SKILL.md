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

Read and follow the [canonical definition](../../../.agents/agent/skills/refresh-plugins/SKILL.md) in full.
This discovery entry is generated; edit the referenced `.agents/` definition.

Claude refresh preserves the same verified session when an installed update requires a fresh client. Agent
arms only its attributable frontend close, waits for the immutable close request and matching closed result,
then resumes the exact session with its original profile, checkout and safe native options. A refusal,
cancellation, stale generation or failed verification never starts another client; report incomplete adoption.
