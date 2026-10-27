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

Read and follow the [canonical definition](../../.agents/agent/utility/memory/SKILL.md) in full.
This discovery entry is generated; edit the referenced `.agents/` definition.

## Claude memory

Use Claude Code's native `/memory` interface to inspect loaded instruction and memory files and its
auto-memory control. Follow the native UI for the requested state and scope; when interaction belongs to
the user, identify that exact native operation instead of editing a setting through another host.

```text
/memory
```

Claude's auto-memory control and instruction files have different scopes. Report which native control
was inspected or changed. Background sessions have restricted memory behavior; do not assume they expose
the interactive interface or can write memory. Use the current [Claude memory documentation](https://code.claude.com/docs/en/memory)
for the available interface, scope and background-session limits.
