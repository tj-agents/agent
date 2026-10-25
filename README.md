# agent

Reusable agent sessions, coordination and native host operations.

Published as `agent@tj-agents` from the shared `tj-agents` marketplace.

## Packages

- `agent-store`: recorded sessions and execution state.
- `agent`: host-neutral coordination, recovery, history, lanes and refresh.
- `codex`: Codex-specific handoff, opening, hooks and memory.
- `claude`: Claude-specific handoff and opening.

The generated packages have no dependency on a Core checkout. Shared runtime is authored beneath
`.agents/agent/runtime` and `.agents/agent-store/runtime`; generated payloads embed the canonical paths
they consume.

Maintainers refresh the offline Marketplace selection snapshot only with
`.agents/agent/utility/plugins/scripts/update_marketplace_catalog.py --source <marketplace-checkout>`;
the adjacent `marketplace_catalog.lock.json` is the canonical producer snapshot.

## Skills

- `agent:lanes`, `agent:listen`, `agent:message`, `agent:open`, `agent:peer`, `agent:plugins`,
  `agent:recent`, `agent:recover`, `agent:refresh`, `agent:search`, `agent:close`
- `agent-store:inspect`
- `codex:handoff`, `codex:hooks`, `codex:memory`, `codex:open`
- `cc:handoff`, `cc:open`

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

