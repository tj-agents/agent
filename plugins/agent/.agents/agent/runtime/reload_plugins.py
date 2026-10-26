#!/usr/bin/env python3
"""Refresh selected tj-agents plugins through native hosts (Python 3.9+)."""

from __future__ import annotations

import argparse
import contextlib
import functools
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parent))

import agent_cli
import codex_hook_trust
from codex_hook_trust import AppServer, github_repository
import launch_check
import plugin_autoupdate
import required_plugins

TIMEOUT = 300
IN_USE = ".in_use"
ADOPTION = "Client adoption is separate from installed package verification; an armed restart resumes this same conversation automatically."


class RefreshError(RuntimeError):
    pass


def read_json(path: Path) -> dict:
    if not path.is_file():
        return {}
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise RefreshError(f"Expected an object in {path}")
    return value


def configuration_root(host: str) -> Path:
    variable = "CLAUDE_CONFIG_DIR" if host == "claude" else "CODEX_HOME"
    return Path(os.environ.get(variable) or Path.home() / ("." + host))


def repository(source: object) -> str | None:
    match = github_repository(source)
    return "/".join(match) if match else None


def selection(host: str, executable: str, project: Path) -> tuple[dict, list[str]]:
    if host == "claude":
        marketplaces, plugins = {}, {}
        for path in (configuration_root(host) / "settings.json", project / ".claude/settings.json",
                     project / ".claude/settings.local.json"):
            settings = read_json(path)
            marketplaces.update(settings.get("extraKnownMarketplaces", {}))
            plugins.update(settings.get("enabledPlugins", {}))
        known = read_json(configuration_root(host) / "plugins/known_marketplaces.json")
        sources = {name: entry.get("source", {}) for name, entry in known.items()}
        sources.update({name: entry.get("source", {}) for name, entry in marketplaces.items()})
        sources = {name: repository(source.get("repo")) if source.get("source") == "github"
                   else repository(source.get("url")) for name, source in sources.items() if isinstance(source, dict)}
        enabled = [name for name, flag in plugins.items() if flag is True]
    else:
        with AppServer(executable, project) as server:
            response = server.request("config/read", {"cwd": str(project), "includeLayers": True})
        config = response["config"]
        sources = {name: repository(source.get("source")) for name, source in config.get("marketplaces", {}).items()
                   if source.get("source_type") == "git"}
        enabled = [name for name, flag in config.get("plugins", {}).items()
                   if isinstance(flag, dict) and flag.get("enabled") is True]
    sources = {name: repo for name, repo in sources.items() if repo and repo.split("/", 1)[0] == "tj-agents"}
    enabled = sorted(name for name in enabled if name.rsplit("@", 1)[-1] in sources)
    used = {name.rsplit("@", 1)[-1] for name in enabled}
    selected = {name: repo for name, repo in sorted(sources.items()) if name in used}
    for name, repo in selected.items():
        error = plugin_autoupdate.marketplace_error(name, repo)
        if error:
            raise RefreshError(error)
    return selected, enabled


def run(command: list[str], project: Path) -> str:
    print("Refreshing: " + " ".join(command[1:]), file=sys.stderr, flush=True)
    result = subprocess.run(command, cwd=project, capture_output=True, text=True,
                            encoding="utf-8", errors="replace", timeout=TIMEOUT)
    if result.returncode:
        raise RefreshError((result.stderr or result.stdout).strip() or f"Command exited {result.returncode}")
    return result.stdout


@functools.lru_cache(maxsize=None)
def resolved(path: object) -> Path:
    return Path(path).resolve()


def path_key(path: object) -> str:
    return os.path.normcase(str(resolved(path)))


def declared_scopes(identity: str, project: Path) -> list[str]:
    return [scope for scope, name in (("project", "settings.json"), ("local", "settings.local.json"))
            if read_json(project / ".claude" / name).get("enabledPlugins", {}).get(identity) is True]


