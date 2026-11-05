from __future__ import annotations

import argparse
import atexit
import hashlib
import json
import os
import queue
import re
import shlex
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

if __name__ == "__main__":
    _source_cache = tempfile.TemporaryDirectory(prefix="tj-source-cache-")
    atexit.register(_source_cache.cleanup)
    sys.pycache_prefix = _source_cache.name
    sys.dont_write_bytecode = True

import host_plugins
import plugin_autoupdate
import plugin_refresh
import required_plugins
from host import Host
from tj_agent import content
from tj_agent.plugins import sources

PRODUCERS = frozenset({"tj-agents/agent", "tj-agents/core"})
REVISION = re.compile(r"[0-9a-f]{40}\Z")
MAX_AGE = 3600
VERIFYING: set[tuple[str, str, Host]] = set()
GUARD = "managed_admission.py"


class AdmissionError(RuntimeError):
    pass


def read(path: Path) -> dict[str, Any]:
    value: object = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise AdmissionError(f"Invalid admission object: {path}")
    return value


def package_root(script: Path) -> Path:
    for root in script.resolve().parents:
        if (root / "harness.json").is_file():
            return root
    raise AdmissionError("Executing admission guard has no package root")


def receipt_path(
    project: Path,
    host: Host,
    *,
    native_project: Path | None = None,
    session: str = "",
) -> Path:
    native_project = (native_project or project).resolve()
    key = hashlib.sha256(
        (
            str(project.resolve())
            + "\0"
            + str(native_project)
            + "\0"
            + host.value
            + "\0"
            + session
        ).encode()
    ).hexdigest()
    return Path(plugin_autoupdate.state_directory()) / "admission" / (key + ".json")


def signatures(paths: list[Path]) -> dict[str, list[int]]:
    result: dict[str, list[int]] = {}
    for root in paths:
        if root.is_symlink() or getattr(root, "is_junction", lambda: False)():
            raise AdmissionError(f"Admission identity contains a link: {root}")
        root = root.resolve()
        entries = [root, *root.rglob("*")] if root.is_dir() else [root]
        for path in entries:
            if path.is_symlink() or getattr(path, "is_junction", lambda: False)():
                raise AdmissionError(f"Admission identity contains a link: {path}")
            if (
                ".git" in path.relative_to(root).parts
                or plugin_refresh.IN_USE in path.parts
                or (path.is_dir() and path.name == "__pycache__")
                or plugin_refresh.generated_cache(path)
            ):
                continue
            try:
                stat = path.stat()
                result[str(path)] = [stat.st_dev, stat.st_ino, stat.st_mode]
                if not path.is_dir():
                    if path.name == ".snapshot-digest":
                        result[str(path)].extend(
                            [
                                stat.st_size,
                                int.from_bytes(
                                    hashlib.sha256(path.read_bytes()).digest(),
                                    byteorder="big",
                                ),
                            ]
                        )
                    else:
                        result[str(path)].extend(
                            [stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns]
                        )
            except FileNotFoundError:
                result[str(path)] = []
    return result


def configuration_paths(project: Path, host: Host) -> list[Path]:
    return [
        *(root / ".agents/plugins.json" for root in (project, *project.parents)),
        *host_plugins.PLUGINS[host].configuration_paths(project),
    ]


def managed_scope(project: Path, required: tuple[str, ...] | None = None) -> bool:
    required = required if required is not None else required_plugins.selected(
        required_plugins.project_root(project)
    )
    return any(identity.endswith("@tj-agents") for identity in required)


