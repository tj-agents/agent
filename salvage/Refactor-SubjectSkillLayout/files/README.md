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

Canonical skills live under `.agents/<namespace>/skills/<subject>/SKILL.md`. Public skill names stay
`<namespace>:<subject>`; the `kind` metadata describes their responsibility. `agent:lanes` and
`agent:close` are policies; the other skills return bounded operation results.

Shared Python code uses two source distributions: `packages/tj-agent/src/tj_agent` owns coordination
and host integrations, and `packages/tj-agent-store/src/tj_agent_store` owns the independent Store.
Agent consumes CLI process and terminal mechanics through the pinned `tj-cli` distribution.
Generated plugin packages carry their declared dependencies under `python/`; skill and hook scripts
resolve that package boundary before importing their implementation.

Shared content hashing and publisher source loading live under the owner-qualified `tj_agent` package:

```python
from tj_agent import content
from tj_agent.plugins import sources
```

Host adapters and runtime delivery import these implementations. Claude registration stays with its native
plugin adapter, avoiding a second module that imports the adapter back. Generated payloads are delivery
artifacts; edit the authored source and regenerate them.

Maintainers refresh the offline Marketplace selection snapshot only with
`.agents/agent/skills/plugins/scripts/update_marketplace_plugins.py --source <marketplace-checkout>`;
`packages/tj-agent/src/tj_agent/plugins/marketplace_plugins.lock.json` is the canonical producer snapshot.

## Install

`./install.ps1` on Windows or `./install.sh` on Linux converges this machine: native permission defaults,
the machine event channel, a supervised background subscriber, the Python bundle in `~/.agents-state/agent/python`, command shims in `~/.agents-state/bin`, packaged shell profiles, Codex
profile agents, plugin catch-up, harness permissions and plugin setup. It is idempotent; `--check` reports
drift without writing and `--step <name>` runs one step with its prerequisites.

Managed launches and refresh repair only the failing install step and retry readiness once: an
unconfigured event channel, a failed plugin update (catch-up) or missing required plugins (plugin setup).
Claude installs missing transitive dependencies before dependents and verifies their persisted native
registrations; startup still requires successful native admission and hooks.

The subscriber runs as a hidden current-user scheduled task on Windows or a systemd user service on
Linux. It reads the existing event channel, verifies repository hints against GitHub, and updates every
registered repository while sessions remain open. Repository registrations share a Git common directory
across linked worktrees and retain project/local scope. Successful revisions are recorded once; failed
updates retry without acknowledging the event. After a long disconnection, an authoritative catch-up
restores convergence. Managed launches install the subscriber on existing machines after the runtime
updates; unavailable supervision leaves foreground update checks working and retries setup later.

Shell profiles load package resources from the managed Python bundle in `~/.agents-state/agent/python`.
Neither launch nor install recovery needs a source checkout. The Windows Bash proxy is built under `~/.agents-state/bin` rather than modifying the repository or an installed plugin cache.

## Skills

- `agent:handoff`, `agent:lanes`, `agent:listen`, `agent:message`, `agent:open`, `agent:peer`, `agent:plugins`,
  `agent:recent`, `agent:recover`, `agent:refresh-plugins`, `agent:search`, `agent:close`
- `agent-store:inspect`
- `codex:hooks`, `codex:memory`

## Authoring and verification

```powershell
pwsh .agents/sync-generated.ps1
pwsh .agents/sync-generated.ps1 -Check
uv run --locked python -B -m unittest discover -s .agents/tests -p "test_*.py"
```

The current implementation uses the standard library and retains Core's portable source conventions.
Use `python` on Windows and `python3` on Linux, preserving each entry point's interpreter requirement.
The shared plugin CI discovers the runtime suite through `.agents/tests`. Python implementation layout
and package boundaries follow `python:structure` and `python:modules`.

`template:structure` owns the plugin source layout. The vendored generator, checks and CI come from
[Template](https://github.com/tj-agents/template); Core's `SKILL_KINDS.md` owns kind meanings.

