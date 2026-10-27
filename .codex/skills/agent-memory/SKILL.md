---
name: memory
description: Inspect or change the current host's native memory controls. Use when asked about memory state, enabling or disabling memory, or opening memory settings for Claude or Codex.
kind: utility
domain: agent
profile: core
applicability: agent sessions
requires: native host
provenance: house
---

# Agent memory

Read and follow the [canonical definition](../../../.agents/agent/utility/memory/SKILL.md) in full.
This discovery entry is generated; edit the referenced `.agents/` definition.

## Codex memory

Use the native Codex executable in the selected `CODEX_HOME` and inspect the `memories` feature:

```text
codex features list
codex features enable memories
codex features disable memories
```

Run only the operation matching the request, then read the native feature state again. `features enable`
and `features disable` persist in that Codex user's `config.toml`; this is a user-configuration change,
not a project-local toggle. If the request permits only a project-local change, report that boundary and
leave the broader setting unchanged. Never assume the feature exists or is enabled on this version, and
never write `.codex/settings.local.json` or `autoMemoryEnabled` as a Codex memory control.

Use [Codex's configuration reference](https://developers.openai.com/codex/config-reference/) for the
current `features.memories` capability and native memory scope.
