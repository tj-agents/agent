---
name: inspect
description: Inspect recorded agent execution, session, ownership and reference state without changing the registry. Use when reading the agent store or diagnosing its recorded state; native process liveness and recovery belong to Machine.
kind: utility
domain: agent
profile: core
applicability: agent sessions
requires: python
provenance: house
lane: L7
---

# Inspect the agent store

Read the existing execution and session registry without claiming work, repairing records or launching a
host. Requires Python 3.9 or newer. Records describe persisted metadata; an unended owner is not evidence
that its native process is alive. Use the recorded handle when a separately authorized task needs Machine's
native observation or recovery.

Run from the loaded skill directory. The default database is the existing sessions.db beneath
AGENT_STATE_DIRECTORY, or the user's .agents-state directory when that override is absent.

```text
Windows: python -B "<skill-directory>/scripts/inspect.py" --execution-key "github.com/tj-agents/core:workflow/agent-store-architecture"
Linux/macOS: python3 -B "<skill-directory>/scripts/inspect.py" --repository "github.com/tj-agents/core"
Result: state_kind is "recorded" and liveness is "not-observed"; work_items, sessions and references contain stored rows.
```

Omit the selector for a complete registry inventory. Use `--database <path>` to inspect an explicitly
selected database. `--execution-key` and `--repository` are mutually exclusive. The operation emits one
JSON object to stdout, with the observed schema_version and sorted collections. An unknown execution,
missing database, unsupported schema or invalid store produces an error object on stderr and exit 1;
invalid arguments exit 2. It never creates the database or migrates its schema.

The canonical runtime beneath `.agents/agent-store/runtime/` owns schema, identities, references,
transactions and read validation. Machine supplies native observations and lifecycle events to that
runtime. Package consumers embed the same canonical bytes under their own package root. Inspection
imports neither a sibling plugin nor Machine and makes no liveness, completion or transfer decision.
