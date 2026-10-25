---
name: open
description: Open a Claude Code CLI in a new terminal tab on an exact directory, optionally resuming a known session, with the full native colour TUI. Use when asked to open a Claude CLI, open a terminal running Claude, or reopen/resume a past conversation in its own window.

kind: utility
domain: agent
profile: core
applicability: claude sessions
requires: python, claude
provenance: house
route: infer
---

# Open a Claude CLI

This is the Claude host adapter behind `agent:open`. Use `agent:open` when the request
names a session, topic or directory; use this skill only to start a fresh Claude session.

Opens an interactive Claude Code session the user drives themselves. Its sibling `cc:handoff` is for
delegating prepared work to a second Claude; this one is for putting a terminal in front of the user.
The terminal invocation and the environment scrub belong to `scripts/agent_cli.py` under
`.agents/agent/runtime`, shared with `cc:handoff`, `codex:handoff`, and the Python terminal
profile launchers.

Never open a Claude CLI by typing `claude` into a shell command, an npm shim such as `claude.cmd`, or
`node.exe`, or any other intermediate process. Those resolve to the npm/NVM shim, which runs the TUI
through an extra process and degrades it to a monochrome, non-interactive window. Use this launcher,
which starts the native `claude` executable as the tab process.

Resolve the exact directory the request concerns and do not substitute another checkout.
Run the launcher with `python` on Windows or `python3` on Linux/macOS:

```sh
# Windows
python '<skill-directory>/scripts/open_claude.py' --working-directory '<absolute-path>' --title '<short-title>'
# Linux/macOS
python3 '<skill-directory>/scripts/open_claude.py' --working-directory '<absolute-path>' --title '<short-title>'
```
On Windows, `python3` is often the Microsoft Store
alias stub rather than a real interpreter, and it fails rather than running the launcher.

- `--resume '<session-id>'` first resolves the exact saved session and its original directory, checks for a
  live writer, and preserves the selected Claude profile. Reconcile an unknown live identity before resuming.
  `--continue` reopens the most recent session for that directory.
- `--confirm-closed` records the user's explicit reconciliation that an unknown saved session has no live writer;
  it never overrides a verified live session.
- `--prompt '<short instruction>'` seeds a first turn. For anything substantial write a file and pass
  `--prompt-path` instead; never relay a long prompt through a nested command string.
- `--model '<model-id>'` only when the user named one or a checked-in selection policy already resolved
  it. Omitted, the CLI uses its own configured default.
- `--dangerously-skip-permissions` only when the user asks for it in that request.

For task recovery, use the packaged Agent task-recovery context. Write its private
UTF-8 instruction file outside version control and pass it with `--prompt-path '<recovery-prompt-file>'`.
Do not also pass `--prompt`.

It opens a tab in whichever terminal it detects it is already running inside (Windows Terminal, tmux,
kitty or Konsole). With none detected, it opens a new window instead and prints a warning — except on
Windows, where a `wt.exe` it can still find opens a tab in the most recently used Windows Terminal window
rather than a new window. Open one tab per request, and report the directory and any session id
afterwards.

## One tab, and never a second

The launcher **exits non-zero** on failure and prints `Launched claude tab '<title>' …` on success.
The tab applies plugin updates published since the last check, then starts the native CLI.

Exit code **3 is distinct from every other failure**: it means the terminal control command timed out
after the tab may already have opened, not that the launch definitely failed. Check the terminal for the
tab before doing anything else; never relaunch automatically on exit 3. Any other non-zero exit means the
launch did not happen.

**Never verify a launch by listing processes, and never re-run the launcher because one looked absent.**
A tab takes seconds to appear and a process listing is trivially misread — an unsorted `Select-Object
-First 3` is enough to miss the newest one. Re-running puts two agents on the same task in the same
repository, which is worse than no handoff at all: they collide on the same files with neither aware of
the other.

If the launcher printed its confirmation, the handoff happened. Report it and stop. If it exited
non-zero, say so; do not retry blind — and never retry on exit code 3 specifically, where a retry risks a
second tab for the same request.