def update_targets(identity: str, project: Path, installations: list) -> list[tuple[str, Path]]:
    declared = declared_scopes(identity, project)
    targets = {}
    for entry in required_plugins.claude_installations(project, installations):
        scope = entry.get("scope")
        if entry.get("id") != identity or scope not in {"user", "project", "local"}:
            continue
        owner = project if scope == "user" else Path(entry["projectPath"])
        targets.setdefault(scope, (scope, owner))
    for scope in declared or ["user"]:
        targets.setdefault(scope, (scope, project))
    return [targets[key] for key in sorted(targets)]


def update_result(output: str, identity: str, scope: str) -> dict:
    for line in reversed(output.splitlines()):
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict):
            break
    else:
        raise RefreshError(f"{identity} ({scope}): claude plugin update printed no result")
    outcome = value.get("updateOutcome")
    if outcome not in {"updated", "up_to_date"} or value.get("refreshFailed") or not value.get("newVersion"):
        raise RefreshError(f"{identity} ({scope}) was not updated: {value.get('message') or outcome}")
    return value


def plugin_errors(installations: list, project: Path) -> list[str]:
    return [f"{entry.get('id')} ({entry.get('scope')}): {error}"
            for entry in required_plugins.claude_installations(project, installations)
            if entry.get("enabled") is True for error in entry.get("errors") or []]


def entry_for(installations: list, identity: str, scope: str, owner: Path) -> dict | None:
    return next((entry for entry in installations if entry.get("id") == identity and entry.get("scope") == scope
                 and (scope == "user" or entry.get("projectPath") and path_key(entry["projectPath"]) == path_key(owner))),
                None)


def persistence_problems(expected: list[tuple[str, str, Path, str]], installations: list) -> list[str]:
    problems = []
    for identity, scope, owner, version in expected:
        entry = entry_for(installations, identity, scope, owner)
        if entry is None or entry.get("version") != version:
            found = entry.get("version") if entry else "nothing"
            problems.append(f"{identity} ({scope}) update did not persist: installed_plugins.json has {found}, "
                            f"the update reported {version}")
    return problems


def dependency_identity(dependency: object, marketplace: str) -> str | None:
    if isinstance(dependency, dict):
        name, marketplace = dependency.get("name"), dependency.get("marketplace") or marketplace
    else:
        name = dependency
    if not isinstance(name, str) or not name or not isinstance(marketplace, str):
        return None
    return name if "@" in name else f"{name}@{marketplace}"


def catalog_dependencies() -> dict[str, list[str]]:
    result = {}
    for name, (_, location) in tj_agents_marketplaces().items():
        for entry in read_json(location / ".claude-plugin/marketplace.json").get("plugins", []):
            if isinstance(entry, dict) and isinstance(entry.get("name"), str):
                result[f"{entry['name']}@{name}"] = [identity for dependency in entry.get("dependencies") or []
                                                     if (identity := dependency_identity(dependency, name))]
    return result


def missing_dependencies(identity: str, scope: str, owner: Path, dependencies: dict[str, list[str]],
                         installations: list) -> list[str]:
    order, seen, visiting = [], set(), set()

    def visit(plugin: str) -> None:
        if plugin in visiting:
            raise RefreshError(f"Cyclic plugin dependency: {plugin}")
        visiting.add(plugin)
        for dependency in dependencies.get(plugin, []):
            if dependency not in seen:
                visit(dependency)
                seen.add(dependency)
                order.append(dependency)
        visiting.remove(plugin)
    visit(identity)
    if not order:
        return []
    effective = required_plugins.claude_installations(owner, installations)
    return [dependency for dependency in order if not any(
        entry.get("id") == dependency and (entry.get("scope") in {"user", "managed"}
                                           or scope != "user" and entry.get("scope") in {"project", "local"})
        for entry in effective)]


