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
plugin setup; managed `tj claude` and `tj codex` sessions start it at launch and periodically when the
marketplace moved. `tj install` installs the machine: plugins, runtime, CLI, machine tools, Handy,
profiles, agent settings and command bindings. It runs
a selected plugin's setup only when that plugin's setup runtime changed since its last successful setup or its
`setup --check` fails, and `tj install --check` reports drift without writing. After a successful changed
installation, the internal `tj update --host <host> --revision <revision>` publishes the installed revision so
managed sessions apply it in place once idle.
See the [canonical plugin update flow](https://github.com/tj-agents/cli/blob/main/docs/plugin-updates.md)
for details. A current machine changes nothing and exits 0. `tj init` remains shell initialization.
tj-agents plugins load only where a repository selects them; machine installation disables any enabled for
every project.

Startup and plugin refresh check repository instructions and configuration without rewriting them.
Apply reported repository drift explicitly with `tj prepare --host <host> --cwd <path> --write`, or use the
selection and settings commands below. Local adoption preserves owner-supplied instruction files.

## Repository selection

Maintainers regenerate the adjacent Marketplace snapshot with
`scripts/update_marketplace_plugins.py --source <marketplace-checkout>`; do not edit the lock by hand.

The commands below show Windows paths and use `python` in Windows PowerShell. On Linux, use
`python3` with POSIX paths and the same script arguments.

Choose `--scope project` for shared project settings or `--scope local` for per-clone, Git-excluded settings.
Explicit scope is honored independently of the remote owner or employer identity. With no scope option,
a Git repository uses project scope when `.agents/plugins.json` is already tracked and local scope otherwise;
outside Git it defaults to project scope. Track the selection after an explicit project adoption to retain
that scope during later default reconciliation. Scope does not select a policy profile.
Complete the native host's project admission and installation before claiming its plugins loaded.

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
   "tj-agents": {"repository": "tj-agents/marketplace"}},
 "plugins": ["agent@tj-agents", "agent-store@tj-agents", "base@tj-agents",
             "engineering@tj-agents", "workflow@tj-agents",
             "dotnet@tj-agents"]}
```

The selection lists every required package explicitly as `<plugin>@<marketplace>`. A marketplace key must
match its published identity, and its repository must be a well-formed `<owner>/<repository>` without a
`.git` suffix. Custom owners and repositories are supported. The `tj-agents` name belongs only to
`tj-agents/marketplace`. Each declared marketplace supplies a selected plugin, and
two marketplace names cannot refer to the same repository. `scripts/plugin_selection.py` validates the selection.
TJ Agents producer repositories are package sources, not selectable marketplaces. Follow the
[shared plugin naming rule](https://github.com/tj-agents/marketplace#readme); ordinary validation rejects
producer registrations, including Python and React, before either host's settings are written.

## Adopt a stack

```powershell
python -B '<skill-directory>\scripts\adopt_plugins.py' --root C:\path\to\repository `
  --stack dotnet --mode preview
```

A stack (`dotnet`, `azure`, `react`, `cpp-gcc`, `cpp-msvc`, `win32`, `neovim`, `rust`, `python`, `github-actions`,
`plugin-repository`) resolves to its plugins through `scripts/plugin_stacks.json`, so nobody guesses which
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
`--plugin team-policy@team --marketplace team=example-org/agent-marketplace`. Subsequent adoption preserves
existing foreign marketplaces and selections. Declared marketplace catalogs follow `main` and supply their
plugins' source pins.

## Generate settings from an existing selection

Omitting the permission preference in `.agents/settings.json` preserves native permission and approval
controls. An explicit preference is independent of plugin selection:

```json
{"configuration":{"core":{"permissions":"standard"}}}
```

`permissions` accepts `bypass` or `standard`; neither is selected by omission.
For an explicit preference, machine install projects it to Claude's user-profile `permissions.defaultMode`
(`bypassPermissions` or `default`) and Codex's `approval_policy` plus `default_permissions` (`never`/`:danger-full-access` or
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

Both commands use the [scope rule above](#repository-selection). Local scope writes
`.claude/settings.local.json`, `.codex/config.toml`, optional `.codex/rules/agent-harness.rules`
and `.agents/repo-capabilities.local.json` ownership state. It git-excludes all of them and the selection,
preserves unrelated selections and approvals, and refuses tracked targets, changed owned settings and links
that escape the root. Settings generation requires Python 3.12 or newer.

## Audit a machine

After every repository carries its own settings, audit the machine before removing old user-profile behavior:

```powershell
python -B '<skill-directory>\scripts\verify_machine.py' --home C:\Users\name --repository C:\path\to\project
```

The verifier is read-only and needs Python 3.12 or newer. Repeat `--repository` for each project; it checks
project Codex agents and local marketplace sources in Codex and Claude settings. GitHub marketplace
declarations are allowed. It reports user-scope plugin selections, marketplaces, hooks, notifications,
instruction files, agent settings, and loose agents or skills. `--json` gives stable finding codes. Exit
status 1 means findings or an inspection failure remain. Output never includes configuration values.
