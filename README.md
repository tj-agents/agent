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

Shared content hashing and publisher source loading live under the owner-qualified `tj_agent` package:

```python
from tj_agent import content
from tj_agent.plugins import sources
```

Host adapters and runtime delivery import these implementations. Claude registration stays with its native
plugin adapter, avoiding a second module that imports the adapter back. Generated payloads are delivery
artifacts; edit the authored source and regenerate them.

Maintainers refresh the offline Marketplace selection snapshot only with
`.agents/agent/utility/plugins/scripts/update_marketplace_plugins.py --source <marketplace-checkout>`;
the adjacent `marketplace_plugins.lock.json` is the canonical producer snapshot.

## Install

`./install.ps1` on Windows or `./install.sh` on Linux converges this machine: native permission defaults,
the machine event channel, a supervised background subscriber, the launch runtime in `~/.agents-state/bin`, packaged shell profiles, Codex
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

Shell profiles load the packaged shell resources from the managed runtime in `~/.agents-state/bin`.
Neither launch nor install recovery needs a source checkout. The Windows Bash proxy is built in that
managed directory rather than modifying the repository or an installed plugin cache.

## Permission and hook configuration

Repository preferences are defined once in `.agents/settings.json`:

```json
{
  "configuration": {
    "core": {
      "permissions": "bypass",
      "trust_hooks": true
    }
  }
}
```

`permissions` is an enum: `bypass` (the default) or `standard`. `trust_hooks` is a boolean and defaults
to `true`. Install reads the permission preference from the project where it runs; repository settings
generation reads the same declaration. The shared preference is translated independently by each host:

| Preference | Claude | Codex |
| --- | --- | --- |
| `bypass` | User `permissions.defaultMode = bypassPermissions` | `approval_policy = never`, `default_permissions = :danger-full-access` |
| `standard` | `permissions.defaultMode = default` | `approval_policy = on-request`, `default_permissions = :workspace` |

Claude configuration is authored in `.claude/runtime/permissions.py`; Codex configuration is authored
in `.codex/runtime/permissions.py`. Install and repository generation dispatch to these modules. The
PowerShell and shell install entry points invoke the same shared installer on Windows and Linux.
Claude's project settings inherit the installed user default for bypass mode because the native host
ignores a project-level bypass default. Codex's module also owns its execution rules and desktop defaults.
Close Codex Desktop before install needs to update its saved local mode.

On Codex launch and refresh, `trust_hooks: true` automatically trusts current hook hashes for enabled,
selected tj-agents plugins after verifying their installed bytes against the publisher. Disabled hooks,
managed policy and project trust keep their native controls. `false` prevents automatic trust writes;
required hooks must already be trusted manually in `/hooks` before readiness succeeds. Existing saved
trust is preserved. Claude uses its native project and plugin approval flow and has no equivalent
per-hook hash trust store for this setting to update.

## Skills

- `agent:handoff`, `agent:lanes`, `agent:listen`, `agent:message`, `agent:open`, `agent:peer`, `agent:plugins`,
  `agent:recent`, `agent:recover`, `agent:refresh-plugins`, `agent:search`, `agent:close`
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

