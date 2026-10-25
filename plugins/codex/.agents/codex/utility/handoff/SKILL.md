---
name: handoff
description: Open an independent unmanaged Codex tab in an exact repository or worktree with a prepared prompt, an optional caller-chosen model and effort, and the full native Codex terminal UI. Use when asked to open Codex, hand work to it, obtain a second opinion from it, or run a prompt against a specific Codex tier.

kind: utility
domain: agent
profile: core
applicability: codex sessions
requires: python, codex
provenance: house
route: infer
---

# Codex handoff

The sibling of `claude:handoff`, for handing work to Codex rather than to a second Claude Code. Either
harness can run either launcher — both only spawn a terminal tab, so neither depends on the
harness it is invoked from. What the prompt itself must contain is the `handoff` skill's subject, not
this one's.

Write the complete handoff prompt to a UTF-8 file before launching. Never relay a substantial prompt
through nested command strings or place its contents directly in the terminal invocation.

Resolve the exact repository or worktree directory the request concerns. Do not substitute another
checkout.

It opens a tab in whichever terminal it detects it is already running inside (Windows Terminal, tmux,
kitty or Konsole). With none detected, it opens a new window instead and prints a warning — except on
Windows, where a `wt.exe` it can still find opens a tab in the most recently used Windows Terminal window
rather than a new window.

## One tab, and never a second

The launcher prints its successful handoff event only after this attempt's native SessionStart is
confirmed through the startup wrapper. A terminal accepting the command is insufficient. A CLI that exits
before confirmation returns nonzero without a successful handoff event. The tab first applies plugin updates published since the last check.
This confirms startup; reading the goal and accepting ownership remain separate pickup evidence.

Exit code **3** means startup or terminal submission remains uncertain. The error retains the attempt's
status-file path. Inspect that exact attempt before any retry; never relaunch automatically on exit 3.
Other nonzero exits report a confirmed startup failure or a wrapper that never started. A late wrapper
is blocked by the abandoned attempt's receipt.

**Never verify a launch by listing processes, and never re-run the launcher because one looked absent.**
A tab takes seconds to appear and a process listing is trivially misread — an unsorted `Select-Object
-First 3` is enough to miss the newest one. Re-running puts two agents on the same task in the same
repository, which is worse than no handoff at all: they collide on the same files with neither aware of
the other.

If the launcher printed its confirmation, one handoff tab was submitted. Accept an already authorized
prepared dispatch; do not call a higher-level workflow to prepare it again. The caller owns the checkpoint,
release and pickup rules for a full-goal transfer. For a bounded side-workstream handoff, retain the
originating goal and foreground writer lease, then continue its next action in the same turn: successful
submission is not pickup and does not satisfy the originating goal's completion condition. A launcher-only
request can finish at verified submission. If it exited non-zero, say so; do not retry blind — and never
retry on exit code 3 specifically, where a retry risks a second tab for the same request.

## Model selection

Pass `--lane L1`–`L6` and the launcher resolves the model *and its reasoning effort* from the canonical
lane tables it ships under `resources/lanes` — the `agent:lanes` ladder, and the repo's only
model-name owner, so no caller has to know a model id and a retiering is one edit in one authored file.
The pair matters here: a Codex model is priced and paced by both. An explicit `--model` or
`--reasoning-effort` still wins for whichever half the user named outright. Supply none and the CLI keeps
its own configured default, exactly as an interactively launched session would.

**The lane is the caller's judgement, and the launcher never guesses it** — a transport that inferred a
lane from the prompt would quietly decide the cost of every handoff. Choose the cheapest suitable rung by
design, stakes, ambiguity and verifiability, from the hardest judgement inside the delegated work — never
by how hard the work feels, file count, a handoff, or a merge, push or publish at its end, which the
delivery gates govern:

| Lane | For |
|---|---|
| `L1` | Critical design decisions or large complex plans with interacting decisions. |
| `L2` | High-stakes judgement that is not design, where a wrong call is costly or hard to undo. |
| `L3` | Open investigation or bounded uncertain design with unresolved alternatives. |
| `L4` | Specified implementation, ordinary review, or a small local choice inside a testable fix. |
| `L5` | Already-decided mechanical work: small file deletions or moves, config or docs cleanup, including removing or migrating `CLAUDE.local.md` once its destination or rule is decided. |
| `L6` | Bulk clerical work whose input is too large for the cheapest rung. |
| `L7` | In-session clerical work only; this launcher refuses it. |

Choose the launch lane for the work assigned to this independent session. If the successor later reaches
a different phase, it can route bounded work to an appropriate lane agent while retaining ownership.

`--frontier` selects the tier above the ladder from the same table, model and effort together. **Pass it
only when the user explicitly asked for that tier or its model by name.** No lane resolves to the tier —
L1 prices the same family an effort step below, and frontier spend is the user's provenance to grant,
never a reward for a hard-looking task — and it rejects `--lane` or `--model` beside it.
`--reasoning-effort` is the one flag it still accepts, winning over the tier's own effort, for a user who
named the pace as well as the tier.

