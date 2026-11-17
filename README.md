# agent

Part of [TJ Agents](https://github.com/tj-agents), shared engineering standards and tooling for
Claude Code and Codex. Use `tj claude` or `tj codex` with your own authentication and repositories to keep
the provider's native terminal interface while adding managed completion and recovery. Agent owns behavior
that acts on an agent session: session registration, the lifecycle commands (`tj start`, `tj status`,
`tj recover`), handoff, refresh, lanes and
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

Install at least one native provider, Claude Code or Codex, and authenticate it with your own account.
Agent uses that provider's existing authentication and your normal repository access. You also need Git,
Python 3.12 or newer, uv for the work-item CLI, and the GitHub CLI for verified release downloads.
Install [TJ CLI](https://github.com/tj-agents/cli#install) and keep `tj` and `tj_hook` on `PATH`.
Named handoff and recovery tabs use Windows Terminal on Windows or tmux on Linux; a public
`tj claude` or `tj codex` launch stays in the current terminal.

Native Claude-to-Claude messaging requires Claude Code 2.1.287 or newer with Agent's
[Mods adapter](https://code.claude.com/docs/en/plugins/mods#turn-mods-on-or-off) enabled.
Check `claude --version` and the active mods in `/plugin`; reload plugins after installation.
If native delivery is unavailable or refused, TJ reports it without silently changing transports.

Clone Agent into a separate tooling directory. Run the installer from the project you intend to use, so
its selection and explicit settings supply the project context. Replace the paths below with your own.

**Windows (PowerShell)**

```powershell
git clone https://github.com/tj-agents/agent "<agent-checkout>"
Set-Location "<your-repository>"
& "<agent-checkout>/install.ps1" --check
& "<agent-checkout>/install.ps1"
```

**Linux**

```bash
git clone https://github.com/tj-agents/agent "<agent-checkout>"
cd "<your-repository>"
"<agent-checkout>/install.sh" --check
"<agent-checkout>/install.sh"
```

`--check` reports drift without applying it. Open a new terminal after installation to load the command
bindings. For an unconfigured repository, complete [repository selection](#repository-selection) below
before relying on managed completion hooks. Installing the tools does not enroll every repository.
Generated Claude and Codex hooks run through `tj_hook`; Template's generator owns that wrapping.

The installer converges this machine: the plugins of installed hosts, retired state, explicitly selected
native permission defaults,
Codex's shared daemon auto-start (a `daemon_auto_start = false` override is removed so refresh can reload open
Codex sessions in place),
the pinned `agent-store` binary, the `tj item` CLI, worktrunk (`git-wt`), Handy, the launch runtime in `~/.agents-state/bin`, the `tj` lifecycle command shims, packaged shell profiles, Codex
profile agents, provisioning a verified PowerShell runtime when no usable PowerShell is available, plugin
setup, plugin scope, Codex trust for tj-agents plugin hooks (verified against their
published source, so no Codex launch asks to trust them) and plugin copies. It is idempotent; `--check` reports
drift without writing and `--step <name>` runs one step with its prerequisites.
Plugin setup runs a selected plugin's `setup` only when its `setup --check` fails or its setup runtime differs
from the one recorded at its last successful setup in `~/.agents-state/plugin-setup.json`. That record is a
rebuildable cache: deleting it runs each selected plugin's setup once more.

`tj plugins update` uses the current machine Git configuration. It removes inherited per-command Git
configuration overrides from its own process, while preserving configuration file locations such as
`GIT_CONFIG_GLOBAL`. A failed marketplace upgrade still attempts to settle verified installed Codex hook
trust; the update remains failed and publishes no successful revision until convergence succeeds.

`tj item` is `tj-work-items`, installed with `uv tool install` (uv is required) together with the work-item
profile package each organization plugin installed on this machine declares in its own payload
(a `work-items.json` naming its package and subdirectory). Its repository comes from the marketplace
snapshot Agent already carries and its revision is the exact commit that plugin was installed from, so no
separate pin needs renewal. The tool's own pin is in `.agents/agent/runtime/work-items.json`, which Renovate
bumps; the step is a no-op while uv's receipt matches the pins. worktrunk and Handy come from the pinned
`tj-cli`'s own installers.

See [plugin updates](https://github.com/tj-agents/cli/blob/main/docs/plugin-updates.md) for update commands,
automatic checks and session reloads.
Install and a Codex session selecting Agent ensure one observer follows the existing Codex daemon while it has
loaded threads. It checks for published updates and reloads the daemon without restarting those conversations;
it exits when the daemon disconnects or has no loaded threads.
Updates check the calling repository's standards index before activation and report the explicit
repository command when it drifts. Codex also checks the adopted repositories of loaded threads from
verified installed builds before publishing.
Local-only adoption preserves owner-supplied instruction files. Failed verification retains existing
instructions and reports the update failure.

`tj install` owns plugin updates, machine setup and repair, including Handy, profiles, agent settings and
command bindings. Run from the managed runtime, it hands over to the pinned Agent build's installer, and after
updating plugins it hands over again to the Agent build that update converged to, so it always upgrades itself.
tj-agents plugins are never enabled for every project; each repository's selection enables its own.

Managed launches repair only the failing install step and retry readiness once: missing required plugins
(plugin setup).
Claude installs missing transitive dependencies before dependents and verifies their persisted native
registrations; startup still requires successful native admission and hooks.

Installer plugin setup reconciles an existing repository selection and regenerates both hosts' settings
through the same scope checks as explicit adoption. Startup only checks that configuration. The current
Marketplace `main` dependency graph adds missing dependencies and removes retired TJ Agents packages,
including retirements after the bundled offline snapshot.
An unreachable marketplace leaves repository files untouched and reports the failure.
The same reconciliation writes each selected plugin's published harness permissions into the repository's
settings, never into user settings; see [repository permissions](#repository-permissions).
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

## Repository selection

Select the generic capabilities and stacks your project needs. Personal is an optional, explicit policy
profile; generic adoption, repository ownership and a choice of configuration scope do not select it.
Provider authentication stays with the native provider, and installing Agent does not configure access to
someone else's repositories.

The shipped adoption script previews and applies selection. Use `python` on Windows or `python3` on
Linux, with the Agent checkout obtained above:

```text
python -B "<agent-checkout>/plugins/agent/.agents/agent/utility/plugins/scripts/adopt_plugins.py" --root "<your-repository>" --mode preview
python -B "<agent-checkout>/plugins/agent/.agents/agent/utility/plugins/scripts/adopt_plugins.py" --root "<your-repository>" --mode write
```

On a new selection with no requested plugins or stacks, the script selects the generic core set: Agent,
Agent Store, Base, Engineering and Workflow, plus their dependencies. Extend a selection with
`--stack python`, for example, or `--plugin <name>`. Preview the change before applying it. Custom marketplaces use
`--marketplace <name>=<owner>/<repository>` with `--plugin <plugin>@<name>`; well-formed custom owners and
repositories are supported. The reserved `tj-agents` marketplace keeps its canonical source.

`--scope project` explicitly chooses shared project configuration; `--scope local` explicitly chooses
local, Git-excluded configuration. With no scope option, a Git repository uses project scope when
`.agents/plugins.json` is already tracked and local scope otherwise. Outside Git the default is project
scope. Scope is independent of the remote owner, employer identity or profile selection. Local adoption
preserves owner-supplied instruction files; the project owner supplies their instruction routing.
To retain project scope during later default reconciliation in Git, track `.agents/plugins.json` after
the explicit project adoption.

After applying the selection, run these commands from that repository:

```text
tj install --step "plugin setup"
tj prepare --host claude
tj prepare --host codex
```

Check only the hosts you installed. `tj prepare` reports the explicit `--write` invocation when project
configuration or the standards index needs convergence. `tj install --onboard` allows a selected plugin's
own setup to perform explicitly requested enrollment; it is not the repository-selection command.

## Repository permissions

Omitting `core.permissions` in `.agents/settings.json` preserves the provider's existing permission and
approval controls. TJ adds no bypass default. An explicit `standard` or `bypass` preference is applied by
the permission setup step; choose one only when the project intends that native policy. Provider
authentication and unrelated native settings are preserved. Plugin-declared command permissions below
are separate from that default mode.

In project scope, `.claude/settings.json` (`permissions.allow` and `permissions.deny`) and
`.codex/rules/agent-harness.rules` (`allow` and `forbidden` prefix rules) carry the permissions its selected
plugins declare in `requires.permissions`, merged with its own `.agents/repository-harness.json` overlay.
Local scope uses `.claude/settings.local.json` and Git-excluded Codex configuration and harness rules.
Adoption and the `tj install` plugin setup reconciliation write them; `--check` reports drift, and a committed
rule that no selected plugin or the overlay declares any more is removed. An uncommitted Claude entry nobody
declares is refused rather than discarded. These plugin declarations do not write user settings.

```json
{"claude_allow": ["Bash(tj item:*)"], "claude_deny": ["Bash(gh auth login *)"],
 "codex_prefix_rules": [], "codex_forbidden_prefix_rules": []}
```

Each plugin's declaration is read from the `harness.json` its marketplace publishes on `main`, at the exact
source revision the marketplace catalog pins, through a blobless Git fetch. A declaration naming a
placeholder such as `${PLUGIN_ROOT}` or a machine-specific absolute path is rejected, since the repository
commits every rule literally.

Precedence: plugins combine as a union in which a plugin's deny or forbidden rule removes the identical
allow entry or Codex pattern. The overlay then extends that result and wins: an overlay entry replaces the
same Claude entry or Codex pattern from any plugin, whichever way the plugin decided it.

## Managed sessions

```text
tj claude
tj codex
tj claude --resume <session-id>
tj codex resume <session-id>
tj claude auth login
tj codex login
```

These commands preserve the provider's native interface, arguments, directory options and resume behavior.
Help, version, authentication and maintenance subcommands pass directly to the native provider. A managed
conversation starts in the current terminal, independently of any calling agent: it inherits no parent or
work item. A work item is optional. Plain `claude` and `codex` still run the provider directly, without TJ's
managed supervision and closeout receipts.

Inside a managed agent's tool process, record the current turn's disposition explicitly:

```text
tj finish
tj wait user "An answer is needed before proceeding"
tj wait child "The delegated result is pending"
tj wait ci "Required checks are running"
tj wait approval "Deployment approval is pending"
tj hold
tj hold --release
tj receipt
```

After `tj finish`, write the final response normally. The native Stop adapter captures it for that session
and turn. Intent alone does not complete the session: result, cleanup and tracker receipts are separate.
New input cancels pending completion; a keep-open hold survives it. Waits and pending background work keep
the session unfinished, and releasing a hold does not itself request completion. Missing or failed native
evidence stays visibly unverified. TJ does not generate synthetic user input or infer completion from the
wording of a response.

Use `tj receipt --launch <launch-id>` to inspect the durable record after the terminal has closed.
Cleanup preserves primary checkouts, authored changes and any worktree whose ownership or integration
cannot be proved. See [CLI's managed-session contract](https://github.com/tj-agents/cli#managed-sessions)
for the completion checks, receipt storage and recovery behavior.

For a separate named tab, follow [agent:handoff](.agents/agent/utility/handoff/SKILL.md): prepare the whole
goal and choose exactly one of `--parent <host:id>` or `--independent`. Parent identity is never inferred
from the environment. A parented launch can report blockers through `tj message` and its managed final
result routes to that exact parent. An independent launch has no inherited reporting obligation.

Handoff does not require a tracker profile. With neither `--item` nor `--create-item`, it launches unlinked
without any work-item lookup or creation, for either relationship and either host. Use `--item <ref>` to
link an existing work item, or `--create-item` to request one through the selected tracker profile using
the session title and the explicit parent's linked item as its parent when present. The two options are
mutually exclusive. Creation fields use `--item-field NAME=VALUE`, which requires `--create-item`.

Both hosts use `tj message <title|host:id> <text>`; TJ chooses the provider transport. Native submission
means queued for the provider, and a feed receipt means stored; neither proves recipient approval or
reading. The [CLI messaging contract](https://github.com/tj-agents/cli#tj-message-and-tj-feed-messaging-between-sessions)
owns route availability, retry identity and receipt meanings.

## Lifecycle

```text
tj start 461 --lane L4 [--codex]      Core worktree creator, card In Progress, agent in a managed tab
tj status [--json]                   registered sessions, native liveness, completion state and linked work
tj recover [461 | --session H]        respawn dead background sessions, resume the rest in managed tabs
tj recover --dry-run                 preview restores and stale sessions to end without changing anything
tj recover --open [461]              reopen exactly the tabs that were open whose agent is not running
tj prepare --host claude|codex       check this repository's selected plugins and standards index
tj prepare --host claude|codex --write apply selected plugin configuration and instructions explicitly
```

`tj` dispatches these to the lifecycle shims the installer writes beside it.
New worktrees use [Core's shared creator](https://github.com/tj-agents/core/blob/main/.agents/engineering/operation/open-worktree/SKILL.md)
from the enabled Base plugin. `tj start` no longer reads or seeds worktrunk configuration; a missing
creator stops the launch before tracker progress changes.
Managed launch paths share [TJ CLI](https://github.com/tj-agents/cli)'s wrapper, which records the launch
in agent-store. The SessionStart hook binds the native session to that launch, including its title and any
explicit work item or parent; a Codex thread is named after its title. Liveness comes from the hosts:
`claude agents --json`, and for Codex the threads loaded in the shared app-server daemon. Handoff and
work-item Codex launches
attach explicitly to that daemon with `--remote unix://` and carry the lane's model and reasoning effort
on the native resume command. This keeps effort overrides from selecting an embedded server.
Launch confirmation checks daemon attachment and the first recorded turn against both requested settings; a mismatch or missing
first turn fails visibly and preserves the tab for inspection. The store holds links only. Its release is pinned in
`.agents/agent/runtime/agent-store.json`, which Renovate bumps.

`tj status` lists registered sessions and their linked open work by default, including sessions without a
work item. Use `tj item mine` to list all open work assigned to you. Status shows native liveness separately
from durable completion state, including pending work, holds and failures, together with the launch and
parent relationship. Item details retain worktree, PR, CI and card information. `tj status --json` returns
an object with `sessions`, `items` and `errors`; lookup failures remain visible rather than implying there
are no sessions. Pass work-item references to filter both sessions and linked work.

`tj session liveness --session HOST:ID --launch-id ID` is read-only: it verifies the exact session/launch
binding and queries native liveness strictly, refusing unknown results instead of reporting them as dead.

Session startup records its initial linked worktree in the store's ownership collection. Engineering's
worktree creator calls `tj session claim-worktree --path PATH` after each creation, associating the
exact linked root with the calling session and its launch. Primary checkouts are never claimed.
The command returns a JSON ownership record; outside an agent session it returns `{"ownership": null}`.
Missing or inconsistent agent identities fail visibly and leave the created worktree intact.
Claude's synchronous registration hook exports its native identity through `CLAUDE_ENV_FILE` for
subsequent tool commands; Codex provides `CODEX_THREAD_ID`.

Before starting or resuming a managed conversation, the wrapper calls `tj prepare`. It checks native
project settings
and the small marked standards index in `AGENTS.md` against `.agents/plugins.json`, and installs missing
selected plugins. Startup and plugin refresh never rewrite repository instruction or configuration files;
they report the explicit `tj prepare --host … --cwd … --write` command that owns convergence. It keeps the
committed permissions as they are, so a launch needs no published harness lookup; adoption and `tj install`
own them. The index requires applicable skill bodies to be read before relevant edits;
ordinary skills remain loaded on demand. Claude imports the shared instructions through `CLAUDE.md`.
Preparation preserves authored guidance and reports a nonempty Codex override that would hide it.
Run `tj prepare --host codex --cwd PATH` directly when starting the native host without TJ supervision; add
`--write` only to explicitly apply its reported repository changes.

Claude installs a missing plugin once at user scope and disables it there unless the user already
enabled it; the repository's project or local settings enable it. Project- and local-scope installations
would shadow the user installation and miss its updates. Codex uses its native
shared package cache while enablement remains in project config; installation removes the user
activation written by `codex plugin add`. Shared cache storage does not enable a plugin everywhere.
Existing user-scoped activation must be removed when adopting this model. Native configuration in a
local-only adoption is reconciled, but its owner's instruction files remain untouched and preparation
warns that the owner must provide routing. Other preparation failures also warn; the provider still starts,
with managed completion unverified when the required lifecycle hooks are unavailable.

Recovery considers every registered session, including those without a work item. It leaves live sessions
alone and restores dead sessions whose host transcript changed after the previous boot. Older dead sessions
are marked ended; `--older` includes them again, and `--ended` includes sessions that ended cleanly.
`--open` instead reopens the newest launch of each session whose managed wrapper recorded it as open and
whose agent is not running. On Windows an ordinary tab close records it closed; logoff, shutdown or a
crash can leave it open. On Linux a tab hangup can arrive before the wrapper records the close.
Missing transcripts and directories are reported per session. On Linux, recovery uses the previous boot
in the systemd journal and opens tmux windows, creating and attaching to `tj-recover` from a plain terminal.

## Skills

- `agent:handoff`, `agent:lanes`, `agent:listen`, `agent:message`, `agent:open`, `agent:plugins`,
  `agent:recent`, `agent:recover`, `agent:search`
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

