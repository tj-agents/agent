# agent

This repository uses the plugin layout `template:check` documents and enforces
(https://github.com/tj-agents/template). Authored skills live once under `.agents/<plugin>/<kind>/<name>/SKILL.md`,
with each skill's scripts and templates beside it; related skills share a family folder, family first
(`state/client/` publishes `state-client`). `.agents/sync_generated.py`, `.agents/sync-generated.ps1`,
`.gitattributes`, `.gitignore`, `CLAUDE.md` and `.github/workflows/ci.yml` are vendored from Template's main branch.
Change them in Template, then run `template:update` to refresh the exact vendored content.

`.codex/skills/`, `.claude/skills/`, `.agents/INDEX.md`, both marketplace files and `plugins/*` are generated. Run
`pwsh .agents/sync-generated.ps1` after authored changes and require `pwsh .agents/sync-generated.ps1 -Check`
before delivery.

```powershell
pwsh .agents/sync-generated.ps1
pwsh .agents/sync-generated.ps1 -Check
```

For skill creation, splitting, renaming or consolidation, load `template:new-skill` before the native creator.
The project selects Template in `.agents/plugins.json`; after Git initialization, use Core's
`machine:plugin-selection` to generate both hosts and complete native admission and installation.

Follow `engineering:docs` when adding or changing a skill or standard. Keep a convention's required `Owns …`
opening, then use a concrete fenced example before additional explanatory prose to settle ambiguous terminology. A
starter's explicit unfinished placeholder is not an example, so replace it with real behaviour before committing
because the guidance-example check rejects it.

This profile applies in projects that explicitly select it. Policies live under `policy/`; supporting conventions, workflows and utilities stay beside their owning skills. Declare mandatory host hooks in the shipped harness manifest and verify their delivery on each supported host.

Agent Store is the leaf package. Agent may depend on Base and Agent Store; Codex and Claude may depend on
Agent. Keep this graph acyclic. Resolve packaged runtime only from the nearest repository or plugin root,
using the package-local `.agents/agent/runtime` or `.agents/agent-store/runtime` path; never fall back to
an author checkout, a global cache, or a sibling installation.
