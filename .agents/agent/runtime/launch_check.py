#!/usr/bin/env python3
"""Apply plugin updates published since the last check, then return (Python 3.11+).

One non-blocking poll of the machine event topic, then a file-only comparison of this project's Claude
installs with the recorded publisher heads. A process starts only for an install that is behind.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import socket
import sys
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))

SERVER = "ntfy.sh"
POLL_TIMEOUT_SECONDS = 3
MAX_RESPONSE = 1 << 20
EXPIRY_SECONDS = 11 * 60 * 60
HOSTS = ("claude", "codex")
HOST_VARIABLE = "TJ_AGENTS_LAUNCH_HOST"
TOPIC = re.compile(r"[A-Za-z0-9_-]{32,128}")
MESSAGE_ID = re.compile(r"[A-Za-z0-9]{1,64}")


def state_directory() -> Path:
    return Path(os.environ.get("AGENT_STATE_DIRECTORY") or Path.home() / ".agents-state")


def read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def https_get(path: str) -> bytes:
    connected = {}

    def connect() -> None:
        try:
            connected["socket"] = socket.create_connection((SERVER, 443), timeout=POLL_TIMEOUT_SECONDS)
        except OSError as error:
            connected["error"] = error

    worker = threading.Thread(target=connect, daemon=True)
    worker.start()
    import ssl

    context = ssl.create_default_context()
    worker.join(POLL_TIMEOUT_SECONDS)
    if "socket" not in connected:
        raise connected.get("error") or TimeoutError("event server did not answer")
    response = bytearray()
    with context.wrap_socket(connected["socket"], server_hostname=SERVER) as stream:
        stream.sendall(f"GET {path} HTTP/1.0\r\nHost: {SERVER}\r\nUser-Agent: tj-agents-launch-check\r\n\r\n"
                       .encode("ascii"))
        while len(response) <= MAX_RESPONSE + 65536 and (chunk := stream.recv(65536)):
            response += chunk
    head, _, body = bytes(response).partition(b"\r\n\r\n")
    status = head.split(b" ", 2)[1:2]
    if status != [b"200"]:
        raise OSError(f"event server answered {b''.join(status).decode('ascii', 'replace') or 'nothing'}")
    return body


def poll(topic: str, since: str, fetch=https_get) -> list[dict]:
    body = fetch(f"/{topic}/json?poll=1&since={since}")
    if len(body) > MAX_RESPONSE:
        raise ValueError("event backlog exceeds the poll limit")
    envelopes = []
    for line in body.splitlines():
        try:
            envelope = json.loads(line)
        except ValueError:
            continue
        if (isinstance(envelope, dict) and envelope.get("event") == "message"
                and isinstance(envelope.get("id"), str) and MESSAGE_ID.fullmatch(envelope["id"])):
            envelopes.append(envelope)
    return envelopes


def update_host(host: str, repositories: set[str] | None, project: Path, updated: set[str] | None = None) -> bool:
    try:
        if host == "codex":
            import plugin_autoupdate

            arguments = ["--no-sweep", "--no-self-update", "--retry-failures"]
            for repository in sorted(repositories or ()):
                arguments += ["--repository", repository]
            return plugin_autoupdate.main(arguments) == 0
        import reload_plugins

        return reload_plugins.refresh_repositories(project, repositories, updated)
    except Exception as error:
        print(f"plugins: {host} update failed: {error}", file=sys.stderr)
        return False


def apply(envelopes: list[dict], cursor: dict, path: Path, project: Path, update, now: float,
         expired: bool = False) -> str:
    import machine_events
    import plugin_autoupdate

    lock = plugin_autoupdate.acquire_lock(path.with_name("launch-check.lock"))
    if lock is None:
        return "busy"
    try:
        identifiers = [envelope["id"] for envelope in envelopes]
        last = identifiers[-1]
        hosts = cursor.get("hosts") if isinstance(cursor.get("hosts"), dict) else {}
        work: dict[str, set[str] | None] = {}
        for host in HOSTS:
            state = hosts.get(host) if isinstance(hosts.get(host), dict) else {}
            applied = state.get("applied")
            start = identifiers.index(applied) + 1 if applied in identifiers else 0
            repositories = None if expired else {repository.casefold() for envelope in envelopes[start:]
                                                  if (repository := machine_events.parse_hint(envelope))}
            if repositories is not None and not repositories:
                hosts[host] = {"applied": last}
            else:
                work[host] = repositories
        if work:
            from concurrent.futures import ThreadPoolExecutor

            with ThreadPoolExecutor(max_workers=len(work)) as pool:
                results = dict(zip(work, pool.map(lambda host: update(host, work[host], project), work)))
            for host, succeeded in results.items():
                previous = hosts.get(host) if isinstance(hosts.get(host), dict) else {}
                hosts[host] = {"applied": last} if succeeded else {"applied": previous.get("applied")}
        if all(hosts[host].get("applied") == last for host in HOSTS):
            plugin_autoupdate.write_json(path, {"since": last, "since_at": now})
        else:
            saved = {"since": cursor.get("since", "all"), "hosts": hosts}
            if isinstance(cursor.get("since_at"), (int, float)):
                saved["since_at"] = cursor["since_at"]
            plugin_autoupdate.write_json(path, saved)
        if work and Path(__file__).resolve().parent == (state_directory() / "bin").resolve():
            plugin_autoupdate.self_update(Path(plugin_autoupdate.__file__), plugin_autoupdate.load_codex_config())
    finally:
        lock.close()
    if not work:
        return "current"
    failed = sorted(host for host, succeeded in results.items() if not succeeded)
    scopes = [repositories for repositories in work.values() if repositories]
    named = ", ".join(sorted(set().union(*scopes))) if scopes else "a full comparison"
    if failed:
        print(f"plugins: {', '.join(failed)} update from {named} failed", file=sys.stderr)
        return "failed"
    print(f"plugins: updated from {named}", file=sys.stderr)
    return "updated"


def reconcile_installs(project: Path, updated: set[str] | None = None) -> list[str]:
    import reload_plugins

    changed = reload_plugins.reconcile_claude(project)
    if updated is not None:
        updated.update(changed)
    return changed


def settle(project: Path, reconcile=None) -> str:
    import plugin_autoupdate

    lock = plugin_autoupdate.acquire_lock(state_directory() / "launch-check.lock")
    if lock is None:
        return "busy"
    try:
        changed = (reconcile or reconcile_installs)(project)
    except Exception as error:
        print(f"plugins: claude install check failed: {error}", file=sys.stderr)
        return "failed"
    finally:
        lock.close()
    if not changed:
        return "current"
    print(f"plugins: updated {', '.join(sorted(changed))}", file=sys.stderr)
    return "updated"


def check(project: Path, fetch=https_get, update=update_host, now: float | None = None, reconcile=None,
          host: str | None = None) -> str:
    directory = state_directory()
    topic = read_json(directory / "machine-events.json").get("topic")
    if not isinstance(topic, str) or not TOPIC.fullmatch(topic):
        return "unconfigured"
    path = directory / "machine-events-cursor.json"
    cursor = read_json(path)
    since = cursor.get("since")
    if not isinstance(since, str) or not MESSAGE_ID.fullmatch(since):
        since = cursor["since"] = "all"
    now = time.time() if now is None else now
    since_at = cursor.get("since_at")
    expired = since != "all" and isinstance(since_at, (int, float)) and now - since_at > EXPIRY_SECONDS
    try:
        envelopes = poll(topic, "all" if expired else since, fetch)
    except (OSError, ValueError) as error:
        print(f"plugins: update check skipped ({type(error).__name__})", file=sys.stderr)
        return "offline"
    result = apply(envelopes, cursor, path, project, update, now, expired) if envelopes else "current"
    return settle(project, reconcile) if result == "current" and host != "codex" else result


def mark_current(fetch=https_get, now: float | None = None) -> bool:
    directory = state_directory()
    topic = read_json(directory / "machine-events.json").get("topic")
    if not isinstance(topic, str) or not TOPIC.fullmatch(topic):
        return False
    envelopes = poll(topic, "all", fetch)
    if envelopes:
        import plugin_autoupdate

        plugin_autoupdate.write_json(directory / "machine-events-cursor.json",
                                     {"since": envelopes[-1]["id"], "since_at": time.time() if now is None else now})
    return True


def require_update_check(result: str) -> None:
    if result == "busy":
        raise RuntimeError("Another plugin update is still running. Wait for it to finish, then retry the launch.")
    if result not in {"current", "updated", "offline"}:
        raise RuntimeError(
            f"Automatic plugin update check is {result}. Run Core's full setup.ps1 on Windows or "
            "setup.sh on Linux, then the same command with --check; verify the fresh-terminal "
            "launch check before continuing ordinary work. A scoped permissions-only setup is incomplete."
        )


def prepare_launch(project, host, executable, arguments):
    import required_plugins

    try:
        return required_plugins.check_launch(project, host, executable, arguments)
    except required_plugins.MissingRequiredPluginsError as error:
        import reload_plugins

        reload_plugins.repair_missing(host, error.project, error.missing, executable)
        return required_plugins.check_launch(project, host, executable, arguments)


def ensure_hooks(project: Path, executable: str | None = None, *, running_session=False) -> None:
    import agent_cli
    import codex_hook_trust
    import required_plugins

    required = required_plugins.selected(required_plugins.project_root(project))
    if not required:
        return
    binary = executable or agent_cli.resolve_codex_executable()[0]
    expected = {}
    roots = required_plugins.check(project, "codex", binary)
    for identity, root in (roots or {}).items():
        definitions = codex_hook_trust.required_definitions(root, identity)
        if definitions:
            expected[identity] = definitions
    codex_hook_trust.apply_trust(binary, project.resolve(), required=expected)
    if running_session:
        try:
            codex_hook_trust.reload_running(binary, project.resolve(), required=expected)
        except (OSError, RuntimeError) as error:
            raise codex_hook_trust.RestartRequired(str(error)) from error


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, default=Path.cwd())
    parser.add_argument("--host", choices=HOSTS)
    parser.add_argument("--target")
    parser.add_argument("arguments", nargs=argparse.REMAINDER)
    arguments = parser.parse_args(argv)
    effective_project = arguments.project.resolve()
    if arguments.host:
        import required_plugins

        try:
            native_arguments = arguments.arguments[1:] if arguments.arguments[:1] == ["--"] else arguments.arguments
            effective_project = prepare_launch(arguments.project.resolve(), arguments.host,
                                               arguments.target, native_arguments)
        except (required_plugins.RequiredPluginsError, RuntimeError, OSError) as error:
            print(f"plugins: {error}", file=sys.stderr)
            return 1
    try:
        host = arguments.host or os.environ.get(HOST_VARIABLE, "").strip().casefold()
        result = check(effective_project or arguments.project.resolve(), host=host if host in HOSTS else None)
    except Exception as error:
        print(f"plugins: update check failed: {error}", file=sys.stderr)
        result = "failed"
    if host in HOSTS and effective_project is not None:
        try:
            require_update_check(result)
        except RuntimeError as error:
            print(f"plugins: {error}", file=sys.stderr)
            return 1
    if host == "codex" and effective_project is not None:
        try:
            ensure_hooks(effective_project, arguments.target)
        except Exception as error:
            print(f"plugins: required hook readiness failed: {error}", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
