<!-- BEGIN tj-agents:standards -->
## Repository standards

Before the first edit, read the full standards relevant to your changes
from the index below. Invoke the skill or read its advertised
SKILL.md and its canonical definition. A skill listing is not a read.
Load only the applicable standards and their needed references; reassess when
the task changes. Report any missing required skill and repair this repo's
plugin installation before editing the files it governs.

Selected plugins: `agent-store@tj-agents`, `agent@tj-agents`, `base@tj-agents`, `cli@tj-agents`, `engineering@tj-agents`, `github-actions@tj-agents`, `personal@tj-agents`, `python@tj-agents`, `skill@tj-agents`, `template@tj-agents`, `workflow@tj-agents`.

- GitHub Actions projects: `github-actions:build`, `github-actions:domain-design`, `github-actions:errors`, `github-actions:explanation`, `github-actions:libraries`, `github-actions:security`, `github-actions:structure`, `github-actions:style`, `github-actions:testing`
- Python projects: `python:build`, `python:dispatch`, `python:domain-design`, `python:enums`, `python:errors`, `python:explanation`, `python:imports`, `python:libraries`, `python:modules`, `python:structure`, `python:style`, `python:testing`
- When relevant to this task: `base:agent-files`, `base:project-setup`, `base:solution-assessment`, `base:task-routing`, `engineering:branching`, `engineering:committing`, `engineering:docs`, `engineering:failure-provenance`, `engineering:git-auth`, `engineering:guide`, `engineering:merging`, `engineering:plans`, `engineering:pr-screenshots`, `engineering:remote-validation`, `engineering:review-lifecycle`, `engineering:session`, `workflow:plan-artifacts`, `workflow:session`
- agent sessions: `agent:lanes`
- material Tommy revises or rehearses: `personal:revision`
- projects explicitly selecting personal: `personal:rules`

<!-- END tj-agents:standards -->

# agent

Agent owns behavior that acts on an agent session: session registration, the lifecycle commands, handoff,
refresh, lanes and machine installation (`install.ps1`, `install.sh`). Deterministic CLI and OS
mechanics with no agent judgement, such as closing a terminal or the clipboard, belong in `tj-agents/cli`;
common behavior and the engineering and workflow process belong in `tj-agents/core`.

