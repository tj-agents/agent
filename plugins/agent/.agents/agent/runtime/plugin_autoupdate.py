#!/usr/bin/env python3
"""Keep this machine's agent plugins current and its merged worktrees removed (Python 3.11+).

Codex marketplaces refresh only when their source revision changed. The worktree sweep runs from the
installed engineering package, so it always matches the current standards.
"""

from __future__ import annotations

import argparse
import hashlib
from concurrent.futures import ThreadPoolExecutor
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time

GIT_TIMEOUT_SECONDS = 30
SWEEP_TIMEOUT_SECONDS = 1800
SCRIPT_NAME = "plugin_autoupdate.py"
RUNTIME = ("plugin_autoupdate.py", "machine_events.py", "launch_check.py", "reload_plugins.py", "agent_cli.py",
           "codex_hook_trust.py", "codex_websocket.py", "required_plugins.py", "cli_client.py", "terminal_command.py")
NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
CLI_TIMEOUT_SECONDS = 60
STILL_ACTIVE = 259
FAILURE_BACKOFF_SECONDS = 24 * 60 * 60
RETIRED = "retired"
HOOK_DIGEST = re.compile(r'"(sha256:[0-9a-f]{64})"')
SHA = re.compile(r"[0-9a-f]{40}")
NAME = re.compile(r"[A-Za-z0-9_.-]+")
REVISION_PATH = re.compile(r"[A-Za-z0-9_-][A-Za-z0-9_.-]*(?:/[A-Za-z0-9_-][A-Za-z0-9_.-]*)*")
CONTENTS_WAIT_SECONDS = 10
SNAPSHOT_FIRST = "(Path(s) if s else d/'hook-snapshots')/k/"
MARKETPLACE_NAME = "tj-agents"
MARKETPLACE_REPOSITORY = "tj-agents/marketplace"


def producer_repository(repository: object) -> bool:
    return (isinstance(repository, str) and repository.casefold().startswith("tj-agents/")
            and repository.casefold() != MARKETPLACE_REPOSITORY)


def marketplace_error(name: str, repository: object) -> str | None:
    canonical_repository = isinstance(repository, str) and repository.casefold() == MARKETPLACE_REPOSITORY
    if ((name == MARKETPLACE_NAME and repository != MARKETPLACE_REPOSITORY)
            or (name != MARKETPLACE_NAME and canonical_repository)):
        return f"The {MARKETPLACE_NAME} marketplace must be {MARKETPLACE_REPOSITORY} and nothing else may use that name"
    if producer_repository(repository):
        return f"{repository} is published through the {MARKETPLACE_NAME} marketplace; select {MARKETPLACE_REPOSITORY}"
    return None


def state_directory() -> Path:
    return Path(os.environ.get("AGENT_STATE_DIRECTORY") or Path.home() / ".agents-state")


def codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")


def claude_home() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")


def read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def write_json(path: Path, value: dict) -> None:
    write_text(path, json.dumps(value, indent=2, ensure_ascii=False) + "\n")


def is_pinned(ref: object) -> bool:
    return isinstance(ref, str) and len(ref) == 40 and all(character in "0123456789abcdefABCDEF"
                                                            for character in ref)


def load_toml(path: Path) -> dict:
    try:
        import tomllib
    except ImportError:
        return {}

    try:
        with path.open("rb") as stream:
            return tomllib.load(stream)
    except (OSError, tomllib.TOMLDecodeError):
        return {}


def github_repository(source: object) -> str | None:
    match = re.fullmatch(r"(?:(?:https?://|ssh://git@|git@)github\.com[/:])?([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)"
                         r"(?:\.git)?/?", source.strip()) if isinstance(source, str) else None
    return f"{match[1]}/{match[2]}".casefold() if match else None


def git_marketplace(source: object) -> dict | None:
    if (not isinstance(source, dict) or source.get("source_type") != "git"
            or not isinstance(source.get("source"), str) or not source["source"]):
        return None
    ref = source.get("ref")
    return {"source": source["source"], "ref": ref if isinstance(ref, str) and ref else None}


