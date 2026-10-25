---
name: handoff
description: Open an independent unmanaged Claude Code window in an exact repository or worktree with a prepared prompt and the full native Claude Code terminal UI. Use when asked to open Claude, open another Claude instance, hand work to a second Claude, or run something in a separate Claude window.

kind: utility
domain: agent
profile: core
applicability: claude sessions
requires: python, claude
provenance: house
route: infer
---

# Claude Code handoff

The sibling of `codex:handoff`, for handing work to a second Claude Code rather than to Codex. For
opening a CLI the user drives themselves, with no prepared prompt, use `claude:open` instead. Either
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
retry on exit code 3 specifically, where a retry risks a second tab for the same handoff.

## Model selection

Configurable family aliases are rejected for handoffs; pass a full non-Haiku model ID or a supported lane.

Pass `--lane L2`–`L6` and the launcher resolves the model from the canonical lane tables it ships under
`resources/lanes` — the `agent:lanes` ladder, and the repo's only model-name owner, so no caller has
to know a model id and a retiering is one edit in one authored file. For local lanes, `--model` wins for a model the
user named outright. `--lane L1` always routes to Codex; omit that lane for an explicit Claude model. Handoff never uses the CLI default: choose L2–L6, frontier, or an explicit
non-Haiku model.

**The lane is the caller's judgement, and the launcher never guesses it** — a transport that inferred a
lane from the prompt would quietly decide the cost of every handoff. Choose the cheapest suitable rung by
design, stakes, ambiguity and verifiability, from the hardest judgement inside the delegated work — never
by how hard the work feels, file count, a handoff, or a merge, push or publish at its end, which the
delivery gates govern:

| Lane | For |
|---|---|
| `L1` | Redirects critical design decisions or large complex plans to Codex L1. |
| `L2` | High-stakes judgement that is not design, where a wrong call is costly or hard to undo. |
| `L3` | Open investigation or bounded uncertain design with unresolved alternatives. |
| `L4` | Specified implementation, ordinary review, or a small local choice inside a testable fix. |
| `L5` | Already-decided mechanical work: small file deletions or moves, config or docs cleanup, including removing or migrating `CLAUDE.local.md` once its destination or rule is decided. |
| `L6` | Bulk clerical work whose input is too large for the cheapest rung. |
| `L7` | In-session clerical work only; this launcher refuses it. |

Choose the launch lane for the work assigned to this independent session. If the successor later reaches
a different phase, it can route bounded work to an appropriate lane agent while retaining ownership.

`--frontier` selects the tier above the ladder from the same table. **Pass it only when the user
explicitly asked for that tier or its model by name.** No lane resolves to the tier, and frontier spend is
the user's provenance to grant, never a reward for a
hard-looking task — and it rejects `--lane` or `--model` beside it.

For local lanes `L2`–`L6`, effort travels with the resolved model (`--lane L4`, for example,
resolves both from the table) and reaches `claude --effort <level>` automatically. An explicit
`--model` beats one of these local lanes, so no lane effort applies; the explicit model's effort
applies only when supplied. `L1` routes to Codex and does not launch Claude.

`--effort '<low|medium|high|xhigh|max>'` is accepted only together with an explicit `--model`, for a
calling skill or workflow (or a user) that already knows the exact pace it wants for a model it named
outright — passing `--effort` alone, or beside `--lane`/`--frontier`, is rejected, because a lane or the
frontier tier already carries its own effort for whichever model it resolves, and an effort picked
without reference to a specific model is exactly the caller's-guess defect this restriction exists to
prevent. Preserve an explicit user-selected model, effort or frontier request; this flag is not a
general invitation to invent one.

A calling skill or workflow that ships its own resolved selection may still pass `--model` directly;
that wins over a local lane `L2`–`L6`. What is no longer acceptable is inventing a model id (or, per
above, an unpaired effort) at the call site.

The launcher requires `--lane L1`–`L6`, `--frontier`, or an explicit non-Haiku `--model`. L1 redirects to
Codex; it rejects L7
and every Haiku family ID or alias before launch, and never trusts the hidden CLI default.

Launch with `scripts/launch_claude.py`, beside this file:
Run it with `python` on Windows or `python3` on Linux/macOS:

```sh
# Windows
python '<skill-directory>/scripts/launch_claude.py' --working-directory '<absolute-checkout-path>' --prompt-path '<absolute-prompt-path>' --title '<short-title>' --lane '<L1..L6>'
# Linux/macOS
python3 '<skill-directory>/scripts/launch_claude.py' --working-directory '<absolute-checkout-path>' --prompt-path '<absolute-prompt-path>' --title '<short-title>' --lane '<L1..L6>'
```
On Windows, `python3` is often the Microsoft Store
alias stub rather than a real interpreter, and it fails rather than running the launcher.

Add `--lane '<L1..L6>'` (or `--frontier`) to have the launcher resolve the model and, where the table
prices one, its effort, or `--model '<model-id>'` for a model the user named outright, optionally paired
with `--effort '<level>'` for that exact model (never with `--lane` or `--frontier`; see above).

Add `--dangerously-skip-permissions` **only when the user asks for it in that request**. It disables every
permission prompt in the new window, so it is never a default and never inferred from the repository
being Tommy's own.

The launcher starts the native `claude` executable directly as the terminal tab process. Do not replace
it with the npm/NVM `claude` shim, `claude.cmd`, `claude.ps1`, `node.exe`, or an intermediate shell
command — the same launch paths that produced a degraded monochrome, non-interactive TUI for Codex.

The terminal invocation and the environment scrub belong to `scripts/agent_cli.py` under
`.agents/agent/runtime`, shared with `claude:open`, `codex:handoff`, and the Python terminal profile
launchers.

This is an unmanaged handoff. Do not invoke Agent Workboard, pass Workboard tokens, bind the session to
Workboard state, or imply that the new window will checkpoint workflow status.

Open only one Claude tab per requested handoff. After launch, report the target checkout and prompt file.

When the handoff's purpose is to verify or test something rather than to hand off a user-facing task, the
written prompt must instruct the spawned session to `SendMessage` its result back to the launching session
as its last step, before any close. An unmanaged handoff with no return channel otherwise leaves the
verification's outcome unread by anyone.