def pinned_sources(
    catalog: Mapping[str, Any],
) -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
    entries: dict[str, dict[str, Any]] = {}
    revisions: dict[str, set[str]] = {producer: set() for producer in PRODUCERS}
    plugins = catalog.get("plugins")
    if not isinstance(plugins, list):
        raise AdmissionError("Selected marketplace has no plugin roster")
    for entry in plugins:
        if not isinstance(entry, dict) or not isinstance(entry.get("source"), dict):
            continue
        source = entry["source"]
        producer = plugin_refresh.repository(source.get("url"))
        if producer not in PRODUCERS:
            continue
        revision = source.get("sha")
        name = entry.get("name")
        if (
            source.get("source") != "git-subdir"
            or not isinstance(revision, str)
            or not REVISION.fullmatch(revision)
            or "ref" in source
            or not isinstance(name, str)
            or source.get("path") != f"./plugins/{name}"
            or name in entries
        ):
            raise AdmissionError(
                "Selected Agent/Core catalog must declare unambiguous immutable sources"
            )
        revisions[producer].add(revision)
        entries[name] = source
    if any(len(values) != 1 for values in revisions.values()):
        raise AdmissionError(
            "Selected marketplace must freeze one Agent/Core publication pair"
        )
    return entries, {
        producer: next(iter(values)) for producer, values in revisions.items()
    }


def guard_ready(root: Path, host: Host) -> bool:
    harness = read(root / "harness.json")
    hooks = harness.get("requires", {}).get("hooks", [])
    declared = any(
        isinstance(hook, dict)
        and str(hook.get("path", "")).endswith(GUARD)
        and host.value in hook.get("hosts", [])
        for hook in hooks
    )
    if not declared:
        return False
    manifest = read(root / "hooks" / (host.value + ".json"))
    for event in ("SessionStart", "UserPromptSubmit", "PreToolUse"):
        commands = [
            hook
            for group in manifest.get("hooks", {}).get(event, [])
            for hook in group.get("hooks", [])
        ]
        if not any(
            GUARD in json.dumps(hook) and not hook.get("async") for hook in commands
        ):
            return False
    return True


def selected_closure(required: tuple[str, ...], roster: dict[str, Any]) -> set[str]:
    graph: dict[str, list[str]] = {}
    plugins = roster.get("plugins")
    if not isinstance(plugins, list):
        raise AdmissionError("Invalid frozen marketplace dependency graph")
    for entry in plugins:
        if not isinstance(entry, dict):
            raise AdmissionError("Invalid frozen marketplace dependency graph")
        name = entry.get("name")
        dependencies = entry.get("requires", [])
        if (
            not isinstance(name, str)
            or name in graph
            or not isinstance(dependencies, list)
            or any(not isinstance(value, str) for value in dependencies)
        ):
            raise AdmissionError("Invalid frozen marketplace dependency graph")
        graph[name] = dependencies
    pending = [
        identity.split("@", 1)[0]
        for identity in required
        if identity.endswith("@tj-agents")
    ]
    selected: set[str] = set()
    while pending:
        name = pending.pop()
        if name in selected:
            continue
        if name not in graph:
            raise AdmissionError(
                "Required selected package is absent from the frozen marketplace"
            )
        selected.add(name)
        pending.extend(graph[name])
    return {name + "@tj-agents" for name in selected}


def ensure_hooks(project: Path, host: Host, executable: str | None = None) -> None:
    required = required_plugins.selected(required_plugins.project_root(project))
    managed = managed_scope(project, required)
    try:
        reason = host_plugins.PLUGINS[host].ensure_hooks(project, executable)
        if managed and reason:
            raise AdmissionError("Required hook adoption is pending: " + reason)
    except (RuntimeError, OSError) as error:
        if managed:
            raise AdmissionError(
                f"Managed hook readiness could not be established: {error}"
            ) from error
        raise


