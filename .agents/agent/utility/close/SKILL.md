---
name: close
description: Assess the current session before its final reply, keeping unfinished work open or preserving a prepared refresh restart, and requesting completion closure only after every owned outcome is finished. Use for the current session; use agent:peer for another session.
kind: utility
domain: agent
profile: core
applicability: agent sessions
requires: bundled CLI process runtime
provenance: house
---

# Close the current session

Owns the current session's completion decision and preservation of a prepared refresh restart. Before
every final reply, ensure the current native input has an assessment. Except for the already armed
refresh checkpoint below, run `assess` once with the exact native host, session and profile, a decision,
and a nonempty reason. Use `python` on Windows and `python3` on Linux. Choose the invocation matching
the decision:

```text
python -B "<skill-directory>/scripts/agent_close.py" assess --host codex --session-id "<exact-id>" --profile "<profile>" --decision keep-open --reason "Linux validation is still pending."
python -B "<skill-directory>/scripts/agent_close.py" assess --host codex --session-id "<exact-id>" --profile "<profile>" --decision complete --reason "Delivery, validation and the authorized handoff are complete."
```

A prepared [agent:refresh-plugins](../refresh-plugins/SKILL.md) checkpoint with a successfully armed
current-input restart already supplies the required assessment. Preserve it when the resumed client
will continue the same conversation and execution owner. Continuing that goal does not require another
assessment. A restart replaces the frontend; it does not declare the goal complete.

```text
Refresh result: current-input restart armed; checkpoint prepared for the same conversation and owner.
Final reply: report the checkpoint and preserve the armed restart without another assess call.
```

Outside that prepared checkpoint, choose `keep-open` while work, validation, delivery or an authorized
handoff remains. A user question, pending approval, unprepared handoff, remaining cleanup or explicit
keep-open request still requires `keep-open`, cancelling even an armed refresh restart. Choose
`complete` only after every owned outcome is finished. A Stop event, merged PR or question about a
missed close-out does not establish completion.

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
