---
name: plugins
description: Select a repository's plugins and generate its rolling-main Claude and Codex settings. Use when adopting plugins for a repository or stack, updating a repository's selected packages, or auditing a machine before removing old user-level plugin settings.

kind: utility
domain: agent
profile: core
applicability: repositories adopting plugins
requires: python, git
provenance: house
route: infer
---

# Plugin selection

Read and follow the [canonical definition](../../.agents/agent/utility/plugins/SKILL.md) in full.
This discovery entry is generated; edit the referenced `.agents/` definition.
