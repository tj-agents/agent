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

Owns inspection and requested changes to the active host's native memory controls. Its host entry point
selects the supported interface and states the setting's scope.

```text
Request: is memory enabled in this Codex profile?
Action: inspect the native memories feature in the selected CODEX_HOME
Result: report its effective state and user-configuration scope without changing it
```

Inspect native state before reporting it. Establish the requested state and scope before a change; ask
only when either is unspecified. A request to inspect memory authorizes no change. Preserve unrelated
settings, and report when the native control is broader than the authorized scope instead of widening it.

Use the native host's memory interface. Do not create a settings format, write memory state through a
custom script, or treat instruction files, conversation history and every memory system as one switch.
Report the observed state, affected host/profile and scope, and any native limitation on when a change
takes effect. If the required native interface is unavailable, report that limitation.