def dependency_installs(dependents: list[tuple[str, str, Path]]) -> list[tuple[str, str, Path]]:
    dependencies, installations, installs = catalog_dependencies(), installed_entries(), []
    registered = read_json(configuration_root("claude") / "plugins/known_marketplaces.json").keys()
    for identity, scope, owner in dependents:
        for dependency in missing_dependencies(identity, scope, owner, dependencies, installations):
            install = (dependency, scope, owner)
            if dependency.rsplit("@", 1)[1] in registered and install not in installs:
                installs.append(install)
    return installs


def apply_claude(executable: str, project: Path, installs: list[tuple[str, str]],
                 updates: list[tuple[str, str, Path]], starting=None, dependencies=None) -> list[str]:
    dependents = [(identity, scope, project) for identity, scope in installs] + updates
    dependencies = list(dict.fromkeys((dependencies or []) + dependency_installs(dependents)))
    for dependency, scope, owner in dependencies:
        run([executable, "plugin", "install", dependency, "--scope", scope], owner)
    for identity, scope in installs:
        if (identity, scope, project) not in dependencies:
            run([executable, "plugin", "install", identity, "--scope", scope], project)
    expected, updated = [], list(dict.fromkeys([identity for identity, _ in installs]
                                               + [dependency for dependency, _, _ in dependencies]))
    for identity, scope, owner in updates:
        if starting is not None:
            starting(identity, scope, owner)
        result = update_result(run([executable, "plugin", "update", identity, "--scope", scope, "--json"], owner),
                               identity, scope)
        expected.append((identity, scope, owner, result["newVersion"]))
        if result["updateOutcome"] == "updated" and identity not in updated:
            updated.append(identity)
    after = json.loads(run([executable, "plugin", "list", "--json"], project))
    problems = persistence_problems(expected, after) + plugin_errors(after, project)
    for identity, scope, owner in list(dict.fromkeys(dependencies + [(identity, scope, project) for identity, scope in installs])):
        inventory = after if owner == project else json.loads(run([executable, "plugin", "list", "--json"], owner))
        entry = next((entry for entry in required_plugins.claude_installations(owner, inventory)
                      if entry.get('id') == identity and entry.get('scope') == scope), None)
        if entry is None or entry.get("enabled") is not True:
            problems.append(f"{identity} ({scope}): native installation did not persist enabled")
    if problems:
        raise RefreshError("; ".join(problems))
    return updated


def refresh_claude(executable: str, project: Path, plugins: list[str]) -> list[str]:
    installations = json.loads(run([executable, "plugin", "list", "--json"], project))
    effective = required_plugins.claude_installations(project, installations)
    installs = [(identity, scope) for identity in plugins for scope in declared_scopes(identity, project)
                if not any(entry.get('id') == identity and entry.get('scope') == scope for entry in effective)]
    updates = [(identity, scope, owner) for identity in plugins
               for scope, owner in update_targets(identity, project, installations)]
    return apply_claude(executable, project, installs, updates)


def refresh_marketplaces(host: str, executable: str, project: Path, marketplaces: dict) -> None:
    known = read_json(configuration_root(host) / "plugins/known_marketplaces.json") if host == "claude" else {}
    for name in marketplaces:
        if host == "claude" and name not in known:
            run([executable, "plugin", "marketplace", "add", marketplaces[name]], project)
        run([executable, "plugin", "marketplace", "update" if host == "claude" else "upgrade", name], project)


def refresh(host: str, executable: str, project: Path, marketplaces: dict, plugins: list[str]) -> list[str]:
    refresh_marketplaces(host, executable, project, marketplaces)
    if host == "claude":
        return refresh_claude(executable, project, plugins) if plugins else []
    user_config = configuration_root(host) / "config.toml"
    user_enabled = plugin_autoupdate.enabled_identities(plugin_autoupdate.load_toml(user_config))
    for identity in plugins:
        run([executable, "plugin", "add", identity], project)
    plugin_autoupdate.remove_user_plugins(user_config, set(plugins) - user_enabled)
    return []


