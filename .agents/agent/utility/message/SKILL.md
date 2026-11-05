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

Owns one verified message attempt to an independent Codex session. The same shipped Python helper runs
from either a Claude or Codex caller.

Use this only when the user authorized messaging the other session. Keep ownership of the current task.
For a child agent in the current conversation, use the host's native collaboration message tool instead.
This utility addresses an independent, already running Codex session; it does not perform a handoff.

Requires Python 3.12+, native Codex 0.166.0 or newer with `app-server proxy`, the experimental direct-input
and paginated-history APIs, and a verified managed client binding in the selected Codex profile. Managed
local starts, handoffs and eligible closed local recoveries use their owned endpoint. Required SessionStart
acknowledgments establish startup readiness; messaging also requires verified native input and recipient
binding. A blank initial prompt can leave that binding pending until actual native input is attributed.
The helper never starts a daemon, launches/resumes a session, edits Codex databases, changes configuration,
or injects terminal keystrokes. Existing live embedded sessions have no supported automated attachment.
Ordinary explicit remote launches preserve their requested endpoint without a managed message binding.
Missing capabilities or binding evidence cause refusal before submission. Fixture validation does not
prove that a particular live session is reachable.

1. Bind the exact thread UUID, expected working directory and Codex profile from the prior launch result
   or user-authorized session inspection. When a verified `running` status receipt exists, use its
   `client_binding` and pass its path as `--launch-receipt`. Ordinary managed shell starts have no launch
   status receipt: the helper resolves exactly one durable `cli-clients/*/client.json` binding matching
   the exact thread, normalized cwd and profile. Missing, stale or multiple candidates refuse before
   connection; the helper never selects the newest record or the current default daemon.
   Supply `--thread`, `--cwd` and `--codex-home` explicitly with either binding source. Before submission,
   the helper revalidates local endpoint, executable, process and native input identities, then confirms
   the exact loaded recipient and history through that frozen endpoint. Conflicting identity refuses.
   Resolve the recipient's execution key and concrete reason for delivery. Never guess a conversation,
   PID or window title. The helper admits ordinary sends only to one unended Agent Store owner or bounded
   responsibility for that execution. Contradictory Store responsibility remains a refusal.
2. Write the exact authorized message to a UTF-8 file. Choose a new receipt path outside version control
   and reuse it for this message. Do not put private message/history data into repository fixtures.
3. Invoke the shipped helper with properly quoted shell arguments. `--codex` accepts a native executable
   path, not a command string; supply the actual executable if the PATH entry is a shell shim.
   Use `python` on Windows or `python3` on Linux/macOS for both helper commands below.

```text
python -B "<skill-directory>/scripts/codex_followup.py" --codex "<codex-executable>" --timeout 20 send --thread <exact-uuid> --cwd "<expected-directory>" --codex-home "<selected-profile>" --launch-receipt "<handoff-status-path>" --message-file "<message-file>" --receipt "<receipt-file>" --execution-key "<verified-execution-key>" --reason "<decision or action this information supports>"
```

Omit `--launch-receipt` when the exact managed recipient is resolved from its durable client binding.
An omitted receipt never authorizes a default-daemon route.

For a correction to an earlier mistaken delivery, use `--corrects <original-receipt>` instead of
`--execution-key`. The original delivery must be observable in the same recipient history; a correction
does not create responsibility for the repair.

The helper checks the server's thread identity, cwd, loaded membership and direct-input capability.
It steers the identified active turn using `expectedTurnId`, or starts a turn on the same loaded idle
thread. It preserves the recipient's model, permissions, working directory and configuration. If the server
requests an approval or another client action, the helper closes without answering it; the recipient UI
remains responsible.
Before submission it exclusively creates a version 2 receipt containing a client message ID and message
hash, and freezes the recipient, profile, endpoint, executable, process identities and native frontend input
anchor. It then checks recipient history for that client ID. Receipt files are private runtime evidence.

Reusing a receipt only checks status; it never submits again. Reuse and status checks verify the frozen
binding and connect only to that endpoint; missing or changed identity refuses the check. A disconnect
or timeout after reserving a receipt is uncertain delivery, not permission to
retry with a fresh receipt. Check with:

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
  task recovery context, then use `agent:open` only for the
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