def verify(
    project: Path,
    host: Host,
    executable: str | None = None,
    *,
    native_project: Path | None = None,
) -> dict[str, Any] | None:
    native_project = (native_project or project).resolve()
    project = required_plugins.project_root(project)
    required = required_plugins.selected(project)
    if not managed_scope(project, required):
        return None
    key = (str(project), str(native_project), host)
    if key in VERIFYING:
        return None
    VERIFYING.add(key)
    try:
        try:
            return cached(project, host, native_project)
        except (AdmissionError, OSError, ValueError, KeyError, TypeError):
            pass
        plugins = host_plugins.PLUGINS[host]
        binary = executable or plugins.resolve_executable()
        location = plugins.marketplace_location(binary, native_project, "tj-agents")
        if not location:
            raise AdmissionError("Selected marketplace checkout is unavailable")
        marketplace = Path(location).resolve()
        revision = plugin_refresh.run(
            [
                "git",
                "--no-replace-objects",
                "-C",
                str(marketplace),
                "rev-parse",
                "HEAD",
            ],
            native_project,
        ).strip()
        if not REVISION.fullmatch(revision):
            raise AdmissionError("Selected marketplace is not at an immutable revision")
        catalog_path = marketplace / plugins.CATALOG
        expected_catalog = plugin_refresh.run(
            [
                "git",
                "--no-replace-objects",
                "-C",
                str(marketplace),
                "show",
                f"{revision}:{plugins.CATALOG}",
            ],
            native_project,
        )
        catalog = read(catalog_path)
        if catalog != json.loads(expected_catalog):
            raise AdmissionError(
                "Selected marketplace catalog differs from its frozen revision"
            )
        roster = json.loads(
            plugin_refresh.run(
                [
                    "git",
                    "--no-replace-objects",
                    "-C",
                    str(marketplace),
                    "show",
                    f"{revision}:plugins.json",
                ],
                native_project,
            )
        )
        if not isinstance(roster, dict):
            raise AdmissionError("Invalid frozen marketplace dependency graph")
        required_closure = selected_closure(required, roster)
        names = {
            entry.get("name")
            for entry in catalog.get("plugins", [])
            if isinstance(entry, dict)
        }
        if any(identity.split("@", 1)[0] not in names for identity in required_closure):
            raise AdmissionError(
                "Required selected package is absent from the frozen marketplace"
            )
        affected = {
            entry.get("name")
            for entry in catalog.get("plugins", [])
            if isinstance(entry, dict)
            and isinstance(entry.get("source"), dict)
            and plugin_refresh.repository(entry["source"].get("url")) in PRODUCERS
        }
        relevant = {
            identity
            for identity in required_closure
            if identity.split("@", 1)[0] in affected
        }
        if not relevant:
            return None
        entries, pair = pinned_sources(catalog)
        remote = plugin_refresh.run(
            [
                "git",
                "ls-remote",
                "https://github.com/tj-agents/marketplace.git",
                "refs/heads/main",
            ],
            native_project,
        ).split()
        if not remote or revision != remote[0]:
            raise AdmissionError(
                "Selected marketplace checkout is stale; run plugin maintenance"
            )
        catalog_watch = [
            marketplace / ".git/HEAD",
            marketplace / ".git/packed-refs",
            marketplace / ".git/refs",
            catalog_path,
        ]
        catalog_proof = signatures(catalog_watch)
        native = required_plugins.inventory(
            binary,
            plugins,
            native_project,
            (),
            "tj-agents" if host is Host.CODEX else None,
        )
        observed: dict[str, int] = {}
        for entry in plugins.installations(native_project, native):
            if not isinstance(entry, dict) or entry.get("enabled") is not True:
                continue
            located = plugins.location(entry, native_project)
            if (
                located is not None
                and located[0].endswith("@tj-agents")
                and located[0].split("@", 1)[0] in entries
            ):
                observed[located[0]] = observed.get(located[0], 0) + 1
        roots = required_plugins.enabled_roots(plugins, native_project, native)
        missing = relevant - roots.keys()
        if missing:
            raise AdmissionError(
                "Selected publication is only partially installed: "
                + ", ".join(sorted(missing))
            )
        reason = plugins.ensure_hooks(
            native_project,
            binary,
            required_roots={identity: roots[identity] for identity in sorted(relevant)},
        )
        if reason:
            raise AdmissionError("Required hook adoption is pending: " + reason)
        configuration_watch = configuration_paths(native_project, host)
        configuration_proof = signatures(configuration_watch)
        installed: dict[str, dict[str, str]] = {}
        guards: list[str] = []
        package_proof: dict[str, list[int]] = {}
        with tempfile.TemporaryDirectory(prefix="tj-admission-") as temporary:
            publishers: dict[Any, Any] = {}
            trees = {
                identity: sources.tree(
                    entries[identity.split("@", 1)[0]],
                    marketplace,
                    project,
                    Path(temporary),
                    publishers,
                )
                for identity in relevant
            }
            if any(observed.get(identity, 0) > 1 for identity in trees):
                raise AdmissionError(
                    "Selected publication has ambiguous native installations"
                )
            missing = trees.keys() - roots.keys()
            if missing:
                raise AdmissionError(
                    "Selected publication is only partially installed: "
                    + ", ".join(sorted(missing))
                )
            for identity in sorted(trees):
                name = identity.split("@", 1)[0]
                root = roots[identity]
                before = signatures([root])
                root = root.resolve()
                expected = trees[identity]
                if plugin_refresh.package_tree(root) != expected:
                    raise AdmissionError(
                        "Installed package differs from selected publication: "
                        + identity
                    )
                if signatures([root]) != before:
                    raise AdmissionError(
                        "Package changed during admission: " + identity
                    )
                package_proof.update(before)
                installed[identity] = {
                    "root": str(root),
                    "digest": content.digest(expected),
                }
                if name in {"agent", "base", "workflow", "engineering", "docs"}:
                    if not guard_ready(root, host):
                        raise AdmissionError(
                            "Required synchronous admission guards unavailable: "
                            + identity
                        )
                    guards.append(identity)
        watch = [
            *catalog_watch,
            *configuration_watch,
            *(Path(value["root"]) for value in installed.values()),
        ]
        proof = {**catalog_proof, **configuration_proof, **package_proof}
        if signatures(watch) != proof:
            raise AdmissionError("Publication identity changed during admission")
        document: dict[str, Any] = {
            "version": 1,
            "project": str(project),
            "native_project": str(native_project),
            "host": host.value,
            "marketplace": {"root": str(marketplace), "revision": revision},
            "producers": pair,
            "packages": installed,
            "guards": guards,
            "checked": time.time(),
            "watch": [str(path) for path in watch],
        }
        document["signatures"] = proof
        plugin_autoupdate.write_json(
            receipt_path(project, host, native_project=native_project), document
        )
        return document
    except AdmissionError:
        raise
    except (
        OSError,
        ValueError,
        RuntimeError,
        KeyError,
        TypeError,
        subprocess.SubprocessError,
    ) as error:
        raise AdmissionError(
            f"Managed publication admission failed: {error}"
        ) from error
    finally:
        VERIFYING.remove(key)


