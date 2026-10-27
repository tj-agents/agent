# agent

Agent owns behavior that acts on an agent session: session registration, recovery, handoff, refresh, peers,
lanes, the session store and machine installation (`install.ps1`, `install.sh`). Deterministic CLI and OS
mechanics with no agent judgement, such as closing a terminal or the clipboard, belong in `tj-agents/cli`;
common behavior and the engineering and workflow process belong in `tj-agents/core`.

Published as `agent@tj-agents` from the shared `tj-agents` marketplace.

## Packages

- `agent-store`: recorded sessions and execution state.
- `agent`: host-neutral coordination, recovery, history, lanes and refresh.
- `codex`: Codex-specific hooks and memory.

The generated packages have no dependency on a Core checkout. Shared runtime is authored beneath
`.agents/agent/runtime` and `.agents/agent-store/runtime`; generated payloads embed the canonical paths
they consume. Claude-only and Codex-only runtime is authored in `.claude/runtime` and `.codex/runtime`
and ships as the `claude` and `codex` packages inside `.agents/agent/runtime`, so runtime and tests run
from the generated `plugins/agent` tree.

Maintainers refresh the offline Marketplace selection snapshot only with
`.agents/agent/utility/plugins/scripts/update_marketplace_plugins.py --source <marketplace-checkout>`;
the adjacent `marketplace_plugins.lock.json` is the canonical producer snapshot.

## Install

`./install.ps1` on Windows or `./install.sh` on Linux converges this machine: native permission defaults,
the machine event channel, the launch runtime in `~/.agents-state/bin`, packaged shell profiles, Codex
profile agents, plugin catch-up, harness permissions and plugin setup. It is idempotent; `--check` reports
drift without writing and `--step <name>` runs one step with its prerequisites.

Managed launches and refresh repair only the failing install step and retry readiness once: an
unconfigured event channel, a failed plugin update (catch-up) or missing required plugins (plugin setup).
Claude installs missing transitive dependencies before dependents and verifies their persisted native
registrations; startup still requires successful native admission and hooks.

Shell profiles load the packaged shell resources from the managed runtime in `~/.agents-state/bin`.
Neither launch nor install recovery needs a source checkout. The Windows Bash proxy is built in that
managed directory rather than modifying the repository or an installed plugin cache.

## Skills

- `agent:handoff`, `agent:lanes`, `agent:listen`, `agent:message`, `agent:open`, `agent:peer`, `agent:plugins`,
  `agent:recent`, `agent:recover`, `agent:refresh-plugins`, `agent:search`, `agent:close`
- `agent-store:inspect`
- `codex:hooks`, `codex:memory`

## Authoring and verification

```powershell
pwsh .agents/sync-generated.ps1
pwsh .agents/sync-generated.ps1 -Check
uv sync --locked
uv run pytest
```

The runtime requires Python 3.12 or newer and retains Core's portable utility conventions. Use `python`
on Windows and `python3` on Linux. CLI owns shared PATH operations in `tj_cli.paths`; the locked `tj-cli`
distribution ships inside Agent's generated runtime and managed installation. Launching does not require
a CLI source checkout or a separate package install. The development environment and CI use uv to resolve
that same dependency and run both the migrated runtime suite and packaging checks.

The layout, the vendored generator and CI come from [Template](https://github.com/tj-agents/template).

