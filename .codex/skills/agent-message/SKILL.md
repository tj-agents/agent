---
name: message
description: Send a follow-up or correction to an existing agent session using the current host's native messaging. Use when asked to message another session, check delivery, or correct an earlier message.
kind: utility
domain: agent
profile: core
applicability: agent sessions
requires: native host
provenance: house
---

# Message an agent session

Read and follow the [canonical definition](../../../.agents/agent/utility/message/SKILL.md) in full.
This discovery entry is generated; edit the referenced `.agents/` definition.

## Codex messaging

Use the native collaboration message tool for a child in the current conversation, following its exposed
recipient schema. For an independent session, use `codex agents` or authorized session inspection to bind
the exact thread UUID, expected checkout and selected `CODEX_HOME`. For execution-scoped sends, verify the
unended owner or bounded responsibility against that checkout and profile before submission.

Ordinary sends use the native executable's queue command in the selected profile:

```text
codex queue --thread <exact-thread-uuid> --message "The failing request is tenant-scoped; keep the tenant boundary in the API repair."
```

Use properly quoted arguments and the actual native executable if the shell command is a shim. Use
`--remote <verified-endpoint>` only when the authorized recipient belongs to that server. Successful native
output means `queued`; retain its submission identifier. The CLI does not return recipient-history proof
or its generated client message ID. A failed or interrupted invocation may still have submitted the
message: preserve its output, check the recipient, and do not automatically retry or send through another
transport. Native `agents` uses the shared app server and does not enumerate `--no-daemon` sessions.

## Required delivery proof or active correction

Use the retained Codex helper only when recipient-history proof or correction of an active turn is
explicitly required. It is not an automatic fallback from an uncertain queue attempt. Resolve
`<package-root>` to this installed Agent package; its helper and dependencies must remain within that
nearest package, with no author-checkout, cache or sibling-package fallback.

Requires Python 3.9+, a native Codex executable with `app-server proxy` and the experimental direct-input
and paginated-history APIs, and an existing shared app-server daemon in the selected Codex profile.
The helper never starts a daemon, launches/resumes a session, edits Codex databases, changes configuration,
or injects terminal keystrokes. A standalone embedded TUI may have no reachable messaging endpoint.
Missing/older capabilities fail explicitly. The retained helper uses experimental protocol capabilities;
fixture validation is not proof that a particular live session is reachable.

1. Bind the exact thread UUID, expected working directory and Codex profile from the prior launch result
   or user-authorized session inspection. Resolve the recipient's execution key and concrete reason for
   delivery. Never guess the newest conversation, a PID, or a window title. The helper admits ordinary
   sends only to one unended Agent Store owner or bounded responsibility for that execution.
2. Write the exact authorized message to a UTF-8 file. Choose a new receipt path outside version control
   and reuse it for this message. Do not put private message/history data into repository fixtures.
3. Invoke the shipped helper with properly quoted shell arguments. `--codex` accepts a native executable
   path, not a command string; supply the actual executable if the PATH entry is a shell shim.
   Use `python` on Windows or `python3` on Linux/macOS for both helper commands below.

```text
python -B "<package-root>/.agents/agent/runtime/codex/codex_followup.py" --codex "<codex-executable>" --timeout 20 send --thread <exact-uuid> --cwd "<expected-directory>" --codex-home "<selected-profile>" --message-file "<message-file>" --receipt "<receipt-file>" --execution-key "<verified-execution-key>" --reason "<decision or action this information supports>"
```

For a correction to an earlier mistaken delivery, use `--corrects <original-receipt>` instead of
`--execution-key`. The original delivery must be observable in the same recipient history; a correction
does not create responsibility for the repair.

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
python -B "<package-root>/.agents/agent/runtime/codex/codex_followup.py" --codex "<codex-executable>" --timeout 20 status --receipt "<receipt-file>"
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
  task recovery context, then use `agent:open` only for the
  verified exact owner. A verified live owner cannot be resumed. Reconcile an unknown owner independently before
  `--confirm-closed`, and never treat that flag as source release. A successful reopen is launch evidence, not delivered follow-up, restored
  transport, or acknowledgment. Retain the current task. Only when all supported automated coordination
  paths are unavailable or ineligible, report the blocker and provide the authorized message for the user to paste. If a receipt
  already exists for a different message or target, or
  `status` found no valid receipt to check, do not paste blindly — disclose that a prior or different send
  may exist. Do not resume, paste, or replay it; check the target session directly before resending anything.

A previous native queue attempt can still arrive later. Do not invoke this helper for the same message
until that attempt is independently reconciled; its receipt cannot correlate a prior queue submission.
