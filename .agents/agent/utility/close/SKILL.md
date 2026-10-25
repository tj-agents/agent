---
name: close
description: Complete the current agent turn and request closure only after every owned outcome is finished. Do not use for another session.
kind: utility
domain: agent
profile: core
applicability: agent sessions
requires: cli close capability
provenance: house
---

# Close the current session

Owns the completion decision and exact registry/launch binding for this session, not terminal control.
Close only when delivery, validation, and authorized handoffs are complete and no user answer, approval,
or check remains. Respect an explicit keep-open request; a question about a missed close-out is a repair
outcome, not authorization to close.

```json
python -B "<skill-directory>/scripts/agent_close.py" prepare --session-id "<exact-id>" --target target.json
python -B "<skill-directory>/scripts/agent_close.py" arm --target target.json --cli-close "<declared-cli-close-script>" --receipt receipt.json --completion complete.json --cancel cancel.json
```

`prepare` accepts only a separately recorded exact `frontend` identity and terminal identity with exact
`binding`; a legacy host PID, including a shared daemon PID, is ineligible. Missing evidence reports
`unavailable`. The target passed to CLI is exactly `{schema_version, frontend:{pid,started_at},
terminal:{kind,...,binding:{pid,started_at}}}`. CLI owns rechecking native process/terminal state and its
durable receipt; Agent does not infer a target or treat arming as closure.

After the final reply, signal the returned completion token with `complete`; on a new prompt or changed
eligibility, signal `cancel` instead. For linked-worktree removal, Agent or Engineering separately creates
fresh (at most 300 seconds) external eligibility binding this exact target, worktree, branch, head, remote
base and base OID, with positive merged/no-other-owner evidence and provenance. CLI alone rechecks native
and Git proof. If prepare or CLI refuses, keep the session open and report the reason. Never close a peer;
use `agent:peer`.