def project_directories(config: dict, temporary: Path | None = None) -> list[Path]:
    projects = config.get("projects", {})
    if not isinstance(projects, dict):
        return []
    temporary = (temporary or Path(tempfile.gettempdir())).resolve()
    return sorted({Path(path).resolve() for path, settings in projects.items()
                   if isinstance(settings, dict) and settings.get("trust_level") == "trusted"
                   and (Path(path) / ".codex/config.toml").is_file()
                   and not Path(path).resolve().is_relative_to(temporary)})


def declarations(config: dict) -> list[tuple[Path, dict]]:
    found = [(Path.home(), config)]
    user_file = (codex_home() / "config.toml").resolve()
    for project in project_directories(config):
        path = project / ".codex" / "config.toml"
        if path.resolve() != user_file:
            found.append((project, load_toml(path)))
    return found


def section(config: dict, name: str) -> dict:
    value = config.get(name)
    return value if isinstance(value, dict) else {}


def declared_targets(config: dict) -> tuple[dict[str, dict], list[str]]:
    user = section(config, "marketplaces")
    candidates: dict[str, dict[tuple, dict]] = {}
    ignored = []
    for rank, (directory, declared) in enumerate(declarations(config)):
        marketplaces = section(declared, "marketplaces")
        for identity, settings in sorted(section(declared, "plugins").items()):
            if not isinstance(settings, dict) or settings.get("enabled") is not True or "@" not in identity:
                continue
            name = identity.rsplit("@", 1)[1]
            source = git_marketplace(marketplaces[name] if name in marketplaces else user.get(name))
            if source is None:
                continue
            error = marketplace_error(name, github_repository(source["source"]))
            if error:
                ignored.append(f"{name} declared by {directory}: {error}")
                continue
            option = candidates.setdefault(name, {}).setdefault(
                (source["source"], source["ref"]), {**source, "project": directory, "rank": rank, "plugins": set()})
            option["plugins"].add(identity)
    targets = {}
    for name, options in sorted(candidates.items()):
        chosen = min(options.values(), key=lambda option: (option["rank"] > 0, is_pinned(option["ref"]),
                                                            option["rank"]))
        targets[name] = {"source": chosen["source"], "ref": chosen["ref"], "project": chosen["project"],
                         "plugins": sorted(chosen["plugins"])}
        ignored.extend(f"{name} at {option['ref'] or 'HEAD'} declared by {option['project']}"
                       for option in options.values() if option is not chosen)
    return targets, ignored


def marketplace_manifest(name: str) -> dict:
    root = codex_home() / ".tmp" / "marketplaces" / name
    for relative in (Path(".agents", "plugins", "marketplace.json"), Path(".claude-plugin", "marketplace.json")):
        manifest = read_json(root / relative)
        if manifest:
            return manifest
    return {}


def plugin_path(source: dict) -> str:
    path = source.get("path")
    return str(PurePosixPath(path)) if isinstance(path, str) and path else "."


def plugin_publishers(name: str) -> dict[str, tuple[str, str, str]]:
    publishers = {}
    for entry in marketplace_manifest(name).get("plugins", []):
        source = entry.get("source") if isinstance(entry, dict) else None
        if isinstance(source, dict) and source.get("source") == "git-subdir" and isinstance(source.get("url"), str):
            ref = source.get("sha") or source.get("ref") or "HEAD"
            if isinstance(ref, str):
                publishers[entry.get("name")] = (source["url"], ref, plugin_path(source))
    return publishers


def plugin_sources(name: str, target: dict) -> dict[str, tuple[str, str, str]]:
    sources = plugin_publishers(name)
    for entry in marketplace_manifest(name).get("plugins", []):
        source = entry.get("source") if isinstance(entry, dict) else None
        if isinstance(source, str) or isinstance(source, dict) and source.get("source") == "local":
            path = source if isinstance(source, str) else source.get("path")
            sources[entry.get("name")] = (target["source"], target.get("ref") or "HEAD", plugin_path({"path": path}))
    return sources


def publisher_key(url: object, ref: object = None) -> str | None:
    repository = github_repository(url)
    return f"{repository}@{ref if isinstance(ref, str) and ref else 'HEAD'}" if repository else None


def tree_key(repository: str, sha: str, path: str) -> str:
    return f"{repository}@{sha}:{path}"


