---
name: close
description: Assess the current session before its final reply, keeping unfinished work open and requesting closure only after every owned outcome is finished. Use for the current session's completion decision; use agent:peer for another session.
kind: utility
domain: agent
profile: core
applicability: agent sessions
requires: bundled CLI process runtime
provenance: house
---

# Close the current session

Owns the current session's completion decision. Before every final reply, run `assess` once with the
exact native host, session and profile, a decision, and a nonempty reason. Use `python` on Windows and
`python3` on Linux. Choose the invocation matching the decision:

```text
python -B "<skill-directory>/scripts/agent_close.py" assess --host codex --session-id "<exact-id>" --profile "<profile>" --decision keep-open --reason "Linux validation is still pending."
python -B "<skill-directory>/scripts/agent_close.py" assess --host codex --session-id "<exact-id>" --profile "<profile>" --decision complete --reason "Delivery, validation and the authorized handoff are complete."
```

Choose `keep-open` while work, validation, delivery, an authorized handoff, a user question, approval or
cleanup remains, or the user explicitly requests it. Choose `complete` only after every owned outcome
is finished. A Stop event, merged PR or question about a missed close-out does not establish completion.

`assess` binds the decision to the current native-hook input independently of frontend attribution.
`keep-open` records the decision and cancels a pending close request. `complete` resolves and freezes
the exact frontend, arms its watcher, and returns the request paths and token. Codex requires the exact
turn and managed client attribution; Claude requires the exact transcript's latest user UUID. Pass
`--host claude` for Claude and `--transcript "<exact-path>"` when its transcript cannot be found uniquely.

If close proof is unavailable, the command records the current-input assessment and reports the reason.
Preserve the session and report that reason. The Stop reminder asks for a missing assessment once;
follow it with this same command.

The runtime owns request validation and cancellation. A new prompt, SessionEnd, repeated Stop or changed
proof cancels pending closure. It revalidates native attribution while bundled `tj_cli.processes` holds
the exact process identity. Closure is recorded only after the frontend exits; the shared backend,
terminal and checkout remain open. Use `agent:peer` for another session.

Linked-worktree removal requires a separate explicit eligibility contract from Agent or Engineering:
fresh (at most 300 seconds) external evidence binding the exact worktree, branch, head, remote base and
base OID, with positive merged/no-other-owner evidence and provenance.
