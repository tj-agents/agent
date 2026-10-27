---
name: listen
description: Run one deterministic command in the background and return its result through the current host completion notification. Use when waiting for a build, test, CI monitor or other deferred result while continuing work or parking the turn.
kind: utility
domain: agent
profile: core
applicability: agent sessions
requires: native host
provenance: house
---

# Background listener

Read and follow the [canonical definition](../../../.agents/agent/utility/listen/SKILL.md) in full.
This discovery entry is generated; edit the referenced `.agents/` definition.

Codex starts the shipped listener, which owns one detached command and returns its compact result to the exact current thread through Codex's native app-server input. Run it with Python 3.9+ after replacing the placeholders; the command begins after `--` and is never a shell string:

```text
python -B "<skill-directory>/scripts/codex_listener.py" start --purpose "exact condition" --timeout 7200 -- python -B .agents/workflows/workflow_ops.py --workflow-run-id <id> monitor --kind pr --id <pr> --head <sha>
```

Keep the returned listener ID. `status --listener-id <id>` reads its durable state. Do not use a foreground `gh ... --watch`, polling loop, or an ordinary background shell process for a deferred delivery decision.
