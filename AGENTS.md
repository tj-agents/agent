# agent

Agent owns behavior that acts on an agent session: session registration, recovery, handoff, refresh, peers,
lanes, the session store and machine installation (`install.ps1`, `install.sh`). Deterministic CLI and OS
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
using the package-local `.agents/agent/runtime` or `.agents/agent-store/runtime` path; never fall back to
an author checkout, a global cache, or a sibling installation.

<!-- BEGIN personal:rules sha256:26dbfd6ffce2eb723026177f422fa84b5e4f9a14c72fca63511bbddc5e0b1ea9 -->
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
alone leave a migration unproven; check installed bindings and fresh runtime behavior. Record any
unverified entry point as incomplete, with its resolver and observable acceptance condition.

```text
Replace old-launch with launch:
1. Migrate the registered shell command, configuration and actual callers.
2. Delete the old entry and regenerate installed settings.
3. Invoke launch from a fresh supported Windows/Linux session and check its actual behavior.
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

## 3. Work items and progress

When authorized work starts or materially changes state, resolve the installed `personal:rules` skill
and load its `resources/work-items.md`. The [work-item policy](https://github.com/tj-agents/personal/blob/main/.agents/personal/policy/rules/resources/work-items.md)
is authored once there and is the bounded import for a source item observed in a different repository;
loading it does not apply Personal's other rules to the observing task.

## 4. Plans

Track work as a GitHub issue or project item. A plan committed under `plans/` is only the detailed
implementation plan behind one such item and links to it; working plans stay machine-local.

This policy is authored once here. A selecting project receives it at SessionStart, and adoption generates
its marked native instruction block from this body and checks it for drift. A selected plugin or an
`always` tier alone is not proof of loading.
<!-- END personal:rules -->