def repair_missing(host: str, project: Path, missing, executable: str | None = None) -> None:
    executable = (executable_for(host) if not executable or Path(executable).suffix.casefold() in {".cmd", ".ps1", ".bat"}
                  else executable)
    if not executable:
        raise RefreshError(f"Cannot repair required {host} plugins: executable is unavailable")
    marketplaces, enabled = selection(host, executable, project)
    undeclared = sorted(set(missing) - set(enabled))
    if undeclared:
        raise RefreshError("Required plugins are not enabled in this host's configuration: "
                           + ", ".join(undeclared) + ". Repair repository selection; no setting was enabled.")
    marketplaces = {name: source for name, source in marketplaces.items()
                    if any(identity.endswith("@" + name) for identity in missing)}
    refresh_marketplaces(host, executable, project, marketplaces)
    if host == "claude":
        installs = [(identity, scope) for identity in missing
                    for scope in declared_scopes(identity, project) or ["user"]]
        apply_claude(executable, project, installs, [])
    else:
        user_config = configuration_root(host) / "config.toml"
        user_enabled = plugin_autoupdate.enabled_identities(plugin_autoupdate.load_toml(user_config))
        for identity in missing:
            run([executable, "plugin", "add", identity], project)
        plugin_autoupdate.remove_user_plugins(user_config, set(missing) - user_enabled)
    required_plugins.check(project, host, executable)


def installed_entries() -> list[dict]:
    plugins = read_json(configuration_root("claude") / "plugins/installed_plugins.json").get("plugins")
    if not isinstance(plugins, dict):
        return []
    return [{**entry, "id": identity} for identity, entries in plugins.items() if isinstance(entries, list)
            for entry in entries if isinstance(entry, dict)]


def tj_agents_marketplaces() -> dict[str, tuple[str, Path]]:
    found = {}
    for name, entry in read_json(configuration_root("claude") / "plugins/known_marketplaces.json").items():
        source = entry.get("source") if isinstance(entry, dict) else None
        if not isinstance(source, dict) or not isinstance(entry.get("installLocation"), str):
            continue
        repo = repository(source.get("repo")) if source.get("source") == "github" else repository(source.get("url"))
        if repo and repo.split("/", 1)[0] == "tj-agents":
            found[name] = (repo, Path(entry["installLocation"]))
    return found


def catalog_publishers(marketplaces: dict[str, tuple[str, Path]]) -> dict[str, dict[str, tuple[str, str]]]:
    result = {}
    for name, (_, location) in marketplaces.items():
        result[name] = {}
        for entry in read_json(location / ".claude-plugin/marketplace.json").get("plugins", []):
            source = entry.get("source") if isinstance(entry, dict) else None
            if isinstance(source, dict) and source.get("source") == "git-subdir":
                key = plugin_autoupdate.publisher_key(source.get("url"), source.get("sha") or source.get("ref"))
                if key:
                    result[name][entry.get("name")] = (key, plugin_autoupdate.plugin_path(source))
    return result


def refresh_heads(repositories: set[str] | None, revision=plugin_autoupdate.remote_revision, query=None) -> set[str]:
    marketplaces = tj_agents_marketplaces()
    owners = {f"{plugin}@{name}": source for name, plugins in catalog_publishers(marketplaces).items()
              for plugin, source in plugins.items()}
    everything = (repositories is None or not plugin_autoupdate.read_contents()["heads"]
                  or any(repo in repositories for repo, _ in marketplaces.values()))
    wanted = {}
    for key, path in owners.values():
        if everything or key.rsplit("@", 1)[0] in repositories:
            wanted.setdefault(key, set()).add(path)
    if not wanted:
        return set()
    installed = {}
    for entry in installed_entries():
        sha = entry.get("gitCommitSha")
        if isinstance(sha, str) and plugin_autoupdate.SHA.fullmatch(sha) and entry["id"] in owners:
            installed.setdefault(owners[entry["id"]][0], set()).add(sha)
    heads = plugin_autoupdate.refresh_contents(wanted, installed, query, revision)
    return {key for key in wanted if key not in heads}


