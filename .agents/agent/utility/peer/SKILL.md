---
name: peer
description: List and inspect agent peers through the current host, or resolve registered Claude and Codex sessions and their visible terminal tabs. Use when asked which agents or CLIs are running, what a peer is doing, or to inspect or close one exact authorized terminal target.

kind: utility
domain: agent
profile: core
applicability: agent sessions
requires: python, native host
provenance: house
route: infer
---

# Peer CLI sessions

Owns peer selection across native session identities and registered physical terminals. Use the current
host's native listing and inspection first; its entry point names those tools. A native agent handle and
a visible terminal title are different identities and are not interchangeable.

```text
Request: which peers are running in this host?
Action: inspect native peers and preserve their native handles
Request: inspect the visible tab "Postgres sweep: Search" across Claude and Codex
Action: resolve its recorded terminal identity, checkout and liveness
```

Use the deterministic registry and terminal operations below for cross-host inventory or a requested
visible terminal target. The runtime is Python 3.9+ on Windows and Linux; use `python3` on Linux/macOS and
`python` on Windows. Session names can come from a branch or directory, while a tab title comes from its
launcher. Use the exact visible title when the request names a physical tab, and verify the resulting
session identity before acting. Messaging follows `agent:message`.

```sh
python3 -B <skill-directory>/scripts/peer_cli.py list --host codex
python3 -B <skill-directory>/scripts/peer_cli.py inspect 'Postgres sweep: Search'
python3 -B <skill-directory>/scripts/peer_cli.py list --all
python3 -B <skill-directory>/scripts/peer_cli.py list --under /path/to/some-org
python3 -B <skill-directory>/scripts/peer_cli.py list --include-unrecorded
python3 -B <skill-directory>/scripts/peer_cli.py resolve 'Postgres sweep: Search'
python3 -B <skill-directory>/scripts/peer_cli.py close 'Postgres sweep: Search'
```

```sh
python -B <skill-directory>\scripts\peer_cli.py list
python -B <skill-directory>\scripts\peer_cli.py close "Postgres sweep: Search"
```

If that interpreter is not available, install Python 3.9+ and ensure `python` (Windows) or `python3`
(Linux/macOS) is on PATH.

`list` emits JSON and defaults to what the caller plausibly cares about: sessions under the current repository and under
its parent folder (sibling checkouts in the same org). `--under <path>` scopes explicitly instead; `--all`
drops scoping and shows every recorded session on the machine. Outside a git repository, `list` cannot
auto-scope and behaves like `--all`.

`--host claude|codex` narrows every operation. `inspect` requires one exact peer and returns bounded
registered provenance plus its current liveness observation. `activity: unobserved` is deliberate:
stored metadata is not evidence of work, acknowledgement, or completion.

`close` prompts unless `--force`. A session records itself at SessionStart, so one started before that hook
existed has no entry — `--include-unrecorded` also reports live `claude.exe`/`codex.exe` processes that own
no entry, so a running CLI is never invisible just because it predates the registry.

Liveness matches the recorded pid and its OS start time. An entry that cannot be matched is unknown, not
dead, and `close` and `close_tab.py` refuse it; an unknown identity is never killed.

## Closing the tab, not just the process

Ending the process is not closing the window. Terminal's default `closeOnExit: automatic` keeps a tab
whose process exited non-zero, and a killed one always does, so `peer-cli close` on its own leaves a dead
pane. Two things fix that, and both are here:

```sh
python3 -B <skill-directory>/scripts/configure_terminal_tab_close.py
python3 -B <skill-directory>/scripts/close_tab.py --list
python3 -B <skill-directory>/scripts/close_tab.py --title 'Postgres sweep: Search'
```

```sh
python -B <skill-directory>\scripts\configure_terminal_tab_close.py --preview
python -B <skill-directory>\scripts\close_tab.py --list
```

On Windows, the configurator edits the default Windows Terminal `settings.json` path (or an explicit
`--settings-path`), preserving JSONC layout and making a timestamped backup. Use `--preview` before a
real change; `--close-on-exit` accepts `always`, `graceful`, `automatic`, or `never`. On Linux it is a
no-op unless an explicit disposable `--settings-path` is supplied for a portable test.

`close_tab.py --list` inventories actual tabs in the detected kitty or tmux terminal, or all Windows
Terminal tabs through UI Automation; it also shows registered targets not presently discoverable. The
Konsole rows are registry-backed only because Konsole has no safe global tab inventory. Its recorded full
D-Bus service/session path is re-queried before a verified peer close.

`close_tab.py --title` operates only on exactly one actual tab. A wildcard title (`*`, `?`, or `[...]`)
always refuses unless `--all` is present, even if it happens to match one tab. Live, unrecorded, and
unknown targets refuse unless `--force`; force changes only that terminal-tab liveness decision and never
authorizes a process signal. Linux closes the exact kitty window or tmux pane, preserving siblings. Windows
rescans UI Automation at close time and never uses focus keystrokes. A stale registered peer can be closed
only by this explicit re-enumerated tab path, not by trusting its dead host process. For a stale Windows
registration without an automation ID, its title must identify exactly one registered peer and one actual
tab. Another exited registration with the same title remains ambiguous because its tab may still exist;
inspect `close_tab.py --list` and select the visible target with `--title` instead.

Its two refusals both exist because they were broken first:

- **Unknown is never dead.** Failed process inspection, malformed identity and PID reuse do not authorize
  a kill or cleanup. Force can select an explicit stale terminal target, but never bypasses pid/start-time
  verification for a process signal.

Closing this session is the current host's own close operation; this utility only closes verified peers.

## Closing a peer

Close one when it is **finished or duplicating**, and say which and why before you do:

- a handoff you launched has landed its work, so the session it ran in has nothing left to own;
- two sessions hold the same worktree or branch. Concurrent sessions in one working tree overwrite each
  other — one switching branches mid-edit is enough to lose a commit's worth of another's work.

Never close a session holding unpushed work, mid-run validation, or a question waiting on the user. Check
with `list` for its directory, and prefer authorized messaging through `agent:message` over guessing. When both
sessions are viable, the one holding the live task context is the one that stays.

Closing a CLI is not reversible. Where the user can see the tabs, name the one you mean and let them close
it, unless they have asked you to do it.

## Monitoring a peer

Read another session's progress only when your next step genuinely depends on it — whether a producer PR
merged, whether a handoff has started. It is a poor substitute for the artifacts that already record that:
a PR's checks, a plan ledger, a run log. Prefer those, and ask a peer directly only when nothing durable
answers the question.
