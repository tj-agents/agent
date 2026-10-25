# Preserve task authority during recovery

Use this contract when reopening or coordinating with an exact prior task owner. Read the canonical Goal
and the existing authority evidence before preparing the private recovery instruction or a tool approval
justification. Recovery carries existing authority; it does not create, expand, or reinterpret it.

Write the private UTF-8 recovery instruction outside version control. Include reviewable task-specific
evidence in both that file and any required tool approval justification:

- exact host, native session ID, selected profile, and recorded working directory;
- canonical Goal path, actual blocker, and the one requested coordination action;
- the actual authorization wording and its provenance, such as the user request, Goal entry, or recorded
  delivery authority that grants the action;
- the authorized scope, explicit limits, holds, and remaining gates; and
- the exact-owner and one-writer state, including every child writer that must acknowledge, release, or be
  reconciled before conflicting work.

Use the current host tool's per-call approval field for this evidence. For Codex `exec_command` requests that
require `sandbox_permissions=require_escalated`, put the task evidence in the `justification` argument. A static
permission-rule justification and a prompt-file path do not transmit the per-task evidence. Never assume the
reviewer reads the private file. If a host exposes no supported field, retain the true gate, name the missing
capability, and use no workaround.

A Goal path alone, or an assertion that work is "authorized", is not enough. State the task-specific
evidence that lets the reviewer assess the requested action. Do not claim approval that the evidence does
not establish.

When the same action remains authorized and the scope is unchanged, continue without routine repeat
consent. Preserve an actual approval-review rejection or ambiguous result as a gate: do not bypass it,
retry it as though it succeeded, or treat recovery as source release. Continue independently authorized
preparation that does not depend on that rejected action, and name the blocked action, its resolver, and
the observable condition for resuming it.
