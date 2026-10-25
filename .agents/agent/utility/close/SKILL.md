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

Use `python` on Windows and `python3` on Linux:

```text
python -B "<skill-directory>/scripts/agent_close.py" prepare --session-id "<exact-id>" --profile "<profile>" --target target.json
python -B "<skill-directory>/scripts/agent_close.py" arm --host codex --session-id "<exact-id>" --turn-id "<current-turn>" --profile "<profile>" --target target.json --cli-close "<declared-cli-close-script>" --completion complete.json --cancel cancel.json
```

`prepare` resolves Codex's current native submitted input to one managed frontend, including private
`--no-daemon` clients. Claude requires its verified startup receipt and latest user UUID in the exact
transcript; pass `--transcript` when it cannot be found uniquely. Missing proof reports `unavailable`. The native
capability receives only `{pid,started_at}` and holds that exact process identity until it signals. Agent resolves
the explicit `--cli-close` capability to its nearest package root and requires its exact shipped
`.agents/cli/utility/close/scripts/process_identity.py`; no omitted capability is inferred. Agent
freezes the native input, profile, backend and working turn. The request retains its original close mode and
generation until an outcome; another request cannot replace or upgrade an unresolved literal close.

Arm before producing the final reply. The native Stop adapter signals the returned completion token
synchronously for that exact armed turn; the next UserPromptSubmit cancels it for the same host/session.
Claude binds arming to the latest non-sidechain user UUID in its exact transcript. Its Stop adapter requires
that same transcript to end in an assistant `end_turn` response after that user; tool-use, a later user, a
missing transcript, or changed path refuses completion rather than guessing from a session-wide Stop.
After completion, the watcher requires the stopped Store activity token to remain idle for three seconds.
It repeats full native attribution while CLI holds the exact process handle, then rechecks the request,
cancellation, transcript and idle token immediately before signalling. A new prompt, SessionEnd, another
Stop or changed proof preserves the frontend. Closure is recorded only after CLI confirms its exit;
the shared backend, terminal and checkout remain open.
For linked-worktree removal, Agent or Engineering separately creates fresh (at most 300 seconds) external
eligibility binding the exact worktree, branch, head, remote base and base OID, with positive merged/no-other-owner
evidence and provenance. If prepare or the native capability refuses, keep the session open and report the reason.
Never close a peer; use `agent:peer`.
