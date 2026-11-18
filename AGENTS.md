# agent

Agent owns behavior that acts on an agent session: session registration, recovery, handoff, refresh, peers,
lanes, the session store and machine installation (`install.ps1`, `install.sh`). Deterministic CLI and OS
mechanics with no agent judgement, such as closing a terminal or the clipboard, belong in `tj-agents/cli`;
common behavior and the engineering and workflow process belong in `tj-agents/core`.

This repository follows the plugin source layout owned by `template:structure` and enforced by
`template:check` (https://github.com/tj-agents/template). Authored skills live once under
`.agents/<namespace>/skills/<subject>/SKILL.md`, with each skill's scripts and templates beside it.
The public identity is `<namespace>:<subject>`; `kind` is independent metadata. Core's `SKILL_KINDS.md`
owns the shared classification vocabulary. `.agents/sync_generated.py`, `.agents/sync-generated.ps1`,
`.gitattributes`, `.gitignore`, `CLAUDE.md` and `.github/workflows/ci.yml` are vendored from Template's main branch.
Change them in Template, then run `template:update` to refresh the exact vendored content.

`.codex/skills/`, `.claude/skills/`, `.agents/INDEX.md`, both marketplace files and `plugins/*` are generated. Run
`pwsh .agents/sync-generated.ps1` after authored changes and require `pwsh .agents/sync-generated.ps1 -Check`
before delivery.

Shared Python implementations live in `packages/tj-agent/src/tj_agent` and
`packages/tj-agent-store/src/tj_agent_store`. Host integrations belong under
`tj_agent.hosts.claude` and `tj_agent.hosts.codex`; CLI mechanics come from the pinned `tj-cli`
distribution. Skill and hook scripts are thin entry adapters. Follow `python:structure` and
`python:modules`; preserve the declared Python3.9 operation floor and standard-library dependencies.
Use the root uv workspace for development and locked dependency resolution. Run
`uv run --locked python -B -m unittest discover -s .agents/tests -p "test_*.py"` on both platforms.

```powershell
pwsh .agents/sync-generated.ps1
pwsh .agents/sync-generated.ps1 -Check
```

For new skills load `skill:new`; for edits, splitting, renaming or consolidation load `skill:update`.
Both require the current host's native creator and a complete cutover without retired names or aliases.
The project selects Template in `.agents/plugins.json`; after Git initialization, use Agent's
`agent:plugins` to generate both hosts and complete native admission and installation.

Follow `engineering:docs` and `skill:check` when adding or changing a skill. A standard opens with
`Owns …` after its title; each changed skill includes a meaningful fenced example. A
starter's explicit unfinished placeholder is not an example, so replace it with real behaviour before committing
because the guidance-example check rejects it.

This profile applies in projects that explicitly select it. Preserve each skill's applicability, prerequisites
and authority when changing its kind. Declare mandatory host hooks in the shipped harness manifest and
verify their delivery on each supported host.

Agent Store is the leaf package. Agent may depend on Base, Agent Store and CLI; Codex may depend on
Agent. Keep this graph acyclic. Consume implementation and resources through declared source or delivered
package boundaries. A source layout example is not a runtime discovery API. Installed entry points must
use their own delivered implementation without searching an author checkout, a global cache or a sibling
installation.
