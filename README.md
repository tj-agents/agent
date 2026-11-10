# agent

Part of [TJ Agents](https://github.com/tj-agents), Tommy's agentic engineering standards and tooling for
Claude Code and Codex with harness parity. Agent owns behavior that acts on an agent session: session
registration, the lifecycle commands (`tj start`, `tj status`, `tj recover`), handoff, refresh, lanes and
machine installation (`install.ps1`, `install.sh`). Deterministic CLI and OS mechanics with no agent
judgement, such as closing a terminal or the clipboard, belong in `tj-agents/cli`; common behavior and the
engineering and workflow process belong in `tj-agents/core`; the session↔work-item link store is
`tj-agents/agent-store`.

Published as `agent@tj-agents` from the shared `tj-agents` marketplace.

## Packages

- `agent-store`: the skill that reads the [agent-store](https://github.com/tj-agents/agent-store) link store.
- `agent`: the lifecycle commands, session hooks, handoff, history, lanes and refresh.
- `codex`: Codex-specific hooks and memory.

The generated packages have no dependency on a Core checkout. Shared runtime is authored beneath
`.agents/agent/runtime`; generated payloads embed the canonical paths
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

Install [TJ CLI](https://github.com/tj-agents/cli#install) first and keep `tj_hook` on `PATH`.
Generated Claude and Codex command hooks run through this native launcher. Template's generator owns
the wrapping; authored hook manifests keep the payload executable and arguments.

`./install.ps1` on Windows or `./install.sh` on Linux converges this machine: retired state, native permission defaults,
Codex's shared daemon auto-start (a `daemon_auto_start = false` override is removed so refresh can reload open
Codex sessions in place),
the pinned `agent-store` binary, the `tj item` CLI, worktrunk (`git-wt`), Handy, the launch runtime in `~/.agents-state/bin`, the `tj` lifecycle command shims, packaged shell profiles, Codex
profile agents, plugin catch-up, harness permissions, plugin setup and plugin copies. It is idempotent; `--check` reports
drift without writing and `--step <name>` runs one step with its prerequisites.

`tj item` is `tj-work-items`, installed with `uv tool install` (uv is required) together with the work-item
profile package of each employer profile plugin installed on this machine, such as `infonetica@tj-agents`.
Their revisions are pinned in `.agents/agent/runtime/work-items.json`, which Renovate bumps; the step is a
no-op while uv's receipt matches the pins. worktrunk and Handy come from the pinned `tj-cli`'s own installers.

Refresh (`.agents/agent/runtime/plugins/refresh.py`) updates only the plugins on both hosts and restarts
background sessions; install owns every machine step. The stale check refuses prompts, naming the drift, and
starts only the fix for each cause: plugins behind their pin start refresh, and each install step whose check
only reads files (agent-store CLI, tj item CLI, launch-check runtime, tj lifecycle commands, shell profiles and
plugin copies) runs that check against the installed Agent plugin, starting `install --step` for the drifted
steps alone. SessionStart, refresh and install always run this machine check; a prompt reruns it only when the
installed plugin builds or a file those checks read changed since the last clean check, so a pin refresh moves
is converged before the next prompt is admitted.

Managed launches repair only the failing install step and retry readiness once: a failed
plugin update (catch-up) or missing required plugins (plugin setup).
Claude installs missing transitive dependencies before dependents and verifies their persisted native
registrations; startup still requires successful native admission and hooks.

Before checking native installations, launch and installer plugin setup reconcile an existing
repository selection and regenerate both hosts' settings through the same scope and ownership checks
as explicit adoption. The current Marketplace `main` dependency graph adds missing dependencies and
removes retired TJ Agents packages, including retirements after the bundled offline snapshot.
An unreachable marketplace leaves repository files untouched and reports the failure.
Unconfigured projects are not enrolled. Installer `--check` reports
selection drift without changing repository files, and a converged rerun writes nothing.

Project reconciliation preserves checkout-local Codex settings when the committed and indexed
configuration is portable. It checks both Git snapshots and regenerated project declarations for
machine-specific paths. Untracked and unversioned configuration retains full path validation; the
repository path scanner still checks the complete files before publication.

Unresolved Git conflicts in the selection and generated native settings are planned through that same
transaction before native configuration is read. Plugin membership uses a three-way set merge;
generated host declarations are rebuilt from it, while unrelated settings retain their changes.
Recovery requires Git's complete unmerged index and refuses conflicting user settings or committed
conflict markers. Check mode writes nothing, and recovery leaves the index and stash intact for the
repository's normal Git resolution workflow. The regression suite creates real stash conflicts on
Windows and Linux, including paths with spaces and Unicode.

The retired machine-event watcher (a scheduled task on Windows, a systemd user unit on Linux) is removed by
install and SessionStart whenever an older cached build re-creates it. On Windows the same paths
retire `tj-watch`: its Startup shortcut, its binary in `%LOCALAPPDATA%\Programs\tj`, and a running copy, which is
stopped only when its executable path is exactly that binary.

Shell profiles load the packaged shell resources from the managed runtime in `~/.agents-state/bin`;
install rewrites the managed block. A plain `claude` or `codex` runs the native CLI.
Neither launch nor install recovery needs a source checkout. The Windows Bash proxy is built in that
managed directory rather than modifying the repository or an installed plugin cache.

Runtime self-update accepts only enabled native Agent package bytes that match the current published
Agent tree. Cache timestamps cannot select an older runtime. Launch updates include the current
project's Codex selection when locating that native package; publisher lookup failure preserves the
installed runtime and reports the update failure.

## Lifecycle

```text
tj start 461 [--codex] [--lane L4]   worktree via worktrunk, card In Progress, agent in a tj run tab
tj status [--json]                   open items with agents (live/idle/dead from the hosts), worktree, PR, CI, card
tj recover [461 | --session H]        respawn dead background sessions, resume the rest in tj run tabs
tj recover --dry-run                 preview restores and stale sessions to end without changing anything
```

`tj` dispatches these to the `tj-start`, `tj-status` and `tj-recover` shims the installer writes beside it.
Every agent tab is a Windows Terminal or tmux tab whose root process is `tj run`, from
[TJ CLI](https://github.com/tj-agents/cli). `tj run` records the launch in agent-store. The SessionStart
hook then binds the native session to that launch, so the session inherits its title, work item and parent,
and a Codex thread is named after its title. Liveness always comes from the hosts:
`claude agents --json`, and for Codex the threads loaded in the shared app-server daemon. Codex launches
attach to that daemon, so they carry no `-c` overrides; a requested reasoning effort is applied to the first
turn through the daemon. The store holds links only. Its release is pinned in
`.agents/agent/runtime/agent-store.json`, which Renovate bumps.

Recovery considers every registered session, including those without a work item. It leaves live sessions
alone and restores dead sessions whose host transcript changed after the previous boot. Older dead sessions
are marked ended; `--older` includes them again, and `--ended` includes sessions that ended cleanly.
Missing transcripts and directories are reported per session. On Linux, recovery uses the previous boot
in the systemd journal and opens tmux windows, creating and attaching to `tj-recover` from a plain terminal.

## Skills

- `agent:handoff`, `agent:lanes`, `agent:listen`, `agent:message`, `agent:open`, `agent:plugins`,
  `agent:recent`, `agent:recover`, `agent:refresh-plugins`, `agent:search`
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

The installer and managed launches make the native `tj`/`tj_hook` pair available
before native plugin operations. Missing binaries are downloaded through CLI's verified release installer,
without a source checkout or Cargo. The installation directory is prepended to the current process PATH,
including the environment inherited by the launched host. Routine launches with an existing pair do not
query releases.

The layout, the vendored generator and CI come from [Template](https://github.com/tj-agents/template).