This repository uses the plugin layout `template:check` documents and enforces
(https://github.com/tj-agents/template). Authored skills live once under `.agents/<plugin>/<kind>/<name>/SKILL.md`,
with each skill's scripts and templates beside it; related skills share a family folder, family first
(`state/client/` publishes `state-client`). `.agents/sync_generated.py`, `.agents/sync-generated.ps1`,
`.gitattributes`, `.gitignore`, `CLAUDE.md` and `.github/workflows/ci.yml` are vendored from Template's main branch.
Change them in Template, then run `template:update` to refresh the exact vendored content.

`.codex/skills/`, `.claude/skills/`, `.agents/INDEX.md`, both marketplace files and `plugins/*` are generated. Run
`pwsh .agents/sync-generated.ps1` after authored changes and require `pwsh .agents/sync-generated.ps1 -Check`
before delivery.

Runtime scripts use Python's standard library on Windows and Linux, retaining the source utility
conventions in [Core's CODE_CONVENTIONS.md](https://github.com/tj-agents/core/blob/main/CODE_CONVENTIONS.md).
Use `uv sync --locked` and `uv run pytest` for the complete development suite. Shared CLI library code
ships through declared distribution resources; installation and managed launches require Python 3.12+.
Keep native application packaging and dependencies out of this plugin publisher unless the work needs them.
`.agents/agent/runtime` stays host-neutral: anything naming one host's tools, settings, rule syntax or paths
lives in `.claude/runtime/` or `.codex/runtime/` and is reached through `Host` or the `claude`/`codex` packages.

```powershell
pwsh .agents/sync-generated.ps1
pwsh .agents/sync-generated.ps1 -Check
```

For new skills load `skill:new`; for edits, splitting, renaming or consolidation load `skill:update`.
Both require the current host's native creator and a complete cutover without retired names or aliases.
The project selects Template in `.agents/plugins.json`; after Git initialization, use Core's
`agent:plugins` to generate both hosts and complete native admission and installation.

Follow `engineering:docs` when adding or changing a skill or standard. Keep a convention's required `Owns …`
opening, then use a concrete fenced example before additional explanatory prose to settle ambiguous terminology. A
starter's explicit unfinished placeholder is not an example, so replace it with real behaviour before committing
because the guidance-example check rejects it.

This profile applies in projects that explicitly select it. Policies live under `policy/`; supporting conventions, workflows and utilities stay beside their owning skills. Declare mandatory host hooks in the shipped harness manifest and verify their delivery on each supported host.

Agent Store is the leaf package. Agent may depend on Base, Agent Store and CLI; Codex may depend on
Agent. Keep this graph acyclic. Resolve packaged runtime only from the nearest repository or plugin root,
using the package-local `.agents/agent/runtime` path, and reach the link store only through the pinned
`agent-store` CLI; never fall back to
an author checkout, a global cache, or a sibling installation.

<!-- BEGIN personal:rules sha256:c4f6a57dde7e906bd13bad312f0042408f0c01ce0c9855968d7b7fa3907b5dc8 -->
# Personal rules

These rules govern every project that explicitly adopts `personal@tj-agents`. Adoption records the
project's personal scope; a home directory, author name or employer association never establishes it, and
employer projects stay outside this profile unless the user explicitly extends that scope. Before planning
or starting work, confirm the task is clear against each rule below.

## 1. Clean cut

Keep one current supported solution. When replacing behavior, remove the superseded names, aliases,
fallback implementations, adapters, configuration formats, compatibility branches and forwarding or
migration docs. Migrate all known callers, project declarations, installed registrations, generated
artifacts and live runtime entry points in the same cutover, and coordinate upgrades with the actual
collaborating developers. Run each migration once during the cutover and delete it; never ship migration
or cleanup code that runs again later. CI fails a commit that removes or renames a skill while a tracked source
still names its old `<package>:<name>`.

Verify the entry points people actually use on every supported platform, including Windows and Linux
launch paths where supported. Source edits, successful generation, passing unit tests and version commands
alone leave a migration unproven; check installed bindings and runtime behavior after `tj install` and a
plugin reload in the current session, or against a fixture or dummy host process. Never launch another agent
session, tab or handoff to verify unless Tommy asks for it. Record any unverified entry point as incomplete,
with its resolver and observable acceptance condition.

```text
Replace old-launch with launch:
1. Migrate the registered shell command, configuration and actual callers.
2. Delete the old entry and regenerate installed settings.
3. Run tj install, reload plugins in this session, run launch against a dummy host and check its behavior.
Result: launch --version alone leaves this migration incomplete.
```

A backwards-compatibility exception exists only as an explicit user requirement. Surface it as a
deliberate policy decision, identify the affected supported behavior and reconcile this profile's scope
before implementing it, keeping the exception visible rather than inside a fallback or adapter.

## 2. Proportionate implementation

Solve the demonstrated problem with the smallest suitable implementation. Add abstractions, dependencies,
configuration switches, duplicate paths or process machinery only for a concrete current need. Prefer the
capabilities the project already selects. Verify the behavior and meaningful failure paths in proportion
to the change, with tests that prove behavior rather than restate the implementation.

## 3. Established problem records

Owns recording of established personal bugs, agent or process mistakes, and deliberate debt. Record one
through Personal's selected tools only when Tommy asks for it; otherwise mention the problem in your report
and carry on with the active task. Never record employer or non-personal evidence through Personal.

Reconcile the canonical existing identity before recording. For a repeated observation, append material
new recurrence evidence, the cause when known, and the current outcome or repair state; do not create a
duplicate item. Verify remote persistence before claiming the problem is documented. The existing Personal
tools own their commands and procedures.

Recording does not authorize, prioritize or dispatch implementation. Apply
[Workflow's incidental-problems decision](https://github.com/tj-agents/core/blob/main/.agents/workflow/resources/problem-records.md#incidental-problems)
to those decisions and preserve the active task and repair owner.

```text
Tommy asks to record a repeated native close prepare failure seen during item A:
1. Update its existing close issue with new recurrence evidence, the known cause and current repair state.
2. Verify the update persists remotely before saying it is documented.
3. Continue item A; do not create a duplicate item or take over the repair.
```

## 4. Plans

Track work as a GitHub issue or project item. A plan committed under `plans/` is only the detailed
implementation plan behind one such item and links to it; working plans stay machine-local.

## 5. Delivery

A coherent goal Tommy authorizes ships as one PR per repository, each containing the complete requested
result. Split a repository's work into several PRs or a stack only when Tommy asks for staged delivery, such
as a large refactor he wants landed in stages. Personal projects have no outside users, so a brief break
between repositories while their PRs land is acceptable.

Extra review rounds, continuation or progress records, additional work items, status messages to other
sessions and further planning are opt-in: do them when Tommy asks or the work demonstrably needs them, and
say which. A selected standard that prescribes slicing, stacking, recorded reviews, ledgers or completion
records does not bind a personal project unless Tommy asks for it. Required CI and a configured approval
gate still apply.

A handoff, plan or worker brief carries the whole requested outcome. Narrowing it to a smaller patch, or
reporting documentation of unfinished work as delivery, is a defect: finish the independent parts and name
the exact decision or dependency that blocks the rest.

```text
Goal: one tj install, plugin scoping and an approval switch across agent, cli and github.
Delivery: three PRs, one per repository, each complete; no stack, plan PR, review receipt or ledger.
Staging: only after Tommy asks for the refactor to land in stages.
```

This policy is authored once here. A selecting project receives it only through the marked AGENTS.md block
adoption generates from this body; rerun adoption in every adopting project when the body changes. A selected plugin or an
`always` tier alone is not proof of loading.
<!-- END personal:rules -->
