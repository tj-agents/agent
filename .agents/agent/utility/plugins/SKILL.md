---
name: plugins
description: Refresh this machine's plugins with tj plugins update, install the machine with tj install, or select a repository's plugins and generate its Claude and Codex settings. Use when plugins look outdated, a plugin PR merged, setting up a machine, adopting a stack, changing selected packages, or auditing machine plugin settings.

kind: utility
domain: agent
profile: core
applicability: repositories adopting plugins
requires: python, git
provenance: house
route: infer
---

# Plugins

Owns plugin refresh, the one machine install and repository plugin selection.

```text
tj plugins update --host claude
tj install
tj install --check
```

`tj plugins update --host <host>` refreshes that host's plugins to their marketplace pins without running
plugin setup, then applies and verifies the updated Agent package's native CLI pin, launch runtime,
command bindings and shell profiles before activating sessions or publishing success. A failed step
remains failed and is retried even when the plugin cache has not changed. `tj run` checks at launch and
periodically while the session remains open. `tj install` installs
the machine: plugins, runtime, CLI, machine tools, Handy, profiles, agent settings and command bindings. It runs
a selected plugin's setup only when that plugin's setup runtime changed since its last successful setup or its
`setup --check` fails, and `tj install --check` reports drift without writing. After a successful changed
installation, the internal `tj update --host <host> --revision <revision>` publishes the installed revision so
`tj run` tabs apply it in place once idle.
See the [canonical plugin update flow](https://github.com/tj-agents/cli/blob/main/docs/plugin-updates.md)
for details. A current machine changes nothing and exits 0. `tj init` remains shell initialization.
tj-agents plugins load only where a repository selects them; machine installation disables any enabled for
every project.

## Repository selection

Maintainers regenerate the adjacent Marketplace snapshot with
`scripts/update_marketplace_plugins.py --source <marketplace-checkout>`; do not edit the lock by hand.

The commands below show Windows paths and use `python` in Windows PowerShell. On Linux/macOS, use
`python3` with POSIX paths and the same script arguments.

A tracked `.agents/plugins.json` retains project scope. An untracked selection in a Git repository
defaults to local scope; an unversioned directory defaults to project scope. Use `--scope project` or
`--scope local` deliberately when adopting or changing that boundary. Local files are per-clone and
git-excluded. Repository ownership does not select a policy or decide whether files should be committed.
Complete native project admission and installation before claiming plugins loaded.

Codex ignores project-local configuration until the exact repository is trusted; a trusted parent
folder does not admit a nested Git repository. Inspect native `config/read` for the actual cwd and its
project layer's `disabledReason`, then verify the selected plugins through `plugin list --json` and a
fresh session. Within authorized installation scope, use the native project-trust flow or configuration
API for that exact repository; preserve other settings and never trust a broader parent on its behalf.
Without that authority, keep native activation pending. A passing declaration check proves source
settings are current, not that native trust, installation or session loading succeeded.

## The selection

```json
{"schema_version": 1,
 "marketplaces": {
   "tj-agents": {"repository": "tj-agents/marketplace"},
   "agents": {"repository": "Concertable/agents"}},
 "plugins": ["agent@tj-agents", "agent-store@tj-agents", "base@tj-agents",
             "engineering@tj-agents", "workflow@tj-agents",
             "dotnet@tj-agents", "concertable@agents"]}
```

The selection records explicitly adopted packages as `<plugin>@<marketplace>`. Generation enables their
transitive published dependencies in both hosts and includes those dependencies' harness permissions;
it does not flatten that dependency graph into the explicit selection. A marketplace key must match its
published identity, and its repository is a valid `<owner>/<repository>`. The `tj-agents`
name belongs only to `tj-agents/marketplace`. Each declared marketplace supplies a selected plugin, and
two marketplace names cannot refer to the same repository. The schema is `plugins/selection.schema.json`.
TJ Agents producer repositories are package sources, not selectable marketplaces. Follow the
[shared plugin naming rule](https://github.com/tj-agents/marketplace#readme); ordinary validation rejects
producer registrations, including Python and React, before either host's settings are written.

## Adopt a stack

```powershell
python -B '<skill-directory>\scripts\adopt_plugins.py' --root C:\path\to\repository `
  --stack dotnet --mode preview
```

A stack (`dotnet`, `azure`, `react`, `cpp-gcc`, `cpp-msvc`, `win32`, `neovim`, `rust`, `python`, `github-actions`,
`plugin-repository`) resolves to its plugins through `scripts/plugin_roster.json`, so nobody guesses which
plugins a project type needs. Repeat `--stack`, or add `--plugin <name>`. Required plugins are added and
existing selections are kept. With no requested stack or plugin, a new selection uses Agent, Agent Store, Base,
Engineering and Workflow. `preview` lists the changed files, `write` applies them together, and `check`
exits nonzero on drift, which is what CI runs. An unknown stack or plugin is rejected with the valid choices.

Personal is only for personal projects explicitly adopting `personal@tj-agents`. Read the repository's
README.md and AGENTS.md before selection; a GitHub owner, home directory or publisher account never
establishes personal scope. When the user explicitly confirms personal scope, include `--plugin personal`;
otherwise omit it. Later adoption preserves that selection. Ownership alone never adds it.
A repository selects one `kind: profile` plugin; an `organization` brings the profile it extends through
`requires`. Selection, adoption and reconciliation refuse a second profile, and refuse any profile other than
the one an organization extends when that organization's installed employer declaration matches the
repository's origin or names it as its source repository. See
[Plugin kinds](https://github.com/tj-agents/docs/blob/main/PLUGINS.md#plugin-kinds).
Project/local file placement is independent of this policy boundary.
Selecting a profile does not prove its instructions loaded.

A bare `--plugin <name>` selects from `tj-agents`. Add a foreign plugin with
`--plugin concertable@agents --marketplace agents=Concertable/agents`. Subsequent adoption preserves
existing foreign marketplaces and selections. Every selected approved publisher follows `main`.

## Generate settings from an existing selection

Native permission defaults come from `.agents/settings.json`, independently of plugin selection:

```json
{"configuration":{"core":{"permissions":"bypass"}}}
```

`permissions` is a host-neutral enum: `bypass` (the default when omitted) or `standard`.
Machine install projects it to Claude's user-profile `permissions.defaultMode` (`bypassPermissions` or
`default`) and Codex's `approval_policy` plus `default_permissions` (`never`/`:danger-full-access` or
`on-request`/`:workspace`). It replaces conflicting legacy Codex sandbox keys while preserving
unrelated settings. Install profile defaults once per machine through Agent's `install.ps1` on Windows
or `install.sh` on Linux. Claude 2.1.257+ ignores bypass in project and local settings, so repository
generation omits that inert value and inherits the installed user default; `standard` writes its
supported project override. Codex projects either mode into repository settings. `check` reports
source drift; it does not prove the Claude user-profile install has run.
Native project trust, managed restrictions and explicit session overrides still apply; an existing
restricted session must change its native permissions or restart before it can use changed defaults.

```powershell
python -B '<skill-directory>\scripts\repo_config.py' --root C:\path\to\repository --mode write
```

`--lock` defaults to `<root>/.agents/plugins.json`. The generator owns Claude's marketplace,
enabled-plugin, `permissions.allow` and `permissions.deny` keys and Codex's marketplace and plugin tables plus
`.codex/rules/agent-harness.rules`; other project settings stay. Those permissions are every selected plugin's
published `requires.permissions` merged with `.agents/repository-harness.json`, whose entries win; see
[repository permissions](https://github.com/tj-agents/agent#repository-permissions). A committed entry nobody
declares any more is removed; an uncommitted Claude allow or deny entry outside the declared harness is refused
rather than discarded, so move it into that overlay first. `--preserve-permissions` keeps the current
permissions, which is how `tj prepare` runs without a published harness lookup. Project scope also rejects
machine-specific absolute paths (Windows drive and UNC paths, `/home/<user>`, `/Users/<user>`, `/mnt/<drive>`)
anywhere in the committed `.claude/settings.json`, `.codex/config.toml` or `.codex/hooks.json`, in every mode,
reporting `file:line`. Replace such a path with a repository-relative one, or move the setting to an
uncommitted local file. `machine_paths.py --root <checkout>` runs that scan alone.

Both commands preserve a tracked selection's project scope, and `--scope local` forces per-clone adoption. Local
scope writes `.claude/settings.local.json`, `.codex/config.toml`, optional `.codex/rules/agent-harness.rules`
and `.agents/repo-capabilities.local.json` ownership state. It git-excludes all of them and the selection,
preserves unrelated selections and approvals, and refuses tracked targets, changed owned settings and links
that escape the root. Settings generation requires Python 3.12 or newer.

## Audit a machine

For a rename or removal, sweep the old identity before claiming the cutover is complete:

```text
tj plugins audit --identity retired-package@tj-agents --root /path/to/repositories
```

Repeat `--identity` and `--root` as needed. The read-only sweep includes known host projects, Git linked
worktrees, native settings and installed registrations. It prints file and line references without
configuration values, and exits nonzero for references or unreadable paths. Add other repository parent
directories explicitly; it cannot discover arbitrary files outside the supplied roots and host records.
Update owned source declarations and regenerate them; change user settings and registrations only through
their owning scripted commands. Report unresolved references with their paths instead of adding aliases.

Managed launches install missing project-enabled plugins and validate the selected dependency closure
through native inventory, including loader error reasons. SessionStart checks the executing Agent hook
against the installed build even outside a managed launch. The hook cannot prove the version of every
cached skill in a running session: retain its explicit unverified warning, and reload or restart as
directed. A clean settings check or installed inventory alone does not prove session-wide loading.

After every repository carries its own settings, audit the machine before removing old user-profile behavior:

```powershell
python -B '<skill-directory>\scripts\verify_machine.py' --home C:\Users\name --repository C:\path\to\project
```

The verifier is read-only and needs Python 3.12 or newer. Repeat `--repository` for each project; it checks
project Codex agents and local marketplace sources in Codex and Claude settings. GitHub marketplace
declarations are allowed. It reports user-scope plugin selections, marketplaces, hooks, notifications,
instruction files, agent settings, and loose agents or skills. `--json` gives stable finding codes. Exit
status 1 means findings or an inspection failure remain. Output never includes configuration values.