A calling skill or workflow that ships its own resolved selection may still pass `--model` and
`--reasoning-effort` directly; that wins over `--lane`. What is no longer acceptable is inventing a model
id at the call site.

Launch with `scripts/launch_codex.py`, beside this file:
Run it with `python` on Windows or `python3` on Linux/macOS:

```sh
# Windows
python '<skill-directory>/scripts/launch_codex.py' --working-directory '<absolute-checkout-path>' --prompt-path '<absolute-prompt-path>' --title '<short-title>'
# Linux/macOS
python3 '<skill-directory>/scripts/launch_codex.py' --working-directory '<absolute-checkout-path>' --prompt-path '<absolute-prompt-path>' --title '<short-title>'
```
On Windows, `python3` is often the Microsoft Store
alias stub rather than a real interpreter, and it fails rather than running the launcher.

Add `--lane '<L1..L6>'` (or `--frontier`) to have the launcher resolve model and effort, or `--model
'<model-id>'` and `--reasoning-effort '<level>'` for values the user named. Omitting them all lets
`codex` fall back to its own configured default — the same behavior an interactively launched session
gets. The launcher rejects `--lane L7` before launch, with no terminal opened: that rung is for
in-session clerical work and cannot open a handoff.

The normal launch path verifies required hook readiness after refresh. Preserve native hook trust;
personal scope does not authorize bypassing it. A hook that cannot be verified blocks the launch with
its recovery action.

## Which executable it starts, and why the choice is not free

It starts a **native** `codex` executable directly as the terminal tab process. Do not replace it with
the npm/NVM `codex` shim, `node`, or an intermediate shell command: those launch paths have produced a
degraded monochrome/non-interactive TUI.

There is more than one native `codex` executable on a machine, and **the newest file is not the newest
build**:

- The npm package ships a vendored native binary per platform under its own `node_modules`; the thing on
  PATH is only a JavaScript entry point in front of it. The launcher takes the binary, not the entry point.
- On Windows, the desktop app additionally caches each downloaded runtime in a hash-named directory and
  *adds* rather than replaces, so that folder accumulates stale payloads indefinitely.

So the launcher enumerates every source for the current platform, asks each candidate for `--version`,
and picks the highest **version**, ranking a prerelease below the release it precedes. It replaces two
defects, not one: the old Windows-only launcher searched only the desktop cache, so the npm build was
never a candidate at all, and within that cache it ranked by file modification time — landing on a stale
alpha. Date-ranking stays wrong even once every source is searched, because a freshly downloaded alpha's
file is newer than the release it precedes.

**The model roster is gated on the CLI version.** A stale build could not offer the current roster
whatever the account was entitled to. That is why the launcher fails loudly below `--minimum-version`
(default `0.154.0`, the oldest build observed to carry the current roster) rather than quietly running an
old one. If it refuses, run `npm install -g @openai/codex@latest` — do not lower the floor to get past it.

It opens as that same tab described above, the same shared tab-opening behaviour `claude:handoff` and
`open-claude` use. Handing off several related tasks against the same repository means several tabs, not
several windows.

## The parent session's environment must not leak

A Claude Code parent exports variables that make a child render plain and behave as a managed nested
session. Two treatments are needed, not one:

Everything is **cleared**; nothing is forced. `NO_COLOR`, `TERM`, the session-binding set `CLAUDECODE`,
`CLAUDE_CODE_CHILD_SESSION`, `CLAUDE_CODE_ENTRYPOINT`, `CLAUDE_CODE_SESSION_ID`, `CLAUDE_PID`, the
`CLAUDE_CODE_MESSAGING_*` pair, and the `WORKBOARD_*` pair.

**`TERM` is cleared here and forced in `claude:handoff` on Windows, and that difference is deliberate.**
Claude Code exports `TERM=xterm-256color`, which suits a Node CLI reading `supports-color`. Codex is a
Rust/crossterm binary, and on native Windows an unset `TERM` is what selects the console's truecolor path
— handing it a POSIX terminfo name instead caps the palette at 256 colours and visibly wrecks the theme.
`COLORTERM` is not set on this machine either, so there is nothing for the leaked `TERM` to be overridden
by. Do not "align" the two launchers; the runtimes differ, so the correct handling differs. On POSIX this
clearing has no effect either way: the shared `agent_cli.py` library behind every launcher here never
forces or clears `TERM` on POSIX at all, because the terminal that actually starts the tab sets `TERM` for
that session itself, and forcing or clearing it here would fight that.

Only variables observed in a real parent session are listed. Codex's own exported session state has not
been read from one, so nothing is cleared on a guess.

This is an unmanaged handoff. Do not invoke Agent Workboard, pass Workboard tokens, bind the session to
Workboard state, or imply that Codex will checkpoint workflow status.

Open only one Codex tab per requested handoff. After launch, report the target checkout, the prompt file,
and the `codex-cli` version the launcher printed.