def read_contents() -> dict[str, dict[str, str]]:
    saved = read_json(state_directory() / "plugin-heads.json")
    return {name: {key: sha for key, sha in saved.get(name, {}).items() if isinstance(sha, str) and SHA.fullmatch(sha)}
            if isinstance(saved.get(name), dict) else {} for name in ("heads", "trees", "attempts")}


def installed_revisions() -> set[str]:
    installed = read_json(claude_home() / "plugins" / "installed_plugins.json").get("plugins")
    claude = {entry.get("gitCommitSha") for entries in installed.values() if isinstance(entries, list)
              for entry in entries if isinstance(entry, dict)} if isinstance(installed, dict) else set()
    recorded = read_json(state_directory() / "plugin-autoupdate.json").get("revisions")
    codex = {sha for key, sha in recorded.items() if key.startswith("codex-plugin:")} if isinstance(recorded, dict) else set()
    return {sha for sha in claude | codex if isinstance(sha, str)}


def contents_lock():
    path = state_directory() / "plugin-heads.lock"
    deadline = time.monotonic() + CONTENTS_WAIT_SECONDS
    while (lock := acquire_lock(path)) is None:
        if time.monotonic() > deadline:
            raise TimeoutError("plugin-heads.json stayed locked")
        time.sleep(0.05)
    return lock


def record_contents(heads: dict[str, str] = None, trees: dict[str, str] = None, keep: set[str] = frozenset(),
                    attempts: dict[str, str] = None) -> None:
    with contents_lock():
        contents = read_contents()
        contents["heads"].update(heads or {})
        contents["attempts"].update(attempts or {})
        current = set(contents["heads"].values())
        live = current | set(keep) | installed_revisions()
        contents["trees"] = {key: oid for key, oid in {**contents["trees"], **(trees or {})}.items()
                             if key.split("@", 1)[1].split(":", 1)[0] in live}
        contents["attempts"] = {key: sha for key, sha in contents["attempts"].items() if sha in current}
        write_json(state_directory() / "plugin-heads.json", contents)


def same_content(contents: dict, repository: str, path: str, old: object, new: str) -> bool | None:
    if old == new:
        return True
    if not isinstance(old, str):
        return None
    before = contents["trees"].get(tree_key(repository, old, path))
    after = contents["trees"].get(tree_key(repository, new, path))
    return None if before is None or after is None else before == after


def github_query(arguments: list[str]) -> dict:
    import machine_events

    return machine_events.gh(arguments) or {}


def repository_contents(key: str, paths: set[str], installed: set[str], known: dict[str, str],
                        query) -> tuple[dict[str, str], dict[str, str]]:
    repo, ref = key.rsplit("@", 1)
    owner, name = repo.split("/", 1)
    if not (NAME.fullmatch(owner) and NAME.fullmatch(name) and REVISION_PATH.fullmatch(ref)):
        return {}, {}
    parts, fields = [f'h: object(expression: "{ref}") {{ oid }}'], {}
    for path in sorted(path for path in paths if path == "." or REVISION_PATH.fullmatch(path)):
        for revision in [ref, *sorted(sha for sha in installed if SHA.fullmatch(sha))]:
            if revision != ref and tree_key(repo, revision, path) in known:
                continue
            alias = f"t{len(fields)}"
            parts.append(f'{alias}: object(expression: "{revision}:{"" if path == "." else path}") {{ oid }}')
            fields[alias] = (revision, path)
    response = query(["graphql", "-f", f'query={{r: repository(owner: "{owner}", name: "{name}") {{ {" ".join(parts)} }}}}'])
    found = ((response or {}).get("data") or {}).get("r") or {}
    head = (found.get("h") or {}).get("oid")
    if not head:
        return {}, {}
    return {key: head}, {tree_key(repo, head if revision == ref else revision, path): oid
                         for alias, (revision, path) in fields.items() if (oid := (found.get(alias) or {}).get("oid"))}


