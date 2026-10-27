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

Owns one authorized message attempt to an exact existing recipient. Keep ownership of the current task;
a message does not transfer responsibility or perform a handoff. The current host's entry point selects
the native tools and any required delivery proof.

```text
Request: tell the existing API repair owner that the failing request is tenant-scoped
Selection: inspect native peers and bind the exact recipient, checkout and profile
Authority: verify that recipient owns or has bounded responsibility for the affected execution
Result: report the native submission status; claim delivery only from recipient evidence
```

Use messaging only when the user authorized it. Preserve the authorized scope, limits, holds and remaining
gates in the message. For an execution-scoped send, resolve the execution key and a concrete decision or
action the information supports; verify one unended owner or bounded responsibility for that execution.
A correction identifies the earlier mistaken delivery to the same recipient and does not create
responsibility for the repair.

Bind the exact native recipient from native discovery, a prior launch result or authorized inspection.
Distinguish a child in the current conversation from an independent session. Never infer a recipient from
the newest conversation, a PID, a visible title alone or a sibling checkout. Keep its working directory
and host profile attached to that identity.

Report submission, delivery, acknowledgment and completion separately. Native acceptance or a successful
CLI exit does not prove the recipient read the message. Claim acknowledgment only from an actual
recipient response, and completion only from evidence of the requested work.

Keep the native message identifier or private receipt for each attempt. A timeout, disconnect or missing
queue entry can leave delivery uncertain; inspect that same attempt and do not submit another copy or
switch transports while it may still arrive. Keep message and history evidence outside version control.

If no attempt was made, assess supported coordination with the exact authorized owner through
`agent:open`, preserving task-specific authority and recovery context. A verified live owner cannot be
resumed; unknown ownership needs independent reconciliation before `--confirm-closed`. Opening a session
proves a launch, not message delivery. When supported paths are unavailable or ineligible, report the
blocker and provide the authorized message for the user to paste. If a prior attempt may exist, check the
recipient directly before any replay or manual paste. Never repair messaging by starting a second owner,
changing the recipient's profile or restarting its terminal.
