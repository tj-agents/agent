---
name: message
description: Send a follow-up or correction to an existing Codex session and verify recipient delivery. Use when asked to message another Codex session, check whether it received a follow-up, or recover from a queued message that did not arrive.
kind: utility
domain: agent
profile: core
applicability: agent sessions
requires: python, codex app-server
provenance: house
---

# Verified Codex follow-up

Owns one verified message attempt to an independent Codex session. In a Claude session this transport is
unavailable and must report `unavailable` without sending; use native child-agent message tools for
in-conversation peers on either host.

Use this only when the user authorized messaging the other session. Keep ownership of the current task.
For a child agent in the current conversation, use the host's native collaboration message tool instead.
This utility addresses an independent, already running Codex session; it does not perform a handoff.

Requires Python 3.9+, a native Codex executable with `app-server proxy` and the experimental direct-input
and paginated-history APIs, and an existing shared app-server daemon in the selected Codex profile.
The helper never starts a daemon, launches/resumes a session, edits Codex databases, changes configuration,
or injects terminal keystrokes. A standalone embedded TUI may have no reachable messaging endpoint.
Missing/older capabilities fail explicitly. Verified against the local 0.157.1 protocol schemas;
fixture validation is not proof that a particular live session is reachable.

1. Bind the exact thread UUID, expected working directory and Codex profile from the prior launch result
   or user-authorized session inspection. Never guess the newest conversation, a PID, or a window title.
2. Write the exact authorized message to a UTF-8 file. Choose a new receipt path outside version control
   and reuse it for this message. Do not put private message/history data into repository fixtures.
3. Invoke the shipped helper with properly quoted shell arguments. `--codex` accepts a native executable
   path, not a command string; supply the actual executable if the PATH entry is a shell shim.
   Use `python` on Windows or `python3` on Linux/macOS for both helper commands below.

```text
python -B "<skill-directory>/scripts/codex_followup.py" --codex "<codex-executable>" --timeout 20 send --thread <exact-uuid> --cwd "<expected-directory>" --codex-home "<selected-profile>" --message-file "<message-file>" --receipt "<receipt-file>"
```

The helper checks the server's thread identity, cwd, loaded membership and direct-input capability.
It steers the identified active turn using `expectedTurnId`, or starts a turn on the same loaded idle
thread. It preserves the recipient's model, permissions, working directory and configuration. If the server
requests an approval or another client action, the helper closes without answering it; the recipient UI
remains responsible.
Before submission it exclusively creates a receipt containing a client message ID and message hash.
It then checks recipient history for that client ID. Receipt files are private runtime evidence.

Reusing a receipt only checks status; it never submits again. A disconnect or timeout after reserving a
receipt is uncertain delivery, not permission to retry with a fresh receipt. Check with:

```text
python -B "<skill-directory>/scripts/codex_followup.py" --codex "<codex-executable>" --timeout 20 status --receipt "<receipt-file>"
```

Report the returned status accurately:

- `delivered` (exit 0): the matching user message appears in the exact recipient's conversation. This
  proves delivery, not acknowledgment, execution, or completion. Claim acknowledgment only after an
  actual recipient response supports it.
- `accepted` or `unverified` (exit 2): delivery remains unproven. Report that plainly and retain the
  receipt. A missing queue entry, successful CLI exit, or timeout is not delivery evidence.
- `unavailable` or `rejected` (exit 2): report the reason. When the helper confirms no send was attempted
  and no receipt exists, first assess whether the already authorized task permits supported coordination
  with the exact owner. Verify the native id, cwd, and selected profile. Follow the shared
  task recovery context, then use `codex:open` only for the
  verified exact owner. A verified live owner cannot be resumed. Reconcile an unknown owner independently before
  `--confirm-closed`, and never treat that flag as source release. A successful reopen is launch evidence, not delivered follow-up, restored
  transport, or acknowledgment. Retain the current task. Only when all supported automated coordination
  paths are unavailable or ineligible, report the blocker and provide the authorized message for the user to paste. If a receipt
  already exists for a different message or target, or
  `status` found no valid receipt to check, do not paste blindly — disclose that a prior or different send
  may exist. Do not resume, paste, or replay it; check the target session directly before resending anything.

Do not substitute `codex queue` success for verified delivery. A queue entry can persist without the
recipient consuming it, particularly for an embedded session without a shared server. An older queued
message may still arrive later; do not silently enqueue, resume with it, or paste another copy. Do not repair
transport by starting a daemon or second owner, changing the recipient's profile, or restarting its terminal.