def github_contents(wanted: dict[str, set[str]], installed: dict[str, set[str]], known: dict[str, str],
                    query) -> tuple[dict[str, str], dict[str, str]]:
    failures = []

    def read(key: str) -> tuple[dict[str, str], dict[str, str]]:
        try:
            return repository_contents(key, wanted[key], installed.get(key, set()), known, query)
        except Exception as error:
            failures.append(f"{key.rsplit('@', 1)[0]} ({error})")
            return {}, {}

    heads, trees = {}, {}
    with ThreadPoolExecutor(max_workers=max(1, min(8, len(wanted)))) as pool:
        for found_heads, found_trees in pool.map(read, sorted(wanted)):
            heads.update(found_heads)
            trees.update(found_trees)
    if failures:
        print(f"plugins: could not read plugin contents of {', '.join(sorted(failures))}; comparing commits instead",
              file=sys.stderr)
    return heads, trees


def refresh_contents(wanted: dict[str, set[str]], installed: dict[str, set[str]], query=None,
                     revision=None, keep: set[str] = frozenset(), known: dict[str, str] = None) -> dict[str, str]:
    heads, trees = github_contents(wanted, installed, read_contents()["trees"], query or github_query)
    heads = {**(known or {}), **heads}
    unread = sorted(key for key in wanted if key not in heads)
    if unread and revision is not None:
        def resolve(key: str) -> str | None:
            repo, ref = key.rsplit("@", 1)
            return revision(f"https://github.com/{repo}.git", ref)

        with ThreadPoolExecutor(max_workers=min(8, len(unread))) as pool:
            heads.update({key: sha for key, sha in zip(unread, pool.map(resolve, unread)) if sha})
    record_contents(heads, trees, {sha for shas in installed.values() for sha in shas} | set(keep))
    return heads


def inspect_installation(name: str, target: dict) -> None:
    cache = codex_home() / "plugins" / "cache" / name
    selected = {identity.rsplit("@", 1)[0] for identity in target["plugins"]}
    target["missing"] = [identity for identity in target["plugins"]
                         if not (cache / identity.rsplit("@", 1)[0]).is_dir()]
    published = plugin_publishers(name)
    target["publishers"] = [{"source": url, "ref": ref}
                            for url, ref in sorted({published[plugin][:2] for plugin in selected if plugin in published})]


def process_alive(pid: object) -> bool:
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    if os.name == "nt":
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            return bool(kernel32.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def codex_session_running(alive=process_alive) -> bool:
    database = state_directory() / "sessions.db"
    if not database.exists():
        return False
    try:
        connection = sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True, timeout=5)
        try:
            rows = connection.execute("SELECT profile, pid FROM sessions WHERE ended_at IS NULL").fetchall()
        finally:
            connection.close()
    except sqlite3.Error:
        return True
    home = os.path.normcase(str(codex_home().resolve()))
    return any(isinstance(profile, str) and os.path.normcase(str(Path(profile).resolve())) == home and alive(pid)
               for profile, pid in rows)


def hook_packages(marketplace: str) -> list[dict]:
    packages = []
    for path in sorted((codex_home() / "plugins" / "cache" / marketplace).glob("*/*/hooks/codex.json")):
        try:
            hooks = json.loads(path.read_text(encoding="utf-8"))["hooks"]
            commands = [hook.get("commandWindows") or hook.get("command") or "" for groups in hooks.values()
                        for group in groups for hook in group["hooks"]]
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            continue
        digests = {match[1] for command in commands if (match := HOOK_DIGEST.search(command))}
        if len(digests) == 1:
            packages.append({"plugin": path.parents[2].name, "root": path.parents[1], "digest": digests.pop(),
                             "snapshot_first": all(SNAPSHOT_FIRST in command for command in commands)})
    return packages


def preserve_hook_snapshots(marketplace: str, packages: list[dict]) -> None:
    for package in packages:
        plugin, root = package["plugin"], package["root"]
        try:
            spec = importlib.util.spec_from_file_location("codex_hook_snapshot_" + plugin,
                                                          root / "hooks" / "codex_hook_snapshot.py")
            loader = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(loader)
            loader.snapshot_root(root, package["digest"], plugin,
                                 codex_home() / "plugins" / "data" / f"{plugin}-{marketplace}" / "hook-snapshots")
        except (Exception, SystemExit):
            continue


