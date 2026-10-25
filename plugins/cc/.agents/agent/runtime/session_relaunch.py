#!/usr/bin/env python3
"""Resume the calling Claude session in a fresh process once it has closed (Python 3.9+)."""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import re
import secrets
import shlex
import subprocess
import sys
import time

HERE = Path(__file__).resolve().parent
IN_USE = ".in_use"
PROMPT = "Plugins were refreshed and this conversation resumed in a fresh process. Continue the requested work."
WAIT_SECONDS = 960
VARIADIC = {"--add-dir", "--mcp-config"}
SINGLE = {"--settings", "--plugin-dir", "--setting-sources", "--permission-mode", "--model"}
FLAGS = {"--strict-mcp-config", "--dangerously-skip-permissions", "--allow-dangerously-skip-permissions"}


def state_directory() -> Path:
    return Path(os.environ.get("AGENT_STATE_DIRECTORY") or Path.home() / ".agents-state")


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


def preserved_arguments(argv: list[str]) -> list[str]:
    kept, index = [], 0
    while index < len(argv):
        token = argv[index]
        name, equals, _ = token.partition("=")
        index += 1
        if name in FLAGS and not equals:
            kept.append(token)
        elif name in SINGLE | VARIADIC and equals:
            kept.append(token)
        elif name in SINGLE and index < len(argv):
            kept += [token, argv[index]]
            index += 1
        elif name in VARIADIC:
            values = []
            while index < len(argv) and not argv[index].startswith("-"):
                values.append(argv[index])
                index += 1
            if values:
                kept += [token, *values]
    return kept


def windows_split(command_line: str) -> list[str]:
    import ctypes
    from ctypes import wintypes

    shell32 = ctypes.windll.shell32
    shell32.CommandLineToArgvW.argtypes = (wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_int))
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
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
             f"(Get-CimInstance -Class Win32_Process -Filter 'ProcessId = {int(pid)}').CommandLine"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30, check=False)
        if result.returncode or not result.stdout.strip():
            raise OSError("the host command line could not be read")
        return windows_split(result.stdout.strip())[1:]
    proc = Path(f"/proc/{int(pid)}/cmdline")
    if proc.is_file():
        return [value.decode("utf-8", "replace") for value in proc.read_bytes().split(b"\0")[1:] if value]
    result = subprocess.run(["ps", "-ww", "-o", "command=", "-p", str(int(pid))],
                            capture_output=True, text=True, timeout=30, check=False)
    if result.returncode or not result.stdout.strip():
        raise OSError("the host command line could not be read")
    return shlex.split(result.stdout.strip())[1:]


def effective_installation(entries: list, project: Path) -> dict | None:
    resolved = project.resolve()
    ranked = []
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("installPath"), str):
            continue
        scope, owner = entry.get("scope"), entry.get("projectPath")
        if scope in {"local", "project"} and owner and resolved.is_relative_to(Path(owner).resolve()):
            ranked.append(((0 if scope == "local" else 1), -len(Path(owner).resolve().parts), entry))
        elif scope in {"user", "managed"}:
            ranked.append((2 if scope == "user" else 3, 0, entry))
    return min(ranked, key=lambda item: item[:2])[2] if ranked else None


def stale_plugins(pid: int, project: Path, identities: list[str], installed: dict, updated: set[str]) -> list[str]:
    stale = []
    for identity in identities:
        entry = effective_installation(installed.get(identity) or [], project)
        if entry is None:
            continue
        current = Path(entry["installPath"])
        loaded = {os.path.normcase(str(version)) for version in (current.parent.iterdir() if pid and current.parent.is_dir() else ())
                  if (version / IN_USE / str(pid)).is_file()}
        if (os.path.normcase(str(current)) not in loaded) and (loaded or identity in updated):
            stale.append(identity)
    return stale


def utility_script(skill: str, name: str) -> Path:
    for root in HERE.parents:
        markers = (root / ".git", root / ".codex-plugin" / "plugin.json", root / ".claude-plugin" / "plugin.json")
        path = root / ".agents" / "agent" / "utility" / skill / "scripts" / name
        if path.is_file() and any(marker.exists() for marker in markers):
            return path
    raise RuntimeError(f"the packaged Agent {skill} utility ({name}) was not found")


def load(path: Path, name: str):
    if str(path.parent) not in sys.path:
        sys.path.insert(0, str(path.parent))
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def recovery_script(project: Path, installed: dict) -> Path:
    return HERE / "agent_recovery.py"


def relaunch(project: Path, installed: dict, shared=None, close=None, arguments=host_arguments) -> dict:
    if close is None:
        raise RuntimeError("session relaunch is unavailable until the declared CLI close capability supplies an exact target")
    shared = shared or load(utility_script("peer", "session_close.py"), "session_close")
    entry = shared.own_host_and_entry()
    if entry.get("host") != "claude":
        raise RuntimeError("the calling session is not a registered Claude session")
    session_id = entry["session_id"]
    native = preserved_arguments(arguments(entry["pid"]))
    token = secrets.token_hex(16)
    record = record_path(session_id)
    write_json(record, {"state": "waiting", "token": token, "session_id": session_id, "pid": entry["pid"],
                        "native_arguments": native, "at": time.time()})
    options = ["--session-id", session_id, "--pid", str(entry["pid"]),
               "--pid-started-at", repr(entry["pid_started_at"]), "--record", str(record), "--token", token,
               "--recovery", str(recovery_script(project, installed)), "--title", entry.get("title") or "Claude",
               "--prompt", PROMPT, "--timeout", str(WAIT_SECONDS),
               *[f"--native-argument={value}" for value in native]]
    try:
        shared.start_detached(HERE / "relaunch_watcher.py", options)
        armed = close.arm()
    except Exception as error:
        write_json(record, {**read_json(record), "state": "cancelled", "reason": str(error), "at": time.time()})
        raise RuntimeError(f"relaunch was not armed: {error}") from error
    return {"relaunch": "armed", "session_id": session_id, "native_arguments": native,
            "close": armed.get("status")}


def session_identity() -> tuple[str, int] | None:
    session_id, pid = os.environ.get("CLAUDE_CODE_SESSION_ID"), os.environ.get("CLAUDE_PID")
    if not session_id or not re.fullmatch(r"[A-Za-z0-9_-]+", session_id):
        return None
    return session_id, int(pid) if pid and pid.isdigit() else 0
