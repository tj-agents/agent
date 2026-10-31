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