def remote_revision(source: str, ref: object = None) -> str | None:
    reference = ref if isinstance(ref, str) and ref else "HEAD"
    if is_pinned(reference):
        return reference.lower()
    try:
        result = subprocess.run(["git", "ls-remote", source, reference], capture_output=True, text=True,
                                encoding="utf-8", errors="replace", timeout=GIT_TIMEOUT_SECONDS, check=False,
                                creationflags=NO_WINDOW)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode:
        return None
    for line in result.stdout.splitlines():
        revision = line.split("\t", 1)[0].strip()
        if len(revision) == 40:
            return revision
    return None


def run_cli(command: list[str], *, cwd: Path) -> bool | str:
    try:
        result = subprocess.run(command, cwd=cwd, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                                encoding="utf-8", errors="replace", timeout=CLI_TIMEOUT_SECONDS, check=False,
                                creationflags=NO_WINDOW)
    except (OSError, subprocess.TimeoutExpired):
        return False
    if result.returncode and "was not found in marketplace" in (result.stderr + result.stdout):
        return RETIRED
    return result.returncode == 0


def refresh_codex(executable: str, targets: dict[str, dict], recorded: dict, revision=remote_revision,
                  run=run_cli, now: float | None = None, busy=codex_session_running, packages=hook_packages,
                  preserve=preserve_hook_snapshots, sources=plugin_sources, query=None) -> dict[str, str]:
    now = time.time() if now is None else now
    outcomes: dict[str, str] = {}
    references = sorted({(source["source"], source.get("ref") or "HEAD")
                         for target in targets.values() for source in [target, *target.get("publishers", [])]})
    with ThreadPoolExecutor(max_workers=max(1, min(8, len(references)))) as pool:
        heads = dict(zip(references, pool.map(lambda source: revision(*source), references)))
    running = None
    for marketplace in sorted(targets):
        target = targets[marketplace]
        key = "codex:" + marketplace
        published = [heads[(source["source"], source.get("ref") or "HEAD")]
                     for source in [target, *target.get("publishers", [])]]
        if published[0] is None:
            outcomes[marketplace] = "unreachable"
            continue
        head = (hashlib.sha256(json.dumps(published).encode("utf-8")).hexdigest()
                if len(published) > 1 else published[0])
        missing = set(target.get("missing", [])) - set(recorded.get("retired:" + key, []))
        if None not in published and recorded.get(key) == head and not missing:
            outcomes[marketplace] = "current"
            continue
        failed_at = recorded.get("failed:" + key)
        if isinstance(failed_at, (int, float)) and now - failed_at < FAILURE_BACKOFF_SECONDS:
            outcomes[marketplace] = "failed recently; retried after 24 hours"
            continue
        hooks = packages(marketplace)
        cache_first = not all(package["snapshot_first"] for package in hooks)
        if cache_first:
            running = busy() if running is None else running
        preserve(marketplace, hooks)
        refreshed = run([executable, "plugin", "marketplace", "upgrade", marketplace],
                        cwd=target["project"]) is True
        listed = sources(marketplace, target) if refreshed else {}
        selected = {plugin: listed[plugin.rsplit("@", 1)[0]] for plugin in target["plugins"]
                    if plugin.rsplit("@", 1)[0] in listed}
        contents = read_contents()
        own = {sha for name, sha in recorded.items() if name.startswith("codex-plugin:") and isinstance(sha, str)}
        wanted, installed, cached = {}, {}, {}
        for plugin, (url, ref, path) in selected.items():
            previous, plugin_head = recorded.get("codex-plugin:" + plugin), heads.get((url, ref))
            publisher, repository = publisher_key(url, ref), github_repository(url)
            if not publisher or previous == plugin_head:
                continue
            if plugin_head and all(tree_key(repository, revision, path) in contents["trees"]
                                   for revision in (plugin_head, previous) if isinstance(revision, str)):
                cached[publisher] = plugin_head
                continue
            wanted.setdefault(publisher, set()).add(path)
            if isinstance(previous, str):
                installed.setdefault(publisher, set()).add(previous)
        if wanted or cached:
            try:
                refresh_contents(wanted, installed, query, keep=own, known=cached)
            except OSError as error:
                print(f"plugins: could not record plugin contents ({error})", file=sys.stderr)
            contents = read_contents()
        retired = []
        for plugin in target["plugins"] if refreshed else []:
            plugin_key = "codex-plugin:" + plugin
            source = selected.get(plugin)
            plugin_head = heads.get(source[:2]) if source else None
            if (plugin_head is not None and plugin not in missing and same_content(
                    contents, github_repository(source[0]), source[2], recorded.get(plugin_key), plugin_head)):
                recorded[plugin_key] = plugin_head
                continue
            added = run([executable, "plugin", "add", plugin], cwd=target["project"])
            if added == RETIRED:
                retired.append(plugin)
            elif added is not True:
                refreshed = False
                break
            elif plugin_head is not None:
                recorded[plugin_key] = plugin_head
        if retired:
            recorded["retired:" + key] = sorted(retired)
        retired = f" (no longer published, not installed: {', '.join(retired)})" if retired else ""
        if refreshed:
            if None not in published:
                recorded[key] = head
            recorded.pop("failed:" + key, None)
            outcomes[marketplace] = "updated" + retired + (
                "; restart Codex sessions opened before this update to reload their hooks" if cache_first and running else "")
        else:
            recorded["failed:" + key] = now
            outcomes[marketplace] = "failed" + retired
    return outcomes


