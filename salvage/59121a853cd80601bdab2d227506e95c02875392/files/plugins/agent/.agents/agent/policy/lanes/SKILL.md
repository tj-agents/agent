---
name: lanes
description: Use when choosing model capability for a task, skill, agent or workflow stage, or updating the shared lane ladder.

kind: policy
domain: agent
profile: core
applicability: agent sessions
requires: native host
provenance: house
---

# Lanes

Owns matching model capability and thinking effort to the reasoning each unit of work needs.

```text
Deep architectural research with consequential interacting design decisions -> L1.
Implement already-designed launcher validation and focused tests -> L4.
Apply a decided configuration replacement across known files -> L5.
```

Choose the cheapest lane that supplies the needed reasoning. L1 is strongest; L7 is cheapest. The same
lane expresses the same relative capability on Claude and Codex.

| Lane | Work |
|---|---|
| **L1** | Critical analysis, deep architectural research, consequential design with interacting constraints |
| **L2** | Difficult bounded analysis or judgement requiring careful reasoning |
| **L3** | Ordinary open investigation or bounded uncertain design |
| **L4** | Specified implementation, ordinary review, small local testable choices |
| **L5** | Mechanical work whose shape is already decided |
| **L6** | Bulk clerical work whose input outgrows the cheapest rung |
| **L7** | Small clerical work: commit, push, sync, one poll |

Select for the reasoning inside the unit of work. Critical analysis can arise in diagnosis or review;
its task label does not cap its lane. A handoff, file count, or delivery operation does not raise it.
Reconsider when the work changes: a strong design phase can be followed by cheaper implementation.

Declare lanes, never model names. Exact model and effort pairs live only in
[`claude.json`](../../resources/lanes/claude.json) and
[`codex.json`](../../resources/lanes/codex.json), under `.agents/agent/resources/lanes/`.
Run the adjacent `resolve.py --lanes` for current pairings. A recorded context ceiling of `null` is
unknown; Claude L7 also has a smaller window and accepts no effort setting.

Only a leaf skill performing one shape of work declares `lane:`. Policies, supporting standards, and
lifecycles inherit the session's model and select lanes for their bounded work; loading guidance must
not change the capability of the task that loaded it. An absent declaration means inheritance.

The generator resolves a skill's lane to Claude's model and effort, or Codex's model alone. Codex has
no per-skill effort key; use a lane agent when both settings must be carried together. Lane agents
also suit bounded loops and bulk work without changing the parent's model. Workflow stage pins derive
from the same tables. Handoffs require an explicit `--lane L1`–`L6`; L7 stays inside an existing session.
Claude L1 is a routing proxy to Codex L1.

The parent retains ownership and gives delegated work explicit paths and responsibility. Reconcile its
result before reusing those paths. A small follow-up may stay with the parent. If cheaper capability
is unavailable, a capable owner continues and reports a material fallback once; recording a lane does
not prove a model switch. Missing capability needed for correctness remains a gate.