def attempt_key(identity: str, scope: str, owner: Path) -> str:
    return f"{identity}|{scope}|{'user' if scope == 'user' else path_key(owner)}"


def reconcile_claude(project: Path, event: bool = False, repositories: set[str] | None = None,
                     fresh: frozenset[str] = frozenset()) -> list[str]:
    marketplaces, plugins = selection("claude", "", project)
    installations = installed_entries()
    effective = required_plugins.claude_installations(project, installations)
    contents = plugin_autoupdate.read_contents()
    publishers = catalog_publishers(tj_agents_marketplaces())
    installs, updates, present, attempts = [], [], [], {}
    for identity in plugins:
        plugin, name = identity.rsplit("@", 1)
        key, path = publishers.get(name, {}).get(plugin, (None, None))
        head = contents["heads"].get(key) if key else None
        forced = event and key is None and (repositories is None or marketplaces.get(name) in repositories)
        installs += [(identity, scope) for scope in declared_scopes(identity, project) or ["user"]
                     if not any(entry.get('id') == identity and entry.get('scope') == scope for entry in effective)]
        for scope, owner in update_targets(identity, project, installations):
            entry = entry_for(installations, identity, scope, owner)
            if entry is None:
                continue
            present.append((identity, scope, owner))
            same = (plugin_autoupdate.same_content(contents, key.rsplit("@", 1)[0], path, entry.get("gitCommitSha"), head)
                    if head is not None else True)
            attempted = contents["attempts"].get(attempt_key(identity, scope, owner)) == head
            if forced or (same is not True if event else same is False) and not attempted:
                updates.append((identity, scope, owner))
                if head is not None:
                    attempts[attempt_key(identity, scope, owner)] = head
    dependencies = dependency_installs(present)
    if not installs and not updates and not dependencies:
        return []
    touched = {work[0].rsplit("@", 1)[1] for work in installs + updates + dependencies}
    executable = agent_cli.resolve_claude_executable()
    refresh_marketplaces("claude", executable, project,
                         {name: repo for name, repo in marketplaces.items() if name in touched - fresh})
    def starting(identity: str, scope: str, owner: Path) -> None:
        if (key := attempt_key(identity, scope, owner)) in attempts:
            with contextlib.suppress(OSError):
                plugin_autoupdate.record_contents(attempts={key: attempts[key]})

    return apply_claude(executable, project, installs, updates, starting, dependencies)


def refresh_repositories(project: Path, repositories: set[str] | None, updated: set[str] | None = None,
                         revision=plugin_autoupdate.remote_revision, query=None) -> bool:
    marketplaces, _ = selection("claude", "", project)
    fresh = {name: repo for name, repo in marketplaces.items() if repositories is None or repo in repositories}
    if fresh:
        refresh_marketplaces("claude", agent_cli.resolve_claude_executable(), project, fresh)
    unresolved = refresh_heads(repositories, revision, query)
    if unresolved:
        print(f"plugins: could not read the published head of {', '.join(sorted(unresolved))}", file=sys.stderr)
    changed = reconcile_claude(project, event=True, repositories=repositories, fresh=frozenset(fresh))
    if updated is not None:
        updated.update(changed)
    return repositories is None or not any(key.rsplit("@", 1)[0] in repositories for key in unresolved)


def package_tree(root: Path) -> dict[str, bytes]:
    if not root.is_dir():
        raise RefreshError(f"Missing plugin directory: {root}")
    result = {}
    for path in root.rglob("*"):
        parts = path.relative_to(root).parts
        if parts[0] == IN_USE:
            continue
        if path.is_symlink():
            raise RefreshError(f"Unexpected plugin symlink: {path}")
        if path.is_file() and ".git" not in parts:
            result[path.relative_to(root).as_posix()] = path.read_bytes()
    return result