def enabled_identities(config: dict) -> set[str]:
    return {identity for identity, settings in section(config, "plugins").items()
            if isinstance(settings, dict) and settings.get("enabled") is True}


def remove_user_plugins(path: Path, identities: set[str]) -> None:
    if not path.is_file():
        return
    original = path.read_text(encoding="utf-8")
    headers = {f"[plugins.{json.dumps(identity)}]" for identity in identities}
    kept, skipping = [], False
    for line in original.splitlines(keepends=True):
        if line.lstrip().startswith("["):
            skipping = line.strip() in headers
        if not skipping:
            kept.append(line)
    text = re.sub(r"\n{3,}", "\n\n", "".join(kept))
    if text != original:
        write_text(path, text)


def publishes(target: dict, repositories: set[str]) -> bool:
    return any(github_repository(source["source"]) in repositories for source in [target, *target["publishers"]])


def refresh_declared(executable: str, config: dict, recorded: dict, previous: list[str] = (),
                     repositories: set[str] | None = None, **options) -> dict:
    targets, ignored = declared_targets(config)
    for name, target in targets.items():
        inspect_installation(name, target)
    declared = set().union(*(enabled_identities(layer) for _, layer in declarations(config)[1:]))
    project_only = declared - (enabled_identities(config) - set(previous))
    selected = {name: target for name, target in targets.items()
                if repositories is None or publishes(target, repositories)}
    outcomes = refresh_codex(executable, selected, recorded, **options)
    remove_user_plugins(codex_home() / "config.toml", project_only)
    kept = {prefix + "codex:" + name for name in targets for prefix in ("", "retired:", "failed:")}
    kept |= {"codex-plugin:" + identity for target in targets.values() for identity in target["plugins"]}
    for key in [key for key in recorded if key not in kept]:
        del recorded[key]
    return {"marketplaces": outcomes, "ignored": ignored, "project_only": sorted(project_only)}


def load_codex_config() -> dict:
    return load_toml(codex_home() / "config.toml")


def installed_plugin_roots(plugin: str, claude_config: Path, codex_config: dict) -> list[Path]:
    roots = []
    prefix = plugin + "@"
    settings = read_json(claude_config / "settings.json").get("enabledPlugins")
    installed = read_json(claude_config / "plugins" / "installed_plugins.json").get("plugins")
    if isinstance(settings, dict) and isinstance(installed, dict):
        for identity, on in settings.items():
            entries = installed.get(identity)
            if on is True and identity.startswith(prefix) and isinstance(entries, list):
                roots.extend(Path(entry["installPath"]) for entry in entries
                             if isinstance(entry, dict) and isinstance(entry.get("installPath"), str))
    plugins = codex_config.get("plugins")
    if isinstance(plugins, dict):
        for identity, settings in plugins.items():
            if identity.startswith(prefix) and isinstance(settings, dict) and settings.get("enabled") is True:
                roots.append(codex_home() / "plugins" / "cache" / identity.split("@", 1)[1] / plugin / "local")
    return roots


def newest_existing(paths: list[Path]) -> Path | None:
    existing = [path for path in paths if path.is_file()]
    return max(existing, key=lambda path: path.stat().st_mtime) if existing else None


