---
name: plugins
description: Select a repository's plugins and generate its rolling-main Claude and Codex settings. Use when adopting plugins for a repository or stack, updating a repository's selected packages, or auditing a machine before removing old user-level plugin settings.

kind: utility
domain: agent
profile: core
applicability: repositories adopting plugins
requires: python, git
provenance: house
route: infer
---

# Plugin selection

Maintainers regenerate the adjacent Marketplace snapshot with
`scripts/update_marketplace_catalog.py --source <marketplace-checkout>`; do not edit the lock by hand.

The commands below show Windows paths and use `python` in Windows PowerShell. On Linux/macOS, use
`python3` with POSIX paths and the same script arguments.

A repository whose GitHub owner is listed in `repo-owners.json` commits `.agents/plugins.json`,
`.claude/settings.json` and `.codex/config.toml`, carrying its selection from the declared marketplaces on
`main`. Any other repository, such as an employer's, gets local scope only. Its files are written per clone
and git-excluded, and never committed, branched or opened as a PR. The scripts default to local scope there
and refuse `--scope project`. Complete the native host's project admission and installation before claiming
those plugins loaded.

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

The selection lists every required package explicitly as `<plugin>@<marketplace>`. A marketplace key must
match its published identity, and its repository must belong to `tj-agents` or `Concertable`. The `tj-agents`
name belongs only to `tj-agents/marketplace`. Each declared marketplace supplies a selected plugin, and
two marketplace names cannot refer to the same repository. The schema is `catalog/plugins.schema.json`.
TJ Agents producer repositories are package sources, not selectable marketplaces. Follow the
[shared catalog naming rule](https://github.com/tj-agents/marketplace#readme); ordinary validation rejects
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
Engineering, Machine and Workflow. `preview` lists the changed files, `write` applies them together, and `check`
exits nonzero on drift, which is what CI runs. An unknown stack or plugin is rejected with the valid choices.

Personal is only for personal projects explicitly adopting `personal@tj-agents`. Read the repository's
README.md and AGENTS.md before selection; a GitHub owner, home directory or publisher account never
establishes personal scope. When the user explicitly confirms personal scope, include `--plugin personal`;
otherwise omit it. Later adoption preserves that selection. Ownership alone never adds it. Infonetica,
including `tj-agents/infonetica` work-tooling source and its worktrees, is work scope: never select or inject
Personal there. Owner-based project/local file placement is independent of this policy boundary.
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
enabled-plugin and `permissions.allow` keys and Codex's marketplace and plugin tables plus
`.codex/rules/agent-harness.rules`; other project settings stay. Existing Claude allow entries outside the
declared harness must move into `.agents/repository-harness.json` first. Project scope also rejects
machine-specific absolute paths (Windows drive and UNC paths, `/home/<user>`, `/Users/<user>`, `/mnt/<drive>`)
anywhere in the committed `.claude/settings.json`, `.codex/config.toml` or `.codex/hooks.json`, in every mode,
reporting `file:line`. Replace such a path with a repository-relative one, or move the setting to an
uncommitted local file. `machine_paths.py --root <checkout>` runs that scan alone.

Both commands pick the scope from repository ownership, and `--scope local` forces per-clone adoption. Local
scope writes `.claude/settings.local.json`, `.codex/config.toml`, optional `.codex/rules/agent-harness.rules`
and `.agents/repo-capabilities.local.json` ownership state. It git-excludes all of them and the selection,
preserves unrelated selections and approvals, and refuses tracked targets, changed owned settings and links
that escape the root. Settings generation requires Python 3.11 or newer.

## Audit a machine

After every repository carries its own settings, audit the machine before removing old user-profile behavior:

```powershell
python -B '<skill-directory>\scripts\verify_machine.py' --home C:\Users\name --repository C:\path\to\project
```

The verifier is read-only and needs Python 3.11 or newer. Repeat `--repository` for each project; it checks
project Codex agents and local marketplace sources in Codex and Claude settings. GitHub marketplace
declarations are allowed. It reports user-scope plugin selections, marketplaces, hooks, notifications,
instruction files, agent settings, and loose agents or skills. `--json` gives stable finding codes. Exit
status 1 means findings or an inspection failure remain. Output never includes configuration values.
