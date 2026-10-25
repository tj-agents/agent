---
name: lanes
description: Pick the model and thinking effort a piece of work is worth by declaring an ordinal lane (L1 most capable to L7 cheapest) that each harness resolves to its own model, so no skill, agent or workflow stage ever names a model. Covers the ladder and what each rung is for, the four questions that choose a rung, why a merge or publish never raises a lane, re-routing per phase rather than running a whole task in its highest lane, where a lane is applied (skill front matter, a delegated lane agent, a workflow stage), and the three places the Claude and Codex ladders are deliberately not level. Use when adding or reviewing a skill or agent, when clerical work is running on an expensive model, when tempted to write a model name anywhere, or when a new model needs pricing into the ladder.

kind: policy
domain: agent
profile: core
applicability: agent sessions
requires: native host
provenance: house
---

# Lanes

**A lane is what work is worth, not which model runs it.** Declare `lane: L5`; never write a model name.
Two tables — `claude.json` and `codex.json` — are the only places a model name may appear, so a new model
or a reprice is a one-file change per harness and every consumer inherits it. They live at `../../../lanes/` from this canonical source definition, at
`../../resources/lanes/` from an installed host entry, and at
`../../../../resources/lanes/` from the packaged canonical definition, alongside `resolve.py`.

## The ladder

L1 is the most capable rung and L7 the cheapest. The same `LN` means the same *relative* capability on
both harnesses, which is the whole point: one declaration, two resolutions.
Relative capability does not require the same exact model family: paired rungs have the same role and price
ordering even when a harness's available families differ.

| Lane | For | Resolves to |
|---|---|---|
| **L1** | a critical design decision, or a big complex plan with substantial interacting decisions; never implementation, review or delivery | the highest appropriate design capability |
| **L2** | high-stakes judgement that designs nothing — a diagnosis of a production failure, a security or migration review | top general family, the setting above its default |
| **L3** | open investigation or bounded uncertain design with unresolved alternatives | top general family, default setting |
| **L4** | specified implementation, ordinary review, and small local choices inside a testable fix | the workhorse family, high setting |
| **L5** | mechanical work whose shape is already decided — including small file deletions or moves, config or docs cleanup, and removing or migrating `CLAUDE.local.md` once its destination or rule is decided | a cheaper family or reduced workhorse setting |
| **L6** | bulk clerical work whose input outgrows the cheapest rung — a monitor loop, a long transcript, a big diff | the cheapest model that still holds the input |
| **L7** | clerical work with a small input — commit, push, pull, sync, one poll | the cheapest usable model |

**This document deliberately names no model.** Which model each rung resolves to is data, in the two
tables, and a rule stated in prose beside the data is a second copy waiting to disagree with it. Run
`resolve.py --lanes` from whichever of the two locations above exists here for the current pairings.

**Only a leaf skill that IS one shape of work may declare a lane.** A standard or supporting contract —
anything loaded *during* other work to be consulted — must not, because its lane would re-point the model
of whatever task loaded it: a migration would drop to the docs rung for having read the persistence
standard. A lifecycle or orchestrating skill that spans phases of varying shape (`feature`, `review`,
`merge`) must not either: invoking one re-points the whole session, so it inherits the session's model and
routes each bounded phase down the ladder instead. The generator refuses a lane on a routed standard for
exactly this reason; the test for the rest is whether an agent *runs* one unvarying shape of work.
`commit` takes a lane; `committing`, `feature`, and `workflow:plan` do not. Planning does not itself
select a lane.

**No declaration means inherit the session's model** — not a default rung. A lane is opt-in per skill, so
adding one is a visible, reviewable decision and no skill silently changes model because a default moved.
Declare the cheapest suitable lane explicitly rather than relying on absence: use L4 only for specified
implementation that still needs code-level judgement, and L5 for small already-decided mechanical work.

```text
Task: Add already-designed working-directory validation to a launcher and its focused tests.
Lane: L4, for specified implementation with local testable choices.
Dispatch: A bounded implementation agent; the parent retains the whole goal.
```

## Choosing a rung — four questions, in this order

1. **Critical design or scale.** Is this a critical design decision, or a big complex plan with substantial
   interacting decisions? Record one concrete criticality or scale reason before selecting L1. A plan,
   architecture, data model, contract, or mechanism label alone does not select it. Bounded uncertain
   design is L3; small local choices inside a specified, testable fix are L4.
2. **Stakes.** For work that designs nothing: how costly, and how hard to undo, is a wrong call? A
   diagnosis or review with a lot riding on it — production, security, stored data — is L2.
3. **Ambiguity.** Is the answer known and this is typing, or is the problem open? Open is never below L3;
   specified implementation that still needs code-level judgement is L4; small already-decided file
   deletion or move, config or docs cleanup is L5. File count, a handoff, or a merge does not raise it.
4. **Verifiability.** What catches the mistake — a compiler, a test suite, or only human judgement? Use
   verification to choose the cheapest rung that still supplies the required judgement; automated checks do
   not remove unresolved design or diagnosis.

**Delivery never raises a lane.** Take the rung from the hardest judgement inside the delegated work,
never from the operations at its end. Nearly every change ends in a merge, push, publish or release, so
reading that as irreversibility would put almost every job on L1. Those steps are governed by the delivery
gates — review, CI, the merge queue, release checks — never by model tier. Implementation, review and
delivery never select L1; a high-stakes review may select L2 under the stakes question.

