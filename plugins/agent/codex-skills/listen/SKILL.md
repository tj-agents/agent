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

Read and follow the [canonical definition](../../.agents/agent/utility/listen/SKILL.md) in full.
This discovery entry is generated; edit the referenced `.agents/` definition.

Use the shipped Codex listener at `<agent-package>/.agents/agent/runtime/codex/listener.py`.
Resolve `<agent-package>` from this installed Agent discovery entry: it is the directory containing
`codex-skills/` and `.agents/`. From its `codex-skills/listen/` directory, the runtime is
`../../.agents/agent/runtime/codex/listener.py`. Keep the caller's command directory explicit.

The listener requires Python 3.11+, `CODEX_THREAD_ID`, this session's registered frontend/profile/backend binding,
and the connected native server's `turn/start.toolOutput` capability. `start` verifies these before
running the command. Report its concrete prerequisite failure; a process handle or queued user message
does not provide this callback.

Use a stable identity for the exact job, including the watched commit or run where relevant. Retain
the returned job directory and reuse that listener for the same identity. Run an executable with its
argument list; use the caller's existing bounded watcher or capture script for more complex commands.

```text
Windows:
python -B "<agent-package>/.agents/agent/runtime/codex/listener.py" start --identity "tests:<full-commit-sha>:runtime" --purpose "Runtime regression tests" --cwd "<repository-root>" --timeout 900 -- python -B -m unittest discover -s .agents/tests -p test_runtime_suite.py

Linux:
python3 -B "<agent-package>/.agents/agent/runtime/codex/listener.py" start --identity "tests:<full-commit-sha>:runtime" --purpose "Runtime regression tests" --cwd "<repository-root>" --timeout 900 -- python3 -B -m unittest discover -s .agents/tests -p test_runtime_suite.py

Inspect an existing listener, using python3 on Linux:
python -B "<agent-package>/.agents/agent/runtime/codex/listener.py" status --job "<returned-job-directory>"
```

The runtime owns the bounded command, captured output and terminal callback through the existing
native connection. It sends `turn/start` with empty `input` and `toolOutput`, preserving the result
as tool output during an active turn or starting generation when idle. Independent work and new user
input do not cancel the listener.

A delivery acknowledgement proves native acceptance; it does not prove the owner has observed or
handled the result. Consume the exact completion once through the shared definition. If delivery is
unknown, inspect the recorded job and conversation without retrying the callback or restarting the
command. A native capability missing from this session remains a reported prerequisite failure.
