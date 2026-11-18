#!/usr/bin/env python3
"""Resume the calling session in a fresh process once it has closed (Python 3.9+)."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import secrets
import shlex
import subprocess
import sys
import time

from tj_agent import entry as agent_entry
from tj_agent.sessions import register_session, session_close
from tj_cli import close as cli_close_module
from tj_agent.hosts import Host
import tj_agent.hosts.plugins as host_plugins
import tj_agent.hosts.sessions as host_sessions
import tj_agent.plugins.required_plugins as required_plugins

HERE = Path(__file__).resolve().parent
PROMPT = "Plugins were refreshed and this conversation resumed in a fresh process. Continue the requested work."
WAIT_SECONDS = 960


def state_directory() -> Path:
    return Path(
        os.environ.get("AGENT_STATE_DIRECTORY") or Path.home() / ".agents-state"
    )


def record_path(session_id: str) -> Path:
    return state_directory() / "session-relaunch" / f"{session_id}.json"


def read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + secrets.token_hex(4))
    temporary.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def windows_split(command_line: str) -> list[str]:
    import ctypes
    from ctypes import wintypes

    shell32 = ctypes.windll.shell32
    shell32.CommandLineToArgvW.argtypes = (
        wintypes.LPCWSTR,
        ctypes.POINTER(ctypes.c_int),
    )
    shell32.CommandLineToArgvW.restype = ctypes.POINTER(wintypes.LPWSTR)
    count = ctypes.c_int()
    values = shell32.CommandLineToArgvW(command_line, ctypes.byref(count))
    if not values:
        raise OSError("the host command line could not be parsed")
    try:
        return [values[index] for index in range(count.value)]
    finally:
        ctypes.windll.kernel32.LocalFree(values)


def host_arguments(pid: int) -> list[str]:
    if os.name == "nt":
        result = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                f"(Get-CimInstance -Class Win32_Process -Filter 'ProcessId = {int(pid)}').CommandLine",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            check=False,
        )
        if result.returncode or not result.stdout.strip():
            raise OSError("the host command line could not be read")
        return windows_split(result.stdout.strip())[1:]
    proc = Path(f"/proc/{int(pid)}/cmdline")
    if proc.is_file():
        return [
            value.decode("utf-8", "replace")
            for value in proc.read_bytes().split(b"\0")[1:]
            if value
        ]
    result = subprocess.run(
        ["ps", "-ww", "-o", "command=", "-p", str(int(pid))],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if result.returncode or not result.stdout.strip():
        raise OSError("the host command line could not be read")
    return shlex.split(result.stdout.strip())[1:]


def caller_host() -> Host | None:
    _, host, _ = register_session.resolve_recorded_pid(os.getppid())
    try:
        return Host(host)
    except ValueError:
        return None


def cli_close_capability(project: Path, host: Host, executable: str) -> Path:
    root = required_plugins.inventory_roots(project, host, executable).get(
        "cli@tj-agents"
    )
    if root is None:
        raise RuntimeError(
            "Automatic refresh requires enabled cli@tj-agents with its close capability"
        )
    return Path(cli_close_module.__file__)


def arm_client_close(
    project: Path, entry: dict, cli_close: Path, token: str, host: Host
) -> dict:
    close = Path(agent_entry.__file__)
    root = state_directory() / "refresh-close" / token
    target, completion, cancel = (
        root / "target.json",
        root / "complete.json",
        root / "cancel.json",
    )
    environment = {**os.environ, host.home_variable: entry["profile"]}
    prepared = subprocess.run(
        [
            sys.executable,
            "-B",
            str(close),
            "tj_agent.sessions.agent_close",
            "prepare",
            "--session-id",
            entry["session_id"],
            "--profile",
            entry["profile"],
            "--target",
            str(target),
        ],
        cwd=project,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
    )
    if prepared.returncode:
        raise RuntimeError(
            (prepared.stderr or prepared.stdout).strip()
            or "cannot prepare exact client close"
        )
    command = [
        sys.executable,
        "-B",
        str(close),
        "tj_agent.sessions.agent_close",
        "arm",
        "--host",
        host.value,
        "--session-id",
        entry["session_id"],
        "--target",
        str(target),
        "--profile",
        entry["profile"],
        "--cli-close",
        str(cli_close),
        "--completion",
        str(completion),
        "--cancel",
        str(cancel),
        "--completion-token",
        token,
    ]
    command += host_sessions.SESSIONS[host].close_arguments(entry)
    armed = subprocess.run(
        command,
        cwd=project,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
    )
    if armed.returncode:
        raise RuntimeError(
            (armed.stderr or armed.stdout).strip() or "cannot arm exact client close"
        )
    request = state_directory() / "client-close" / f"{token}.json"
    result = state_directory() / "client-close" / f"{token}.result.json"
    raw = request.read_bytes()
    return {
        "request": str(request.resolve()),
        "request_sha256": hashlib.sha256(raw).hexdigest(),
        "result": str(result.resolve()),
        "completion": str(completion.resolve()),
        "cancel": str(cancel.resolve()),
        "completion_token": token,
    }


def claim_record(record: Path, value: dict) -> None:
    import tj_agent.plugins.plugin_autoupdate as plugin_autoupdate

    record.parent.mkdir(parents=True, exist_ok=True)
    lock = None
    deadline = time.monotonic() + 5
    try:
        while lock is None and time.monotonic() < deadline:
            lock = plugin_autoupdate.acquire_lock(
                record.with_name(record.name + ".claim.lock")
            )
            if lock is None:
                time.sleep(0.02)
        if lock is None:
            raise RuntimeError("the automatic refresh recovery record is busy")
        existing = read_json(record)
        if existing.get("state") == "waiting":
            raise RuntimeError(
                "an automatic refresh recovery is already armed for this exact session"
            )
        write_json(record, value)
    finally:
        if lock is not None:
            lock.close()


def cancel_close(close: dict, reason: str) -> None:
    try:
        path = Path(close["cancel"])
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("x", encoding="utf-8") as stream:
            json.dump({"cancelled": True, "reason": reason}, stream)
            stream.write("\n")
    except (KeyError, OSError, TypeError):
        pass


def relaunch(
    project: Path,
    installed: dict,
    shared=None,
    close=None,
    arguments=host_arguments,
    *,
    host: Host,
) -> dict:
    sessions = host_sessions.SESSIONS[host]
    shared = shared or session_close
    entry = shared.own_host_and_entry()
    if entry.get("host") != host.value:
        raise RuntimeError("the calling session is not the expected registered CLI")
    sessions.validate_relaunch(entry, project)
    session_id = entry["session_id"]
    native = sessions.native_arguments(arguments(entry["pid"]))
    executable = host_plugins.PLUGINS[host].resolve_executable()
    sessions.check_relaunch(project, executable, native, session_id, PROMPT)
    cli_close = cli_close_capability(project, host, executable)
    token = secrets.token_hex(16)
    record = record_path(session_id)
    claim_record(
        record,
        {
            "state": "waiting",
            "token": token,
            "session_id": session_id,
            "pid": entry["pid"],
            "host": host.value,
            "profile": entry.get("profile"),
            "cwd": str(project.resolve()),
            "native_arguments": native,
            "at": time.time(),
        },
    )
    options = [
        "--host",
        host.value,
        "--session-id",
        session_id,
        "--pid",
        str(entry["pid"]),
        "--pid-started-at",
        repr(entry["pid_started_at"]),
        "--record",
        str(record),
        "--token",
        token,
        "--recovery",
        str(Path(agent_entry.__file__)),
        "--title",
        entry.get("title") or host.value.title(),
        "--prompt",
        PROMPT,
        "--timeout",
        str(WAIT_SECONDS),
        "--expected-cwd",
        str(project.resolve()),
        *[f"--native-argument={value}" for value in native],
    ]
    armed = None
    if entry.get("profile"):
        options += [
            "--history-root",
            str(Path(entry["profile"]) / host.history_directory),
        ]
    try:
        shared.start_detached(HERE / "relaunch_watcher.py", options)
        armed = arm_client_close(project, entry, cli_close, token, host)
        write_json(record, {**read_json(record), "close": armed, "at": time.time()})
    except Exception as error:
        if armed is not None:
            cancel_close(
                armed, "automatic refresh recovery was cancelled before relaunch"
            )
        write_json(
            record,
            {
                **read_json(record),
                "state": "cancelled",
                "reason": str(error),
                "at": time.time(),
            },
        )
        raise RuntimeError(f"relaunch was not armed: {error}") from error
    return {
        "relaunch": "armed",
        "session_id": session_id,
        "native_arguments": native,
        "close": "armed",
    }