def source_tree(source: object, location: Path, project: Path, temporary: Path,
                publishers: dict) -> dict[str, bytes]:
    if isinstance(source, dict) and source.get("source") == "git-subdir":
        publisher = repository(source.get("url"))
        if not publisher or publisher.split("/", 1)[0] != "tj-agents":
            raise RefreshError("Cannot verify package from an unknown publisher")
        ref = source.get("sha") or source.get("ref") or "main"
        if not isinstance(ref, str) or not ref or ref.startswith("-"):
            raise RefreshError("Invalid package source revision")
        key = (publisher, ref)
        if key not in publishers:
            checkout = temporary / str(len(publishers))
            run(["git", "init", str(checkout)], project)
            run(["git", "-C", str(checkout), "fetch", "--depth=1",
                 f"https://github.com/{publisher}.git", ref], project)
            run(["git", "-C", str(checkout), "checkout", "--detach", "FETCH_HEAD"], project)
            publishers[key] = checkout
        location = publishers[key]
        source = source.get("path")
    elif isinstance(source, dict) and source.get("source") == "local":
        source = source.get("path")
    if not isinstance(source, str) or not source or Path(source).is_absolute():
        raise RefreshError("Cannot verify catalog package source")
    root = (location / source).resolve()
    if not root.is_relative_to(location.resolve()):
        raise RefreshError("Catalog package escapes its publisher")
    return package_tree(root)


def verify(host: str, executable: str, project: Path, marketplaces: dict, plugins: list[str]) -> dict:
    with tempfile.TemporaryDirectory(prefix="tj-agents-verify-") as temporary:
        return verify_packages(host, executable, project, marketplaces, plugins, Path(temporary))


def verify_packages(host: str, executable: str, project: Path, marketplaces: dict,
                    plugins: list[str], temporary: Path) -> dict:
    print(f"Verifying {host} native installations and marketplace revisions...", file=sys.stderr, flush=True)
    inventory = json.loads(run([executable, "plugin", "list", "--json"], project))
    entries = inventory if host == "claude" else inventory.get("installed", [])
    if host == "claude":
        entries = required_plugins.claude_installations(project, entries)
    installed = {entry.get("id", entry.get("pluginId")) for entry in entries if entry.get("enabled") is True}
    problems = [f"Missing enabled native installation: {identity}" for identity in plugins if identity not in installed]
    if host == "claude":
        problems += plugin_errors(entries, project)
    revisions = {}
    publishers = {}
    for name, repo in marketplaces.items():
        print(f"Verifying {name} against {repo} main...", file=sys.stderr, flush=True)
        remote = run(["git", "ls-remote", f"https://github.com/{repo}.git", "refs/heads/main"], project).split()
        if not remote or not re.fullmatch(r"[0-9a-f]{40}", remote[0]):
            raise RefreshError(f"Could not resolve main for {repo}")
        if host == "claude":
            known = read_json(configuration_root(host) / "plugins/known_marketplaces.json")
            location = known.get(name, {}).get("installLocation")
        else:
            with AppServer(executable, project) as server:
                listing = server.request("plugin/list", {"cwds": [str(project)]})
            location = next((str(Path(market["path"]).parents[2]) for market in listing.get("marketplaces", [])
                             if market.get("name") == name), None)
        if not location:
            problems.append(f"Missing native marketplace checkout: {name}")
            continue
        local = run(["git", "-C", location, "rev-parse", "HEAD"], project).strip()
        revisions[name] = {"installed": local, "main": remote[0]}
        if local != remote[0]:
            problems.append(f"{name} checkout is at {local}, main is {remote[0]}")
        catalog = read_json(Path(location) / (".claude-plugin/marketplace.json" if host == "claude"
                                             else ".agents/plugins/marketplace.json"))
        sources = {entry["name"]: entry.get("source") for entry in catalog.get("plugins", [])}
        for identity in plugins:
            plugin, marketplace = identity.rsplit("@", 1)
            if marketplace != name:
                continue
            try:
                expected = source_tree(sources.get(plugin), Path(location), project, temporary, publishers)
            except RefreshError as error:
                problems.append(f"{identity}: {error}")
                continue
            for entry in entries:
                if entry.get("id", entry.get("pluginId")) != identity:
                    continue
                install = (Path(entry["installPath"]) if host == "claude" else configuration_root(host)
                           / "plugins/cache" / name / plugin / (entry.get("version") or "local"))
                if package_tree(install) != expected:
                    problems.append(f"Native installed bytes differ from marketplace: {identity} ({install})")
    return {"host": host, "plugins": plugins, "marketplaces": revisions, "problems": problems}