def console_python() -> str:
    executable = Path(sys.executable)
    console = executable.with_name("python.exe")
    return str(console) if executable.name.lower() == "pythonw.exe" and console.is_file() else sys.executable


def sweep_script(codex_config: dict) -> Path | None:
    relative = Path(".agents", "engineering", "workflow", "merge", "scripts", "worktree_sweep.py")
    return newest_existing([root / relative for root in installed_plugin_roots("engineering", claude_home(),
                                                                                codex_config)])


def sweep_worktrees(codex_config: dict) -> str:
    script = sweep_script(codex_config)
    if script is None:
        return "engineering package not installed"
    try:
        result = subprocess.run([console_python(), "-B", str(script)], stdin=subprocess.DEVNULL,
                                capture_output=True, text=True, encoding="utf-8", errors="replace",
                                timeout=SWEEP_TIMEOUT_SECONDS, check=False, creationflags=NO_WINDOW)
    except (OSError, subprocess.TimeoutExpired) as error:
        return f"failed: {error}"
    return "ran" if result.returncode == 0 else f"failed with exit code {result.returncode}"


def packaged_copies(codex_config: dict) -> list[Path]:
    relative = Path("resources", "machine", "scripts", SCRIPT_NAME)
    return [root / relative for root in installed_plugin_roots("machine", claude_home(), codex_config)
            if (root / relative).is_file()]


def self_update(current: Path, codex_config: dict) -> bool:
    candidates = [path for path in packaged_copies(codex_config) if path.resolve() != current.resolve()
                  and all(path.with_name(name).is_file() for name in RUNTIME)]
    if not candidates:
        return False
    newest = max(candidates, key=lambda path: path.stat().st_mtime)
    try:
        changed = False
        for name in RUNTIME:
            source, target = newest.with_name(name), current.with_name(name)
            if target.is_file() and source.read_bytes() == target.read_bytes():
                continue
            shutil.copyfile(source, target.with_name(target.name + ".next"))
            os.replace(target.with_name(target.name + ".next"), target)
            changed = True
    except OSError:
        return False
    return changed


def acquire_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    stream = path.open("a+b")
    try:
        if os.name == "nt":
            import msvcrt

            stream.seek(0)
            if not stream.read(1):
                stream.write(b"0")
                stream.flush()
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        stream.close()
        return None
    return stream


def main(argv: list[str] | None = None) -> int:
    if sys.version_info < (3, 11):
        print("plugin_autoupdate requires Python 3.11 or newer", file=sys.stderr)
        return 2
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--no-self-update", action="store_true")
    parser.add_argument("--no-sweep", action="store_true")
    parser.add_argument("--retry-failures", action="store_true")
    parser.add_argument("--repository", action="append", type=str.casefold,
                        help="Refresh only marketplaces that list or publish from this owner/name")
    arguments = parser.parse_args(argv)
    directory = state_directory()
    lock = acquire_lock(directory / "plugin-autoupdate.lock")
    if lock is None:
        return 1
    try:
        state_file = directory / "plugin-autoupdate.json"
        state = read_json(state_file)
        recorded = state.get("revisions") if isinstance(state.get("revisions"), dict) else {}
        if arguments.retry_failures:
            recorded = {key: value for key, value in recorded.items() if not key.startswith("failed:")}
        previous = state.get("project_only") if isinstance(state.get("project_only"), list) else []
        config = load_codex_config()
        executable = shutil.which("codex")
        repositories = set(arguments.repository) if arguments.repository else None
        outcomes = refresh_declared(executable, config, recorded, previous, repositories) if executable else {}
        sweep = "skipped" if arguments.no_sweep else sweep_worktrees(config)
        write_json(state_file, {"revisions": recorded, "project_only": outcomes.get("project_only", previous),
                                "last_run": time.time(), "outcomes": outcomes, "worktree_sweep": sweep})
        if not arguments.no_self_update:
            self_update(Path(__file__), load_codex_config())
    finally:
        lock.close()
    results = outcomes.get("marketplaces", {})
    return int(any(str(value).startswith(("failed", "unreachable")) for value in results.values()))


if __name__ == "__main__":
    raise SystemExit(main())