def cached(
    project: Path, host: Host, native_project: Path | None = None
) -> dict[str, Any]:
    native_project = (native_project or project).resolve()
    project = required_plugins.project_root(project)
    document = read(receipt_path(project, host, native_project=native_project))
    if (
        document.get("version") != 1
        or document.get("project") != str(project)
        or document.get("native_project") != str(native_project)
        or document.get("host") != host.value
        or not 0 <= time.time() - document["checked"] < MAX_AGE
        or signatures([Path(path) for path in document["watch"]])
        != document.get("signatures")
    ):
        raise AdmissionError(
            "Managed admission expired or package/hook identity changed. "
            "Run native plugin maintenance, then agent:refresh-plugins."
        )
    return document


def executing_identity(document: Mapping[str, Any], root: Path) -> str:
    identity = read(root / "harness.json").get("plugin", "")
    marketplace, _, name = identity.partition("/")
    identity = f"{name}@{marketplace}"
    admitted = document["packages"].get(identity)
    if not isinstance(admitted, dict):
        raise AdmissionError("Executing package is outside the admitted selection")
    if root.resolve() != Path(admitted["root"]).resolve():
        marker = root / ".snapshot-digest"
        if not marker.is_file() or not re.fullmatch(
            r"sha256:[0-9a-f]{64}", marker.read_text(encoding="utf-8")
        ):
            raise AdmissionError("Executing package is an old or unadmitted snapshot")
        files = plugin_refresh.package_tree(root)
        files.pop(".snapshot-digest", None)
        if content.digest(files) != admitted["digest"]:
            raise AdmissionError(
                "Executing snapshot bytes differ from the admitted package"
            )
    return identity