def executable_for(host: str) -> str:
    return agent_cli.resolve_claude_executable() if host == "claude" else agent_cli.resolve_codex_executable()[0]


def relaunch_session(project: Path, updated: set[str]) -> dict | None:
    import session_relaunch

    identity = session_relaunch.session_identity()
    if identity is None:
        return None
    installed = read_json(configuration_root("claude") / "plugins/installed_plugins.json").get("plugins") or {}
    _, plugins = selection("claude", "", project)
    stale = session_relaunch.stale_plugins(identity[1], project, plugins, installed, updated)
    if not stale:
        return None
    if os.environ.get("CLAUDE_CODE_CHILD_SESSION"):
        return {"relaunch": "skipped", "stale": stale, "reason": "a child session cannot relaunch its parent; "
                                                              "run agent:refresh from the parent session"}
    try:
        result = session_relaunch.relaunch(project, installed, host="claude")
    except (OSError, RuntimeError, ValueError) as error:
        return {"relaunch": "failed", "stale": stale, "error": str(error)}
    if result.get("relaunch") != "armed":
        return {"relaunch": "failed", "stale": stale, "error": "the owning Claude client restart was not armed"}
    return {**result, "stale": stale, "adoption": "pending"}


def relaunch_codex(project: Path, reason: str, allowed: bool) -> dict:
    import session_relaunch

    if not allowed:
        raise RefreshError("Codex requires client restart, but --no-relaunch forbids it: " + reason)
    result = session_relaunch.relaunch(project, {}, host="codex")
    if result.get("relaunch") != "armed":
        raise RefreshError("The owning Codex client restart was not armed")
    return {**result, "reason": reason, "adoption": "pending"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", choices=("claude", "codex", "both"), default="both")
    parser.add_argument("--project", type=Path, default=Path.cwd())
    parser.add_argument("--no-relaunch", action="store_true", help="Never relaunch the calling CLI session")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--all", action="store_true", help="Refresh every selected plugin, not only published changes")
    modes.add_argument("--verify", action="store_true", help="Refresh, then inspect native installations and remote main revisions")
    modes.add_argument("--verify-only", action="store_true", help="Inspect without changing plugins")
    parser.add_argument("--onboard", action="store_true", help="allow explicitly requested plugin enrollment")
    arguments = parser.parse_args(argv)
    if arguments.verify_only and arguments.onboard:
        parser.error("--verify-only cannot be used with --onboard")
    project = arguments.project.resolve()
    hosts = ("claude", "codex") if arguments.host == "both" else (arguments.host,)
    updated: set[str] = set()
    def converge_plugin_setup() -> list[str]:
        executables = {host: None for host in hosts}
        return required_plugins.check_setup(project, hosts, executables) if arguments.verify_only \
            else required_plugins.converge_setup(project, hosts, executables, arguments.onboard)
    relaunch_allowed = "claude" in hosts and not arguments.no_relaunch and not arguments.verify_only
    if not (arguments.all or arguments.verify or arguments.verify_only):
        result = launch_check.check(project, update=lambda host, repositories, root:
                                    launch_check.update_host(host, repositories, root, updated),
                                    reconcile=lambda root: launch_check.reconcile_installs(root, updated),
                                    host=None if arguments.host == "both" else arguments.host)
        if result != "unconfigured":
            summary = {"check": result}
            restart_reason = None
            if "codex" in hosts and result in {"current", "updated", "offline"}:
                try:
                    launch_check.ensure_hooks(project, running_session=True)
                except codex_hook_trust.RestartRequired as error:
                    restart_reason = str(error)
                except Exception as error:
                    summary["hook_readiness_error"] = str(error)
                    result = "failed"
            setup_errors = (converge_plugin_setup() if result not in {"failed", "busy"}
                            else [f"skipped because launch check is {result}"])
            summary["plugin setup"] = setup_errors
            if restart_reason and result not in {"failed", "busy", "offline"} and not setup_errors:
                try:
                    summary["codex"] = relaunch_codex(project, restart_reason, not arguments.no_relaunch)
                except (OSError, ValueError, RuntimeError) as error:
                    summary["hook_readiness_error"] = str(error)
                    result = "failed"
            relaunched = relaunch_session(project, updated) if relaunch_allowed and result != "failed" and not setup_errors else None
            if relaunched:
                summary["claude"] = relaunched
            print(json.dumps(summary, indent=2))
            if result == "updated":
                print(ADOPTION, file=sys.stderr)
            failed = bool(relaunched and relaunched["relaunch"] == "failed")
            return int(result in {"failed", "offline", "busy"} or failed or bool(setup_errors))
    reports = []
    restart_reason = None
    for host in hosts:
        try:
            executable = executable_for(host)
            marketplaces, plugins = selection(host, executable, project)
            changed = [] if arguments.verify_only else refresh(host, executable, project, marketplaces, plugins)
            updated.update(changed)
            report = (verify(host, executable, project, marketplaces, plugins)
                      if arguments.verify or arguments.verify_only else {"host": host, "plugins": plugins, "problems": []})
            if host == "codex" and not arguments.verify_only and not report["problems"]:
                try:
                    launch_check.ensure_hooks(project, executable, running_session=True)
                except codex_hook_trust.RestartRequired as error:
                    restart_reason = str(error)
        except (RefreshError, agent_cli.LaunchError, OSError, ValueError, RuntimeError, KeyError, TypeError, subprocess.TimeoutExpired) as error:
            report = {"host": host, "problems": [str(error)]}
        reports.append(report)
    setup_errors = (converge_plugin_setup() if not any(report["problems"] for report in reports)
                    else ["skipped because plugin refresh or verification failed"])
    if setup_errors:
        reports.append({"host": "plugin setup", "plugins": [], "problems": setup_errors})
    if restart_reason and not any(report["problems"] for report in reports):
        try:
            result = relaunch_codex(project, restart_reason, not arguments.no_relaunch)
        except (OSError, ValueError, RuntimeError) as error:
            reports.append({"host": "codex restart", "plugins": [], "problems": [str(error)]})
        else:
            reports.append({"host": "codex restart", **result, "problems": []})
    if "claude" in hosts and relaunch_allowed and not any(report["problems"] for report in reports):
        relaunched = relaunch_session(project, updated)
        if relaunched:
            reports.append({"host": "claude relaunch", "plugins": [], "problems":
                            [] if relaunched["relaunch"] != "failed" else ["relaunch failed: " + relaunched["error"]]})
    print(json.dumps(reports, indent=2))
    for report in reports:
        for problem in report["problems"]:
            print(f"plugins: {report['host']}: {problem}", file=sys.stderr)
    print(ADOPTION, file=sys.stderr)
    return int(any(report["problems"] for report in reports))


if __name__ == "__main__":
    raise SystemExit(main())
