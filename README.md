# agent

Reusable agent sessions, coordination and native host operations.

Published as `agent@tj-agents` from the shared `tj-agents` marketplace.

## Packages

- `agent-store`: recorded sessions and execution state.
- `agent`: host-neutral coordination, recovery, history, lanes and refresh.
- `codex`: Codex-specific hooks and memory.

The generated packages have no dependency on a Core checkout. Shared runtime is authored beneath
`.agents/agent/runtime` and `.agents/agent-store/runtime`; generated payloads embed the canonical paths
they consume.

Managed launches and refresh recover unconfigured setup or failed synchronization by fetching canonical
`tj-agents/core` main into `~/.agents-state/sources/core`, running its supported setup entrypoint and
`--check`, and retrying readiness once. Recovery preserves credential enrollment controls and needs no
pre-existing Core checkout. Claude installs missing transitive dependencies before dependents and verifies
their persisted native registrations; startup still requires successful native admission and hooks.

Maintainers refresh the offline Marketplace selection snapshot only with
`.agents/agent/utility/plugins/scripts/update_marketplace_catalog.py --source <marketplace-checkout>`;
the adjacent `marketplace_catalog.lock.json` is the canonical producer snapshot.

## Skills

- `agent:clip`, `agent:handoff`, `agent:lanes`, `agent:listen`, `agent:message`, `agent:open`, `agent:peer`, `agent:plugins`,
  `agent:recent`, `agent:recover`, `agent:refresh`, `agent:search`, `agent:close`
- `agent-store:inspect`
- `codex:hooks`, `codex:memory`

## Authoring and verification

```powershell
pwsh .agents/sync-generated.ps1
pwsh .agents/sync-generated.ps1 -Check
python -B -m unittest discover -s .agents/tests -p "test_*.py"
```

The runtime uses the standard library and retains Core's portable utility conventions. Use `python`
on Windows and `python3` on Linux. The shared plugin CI discovers the migrated runtime suite through
`.agents/tests`; generation is tooling, and these utilities do not introduce an application package or
third-party runtime dependencies.

The layout, the vendored generator and CI come from [Template](https://github.com/tj-agents/template).