def gate(project: Path, host: Host, session: str, event: str, root: Path) -> None:
    native_project = project.resolve()
    project = required_plugins.project_root(project)
    if not managed_scope(project):
        return
    if not session:
        raise AdmissionError(
            "Native session identity is unavailable for managed admission"
        )
    document = cached(project, host, native_project)
    declared = read(root / "harness.json").get("plugin", "")
    marketplace, _, name = declared.partition("/")
    identity = f"{name}@{marketplace}"
    path = receipt_path(
        project,
        host,
        native_project=native_project,
        session=session + "@" + identity,
    )
    if event == "SessionStart":
        proof = signatures([root])
        executing_identity(document, root)
        if signatures([root]) != proof:
            raise AdmissionError("Executing package changed during session admission")
        bound = {
            "admission": document,
            "session": session,
            "root": str(root.resolve()),
            "signature": proof,
        }
        plugin_autoupdate.write_json(path, bound)
        return
    for guard in document["guards"]:
        record = read(
            receipt_path(
                project,
                host,
                native_project=native_project,
                session=session + "@" + guard,
            )
        )
        if (
            record.get("session") != session
            or record.get("admission") != document
            or signatures([Path(record["root"])]) != record.get("signature")
        ):
            raise AdmissionError(
                "Native session has not adopted every required guard: " + guard
            )
    bound = read(path)
    if bound.get("root") != str(root.resolve()):
        raise AdmissionError("Executing guard does not match this session binding")


def denial(event: str, message: str) -> dict[str, Any]:
    if event == "PreToolUse":
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": message,
            }
        }
    return {"decision": "block", "reason": message}


def maintenance_command(project: Path, host: Host) -> str:
    arguments = [
        sys.executable,
        "-B",
        str(Path(__file__).resolve().with_name("reload_plugins.py")),
        "--host",
        host.value,
        "--project",
        str(required_plugins.project_root(project)),
        "--verify",
    ]
    return (
        subprocess.list2cmdline(arguments) if os.name == "nt" else shlex.join(arguments)
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("operation", choices=("setup", "hook"))
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--host", type=Host, choices=list(Host))
    parser.add_argument("--project", type=Path, default=Path.cwd())
    args = parser.parse_args()
    try:
        if args.operation == "setup":
            hosts = (
                [args.host]
                if args.host
                else [
                    host
                    for host in Host
                    if Path(__file__)
                    .resolve()
                    .is_relative_to(host.configuration_root.resolve())
                ]
            )
            if not hosts:
                raise AdmissionError("Setup requires an explicit native host")
            for host in hosts:
                document = verify(args.project, host)
                if document is not None:
                    executing_identity(document, package_root(Path(__file__)))
            return 0
        payload: dict[str, Any] = {}
        value: object = json.load(sys.stdin)
        if not isinstance(value, dict):
            raise AdmissionError("Admission hook payload must be an object")
        payload = value
        host = args.host
        if host is None:
            raise AdmissionError("Admission hook requires a host")
        event = payload.get("hook_event_name", "")
        project = Path(payload.get("cwd") or args.project)
        session = payload.get("session_id") or os.environ.get(host.session_variable, "")
        outcome: queue.Queue[Exception | None] = queue.Queue()

        def run_gate() -> None:
            try:
                gate(project, host, session, event, package_root(Path(__file__)))
                outcome.put(None)
            except (AdmissionError, OSError, ValueError, KeyError, TypeError) as error:
                outcome.put(error)

        threading.Thread(target=run_gate, daemon=True).start()
        try:
            error = outcome.get(timeout=8)
        except queue.Empty as timeout:
            raise AdmissionError(
                "Managed admission exceeded its verification budget. "
                "Run native plugin maintenance, then agent:refresh-plugins."
            ) from timeout
        if error is not None:
            raise error
        return 0
    except (AdmissionError, OSError, ValueError, KeyError, TypeError) as error:
        if args.operation == "hook":
            event = (
                payload.get("hook_event_name", "")
                if "payload" in locals()
                else "UserPromptSubmit"
            )
            message = str(error)
            if args.host is not None:
                location = payload.get("cwd")
                project = Path(location) if isinstance(location, str) else args.project
                message += " Run from a terminal: " + maintenance_command(
                    project, args.host
                )
            print(json.dumps(denial(event, message)))
            return 0 if event in {"UserPromptSubmit", "PreToolUse"} else 2
        print(str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
