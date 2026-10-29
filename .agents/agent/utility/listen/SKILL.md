---
name: listen
description: Run one deterministic command in the background and return its result through the current host completion notification. Use when waiting for a build, test, CI monitor or other deferred result while continuing work or parking the turn.
kind: utility
domain: agent
profile: core
applicability: agent sessions
requires: native host
provenance: house
---

# Background listener

Own background execution and completion delivery for one deterministic command. The caller owns the
command's meaning, authorization, result handling and next action. Use the current host entry point;
keep one listener for that command or exact watched identity.

```text
Input: an authorized CI watcher for one repository, commit and check run.
Start: pass that exact identity, bounded command, directory and purpose to the host adapter.
Continue: retain its listener identity while performing independent work or parking the turn.
Complete: consume that listener's result once and resume the owning task's next authorized action.
```

Invoke this skill when a deferred result would otherwise leave the agent waiting or require the user
to ask again. Start the listener before reporting that wait or moving to independent work. Run ordinary
scripts and tools to wait; a model must not poll unchanged state. A job-specific watcher owns its
condition, bounded timeout and compact output, then exits when a decision is needed. Reuse an existing
watcher instead of creating another polling implementation. Short commands that finish inline stay
in the foreground. Use the caller's existing log/capture wrapper for verbose commands so completion
returns a compact result and the full artifact path.

Pass the exact command, working directory, purpose and completion condition through the host adapter.
Retain its returned listener identity with the current task and reuse an existing listener for the same
job. If the command already runs without a listener, use an available native attachment mechanism;
report a missing attachment capability instead of launching the command again. A process ID alone does
not establish completion delivery.

On the native completion event, read that task's result once, verify its identity, report the outcome
and perform the caller's next authorized action. Keep existing host notification settings for desktop
alerts. Do not add a separate alert service or alter those settings as listener setup. A failed command,
query error or timeout also returns to the owner; it does not silently restart the command.

Only use notification primitives documented by the current surface. If none can return completion,
state the concrete missing capability before starting the command and continue supported foreground
work. A fresh headless session or OS scheduler can own durable work through its existing workflow, but
does not replace this listener's return path. The caller retains task ownership and its existing authority.