Nothing here says "how hard does this feel". A long mechanical edit is still L5; a one-paragraph plan that
picks a mechanism is L3 or L4 according to its uncertainty and verification. One extra axis applies at the floor alone: clerical work is L7 only while
its input fits the cheapest rung, and an input that outgrows it selects L6.

**The frontier tier is above the ladder and is not a rung.** Each table carries a `frontier` entry the
four questions can never select: it is priced by provenance — the user explicitly asking for that tier or
its model by name — not by task shape. A test asserts no rung resolves to the frontier's exact
model-and-effort pair, so lane inflation cannot reach the tier. Consumers that expose it
(the handoff launchers' `-Frontier`) make selecting it a distinct visible act rather than one lane value
among others.

**Reconsider substantive work.** Load this contract once per logical task and reuse it while its source
identity is unchanged. Select suitable capability for substantive investigation, design, implementation,
or review; reconsider when the required judgment changes. Routine work needs no written lane rationale.
Commits, pushes, PR administration, and polls still use their declared cheap leaf lane when supported.
Batch clerical work and monitoring loops into a bounded cheap worker when worthwhile; one tiny command
may stay inline when switching would cost more. Neither route needs another skill read or handoff.

Lane selection never transfers task ownership. On either host the parent does its own work in the same
checkout and may give a bounded implementation or review task to a lane agent.
Give that agent explicit path and responsibility ownership, avoid overlapping writers, and reconcile its
result in the parent. A small follow-up can stay with the parent. If delegation is unavailable, use the
current owner's supported fallback; do not open an independent session solely to change model or cost.
Prefer a supported in-session control or a bounded worker when changing capability saves meaningful work.
An unavailable cheaper setting does not block an owner capable of safely completing the task. Continue
with that owner and record a material fallback once; never claim a model switch occurred merely because
a lane was recorded. Missing capability required for correctness remains a real gate. L1 remains reserved
for critical design; delegate substantial implementation or review rather than running it at L1.

## Where a lane is applied

- **A skill** declares `lane:` in front matter. The generator resolves it and stamps the harness's own
  keys into each generated payload: `model:` plus `effort:` for Claude, `model:` alone for Codex —
  **Codex has no per-skill effort key**, so a Codex skill gets the rung's model at whatever effort the
  session is on. Where that half matters, use a delegated lane agent, which is the only place Codex can
  carry `model_reasoning_effort`. Invoking the skill re-points the session and the switch outlives it,
  which is why only a terminal leaf task declares one.
- **A delegated lane agent** (`lane-l5`, `lane-l7`) is the alternative for cheap work that runs many
  turns. Prompt caches are model-scoped and a mid-conversation effort change invalidates the message
  cache, so re-pointing the main loop for a single clerical turn can cost more than it saves, while a
  delegated agent carries only its own small prompt and leaves the parent's cache intact. Reach for it
  when the cheap work is a loop — a monitor, a bulk pass — not a one-shot; `lane-l6` exists for exactly
  the loop whose input outgrows `lane-l7`.
- **A workflow stage** keeps its own pinned model in `.agents/workflows/hosts/*.json`, because those pins
  live under the workflow contract's version rather than being re-resolved at dispatch. They are derived
  from the ladder and a test asserts they still agree with it, so a rung and its stage cannot drift apart
  — but the pin is the value the runtime reads.
- **A handoff launcher** takes a lane (or frontier) flag per dispatch: both `claude:handoff` and
  `codex:handoff`'s Python launchers take `--lane` (or `--frontier`), and `codex:handoff` also
  takes `--reasoning-effort` for a Codex model named outright. Either way the calling agent judges the
  lane by the four questions and the launcher only prices it, never inferring one from the prompt.

L7 is in-session clerical work only. Both handoff launchers reject it before opening a tab. Claude handoff
also requires an explicit L1–L6 lane, frontier tier, or non-Haiku model, because an unchecked CLI default
could otherwise select Haiku.

## Where the two ladders are not level — know these before trusting parity

1. **L7 is a capability step on Claude, not just a price step.** Its model rejects `effort` outright and
   its context window is a fifth of every rung above it, so a clerical skill that reads a large diff or a
   long transcript fails on L7 where L6 would not. The Claude table records that ceiling; the Codex one
   records `null`, meaning *unrecorded*, not unlimited.
2. **The floor is a different kind of drop on each harness.** Claude's L7 drops to a small-window model;
   Codex's L7 is the small family at its lowest setting, with no recorded ceiling. Work that genuinely
   needs a big-window floor has L6, which keeps Claude on the workhorse with its full context and Codex at
   higher effort in the small family.
3. **A specialist model is a second axis, not a rung.** Codex's review stage names its own review-tuned
   model with a fallback — chosen for the *kind* of work, not its capability rank. Such a choice is
   declared as a `specialist` on the stage, never as its own lane, and it then owns its effort too;
   folding it into the ladder would imply a capability ordering that is not what it means.

## Anti-patterns

- **A model name outside the lane tables.** In a skill, an agent, a workflow script, a hook, a settings
  file: every one of those is a place that has to be found again at the next model release.
- **A lane picked by how the work feels** rather than by the four questions. That is how every rung drifts
  to L1 and the ladder stops meaning anything.
- **A lane raised by delivery.** "It ends in a merge, so it is irreversible" is the same drift; the
  delivery gates own that risk.
- **An alias where an ID belongs.** The tables carry exact model IDs because `ANTHROPIC_DEFAULT_*_MODEL`
  re-points a family alias — an alias can silently resolve to a different, dearer model than intended.
- **A new rung for a model that is merely different.** Rungs are ordinal. A model that is cheaper *and*
  weaker is a rung; a model that is sideways is a specialist.
