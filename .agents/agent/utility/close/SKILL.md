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
python -B "<skill-directory>/scripts/agent_close.py" prepare --session-id "<exact-id>" --profile "<profile>" --target target.json
python -B "<skill-directory>/scripts/agent_close.py" arm --host codex --session-id "<exact-id>" --turn-id "<current-turn>" --profile "<profile>" --target target.json --cli-close "<declared-cli-close-script>" --completion complete.json --cancel cancel.json
```

`prepare` accepts only a recorder-attributed managed frontend with its exact PID and creation time; a legacy
host PID, including a shared daemon PID, is ineligible. Missing evidence reports `unavailable`. The native
capability receives only `{pid,started_at}` and holds that exact process identity until it signals. Agent resolves
the explicit `--cli-close` capability to its nearest package root and requires its exact shipped
`.agents/cli/utility/close/scripts/process_identity.py`; no omitted capability is inferred. Agent
freezes the native input, profile, backend and Store activity state and does not treat arming as closure.

Arm before producing the final reply. The native Stop adapter signals the returned completion token
synchronously for that exact armed turn; the next UserPromptSubmit cancels it for the same host/session.
Claude binds arming to the latest non-sidechain user UUID in its exact transcript. Its Stop adapter requires
that same transcript to end in an assistant `end_turn` response after that user; tool-use, a later user, a
missing transcript, or changed path refuses completion rather than guessing from a session-wide Stop.
For linked-worktree removal, Agent or Engineering separately creates fresh (at most 300 seconds) external
eligibility binding the exact worktree, branch, head, remote base and base OID, with positive merged/no-other-owner
evidence and provenance. If prepare or the native capability refuses, keep the session open and report the reason.
Never close a peer; use `agent:peer`.
