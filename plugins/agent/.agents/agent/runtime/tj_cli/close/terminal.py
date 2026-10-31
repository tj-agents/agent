"""Close and focus exact terminal objects after proving their process binding."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from importlib import resources
from pathlib import Path
from typing import Any

from tj_cli import processes

CONFIRM_ATTEMPTS = 10
CONFIRM_DELAY_SECONDS = 0.1
type JSON = dict[str, Any]
type Target = Mapping[str, object]
type Guard = Callable[[], None]


def _no_guard() -> None:
    return None


def _run(command: list[str]) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise processes.Refusal(
            "terminal control command failed: " + str(error)
        ) from error


@contextmanager
def _adapter() -> Iterator[Path]:
    resource = resources.files("tj_cli.close").joinpath("windows_terminal_uia.ps1")
    with resources.as_file(resource) as path:
        if not path.is_file():
            raise processes.Refusal("Windows Terminal adapter is unavailable")
        yield path


def _nonempty(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _tmux_command(identity: JSON, *arguments: str) -> list[str]:
    command = ["tmux"]
    socket = _nonempty(identity.get("socket"))
    if socket:
        command.extend(["-S", socket])
    return [*command, *arguments]


def _tmux(identity: JSON) -> list[JSON]:
    result = _run(
        _tmux_command(
            identity, "list-panes", "-a", "-F", "#{pane_id}\t#{pane_pid}\t#{pane_title}"
        )
    )
    if result.returncode:
        raise processes.Refusal(
            (result.stderr or result.stdout or "tmux inventory failed").strip()
        )
    items: list[JSON] = []
    for line in result.stdout.splitlines():
        pane, separator, rest = line.partition("\t")
        pid, separator, title = rest.partition("\t")
        if separator and pane and pid.isdigit():
            item: JSON = {
                "kind": "tmux",
                "pane_id": pane,
                "pid": int(pid),
                "title": title,
            }
            if _nonempty(identity.get("socket")):
                item["socket"] = identity["socket"]
            items.append(item)
    return items


def _kitty(identity: JSON) -> list[JSON]:
    listen = _nonempty(identity.get("listen_on"))
    if not listen:
        raise processes.Refusal("kitty target needs listen_on")
    result = _run(["kitty", "@", "--to", listen, "ls"])
    if result.returncode:
        raise processes.Refusal(
            (result.stderr or result.stdout or "kitty inventory failed").strip()
        )
    try:
        windows: object = json.loads(result.stdout)
    except ValueError as error:
        raise processes.Refusal("kitty returned malformed inventory") from error
    if not isinstance(windows, list):
        raise processes.Refusal("kitty returned malformed inventory")
    items: list[JSON] = []
    for os_window in windows:
        if not isinstance(os_window, dict):
            continue
        tabs = os_window.get("tabs", [])
        if not isinstance(tabs, list):
            continue
        for tab in tabs:
            if not isinstance(tab, dict):
                continue
            windows_in_tab = tab.get("windows", [])
            if not isinstance(windows_in_tab, list):
                continue
            for window in windows_in_tab:
                if not isinstance(window, dict) or not isinstance(
                    window.get("id"), int
                ):
                    continue
                roots: list[object] = [window.get("pid")]
                foreground = window.get("foreground_processes", [])
                if isinstance(foreground, list):
                    roots.extend(
                        item.get("pid") for item in foreground if isinstance(item, dict)
                    )
                items.append(
                    {
                        "kind": "kitty",
                        "listen_on": listen,
                        "window_id": str(window["id"]),
                        "title": str(window.get("title") or ""),
                        "root_pids": [
                            pid for pid in roots if isinstance(pid, int) and pid > 0
                        ],
                    }
                )
    return items


def _windows(_identity: JSON) -> list[JSON]:
    if os.name != "nt":
        raise processes.Refusal("Windows Terminal control is available only on Windows")
    with _adapter() as adapter:
        result = _run(
            [
                "powershell.exe",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(adapter),
                "-Json",
            ]
        )
    if result.returncode:
        raise processes.Refusal(
            (
                result.stderr or result.stdout or "Windows Terminal inventory failed"
            ).strip()
        )
    try:
        values: object = json.loads(result.stdout or "[]")
    except ValueError as error:
        raise processes.Refusal(
            "Windows Terminal returned malformed inventory"
        ) from error
    if not isinstance(values, list):
        raise processes.Refusal("Windows Terminal returned malformed inventory")
    return [
        {
            "kind": "windows-terminal",
            "runtime_id": value.get("runtimeId"),
            "pid": value.get("processId"),
            "title": value.get("title"),
        }
        for value in values
        if isinstance(value, dict)
        and _nonempty(value.get("runtimeId"))
        and isinstance(value.get("processId"), int)
    ]


def _identity(value: Mapping[str, object]) -> JSON:
    return dict(value)


def _inventory(identity: JSON) -> list[JSON]:
    kind = identity.get("kind")
    if kind == "tmux":
        return _tmux(identity)
    if kind == "kitty":
        return _kitty(identity)
    if kind == "windows-terminal":
        return _windows(identity)
    if kind == "konsole":
        return [_konsole(identity)]
    raise processes.Refusal("unsupported terminal identity")


def inventory(identity: Mapping[str, object]) -> tuple[Mapping[str, object], ...]:
    return tuple(_inventory(_identity(identity)))


def query(identity: Mapping[str, object]) -> tuple[Mapping[str, object], ...]:
    return inventory(identity)


def _konsole(identity: JSON) -> JSON:
    executable = (
        shutil.which("qdbus6") or shutil.which("qdbus") or shutil.which("qdbus-qt5")
    )
    service = _nonempty(identity.get("service"))
    session = _nonempty(identity.get("session"))
    if not executable or not service or not session or not session.startswith("/"):
        raise processes.Refusal(
            "Konsole needs qdbus6/qdbus and its recorded service and full session path"
        )
    roots: list[int] = []
    for method in (
        "org.kde.konsole.Session.processId",
        "org.kde.konsole.Session.foregroundProcessId",
    ):
        result = _run([executable, service, session, method])
        if result.returncode:
            raise processes.Refusal(
                (result.stderr or result.stdout or "Konsole query failed").strip()
            )
        try:
            pid = int(result.stdout.strip())
        except ValueError:
            continue
        info = processes.process_info(pid)
        if info is not None:
            roots.append(info.pid)
    if not roots:
        raise processes.Refusal("the recorded Konsole session has no queryable process")
    return {
        "kind": "konsole",
        "service": service,
        "session": session,
        "root_pids": roots,
        "title": str(identity.get("title") or ""),
    }


def _matches(item: JSON, identity: JSON) -> bool:
    kind = identity.get("kind")
    if item.get("kind") != kind:
        return False
    if kind == "tmux":
        return item.get("pane_id") == identity.get("pane_id") and item.get(
            "socket"
        ) == identity.get("socket")
    if kind == "kitty":
        return item.get("listen_on") == identity.get("listen_on") and item.get(
            "window_id"
        ) == str(identity.get("window_id"))
    if kind == "windows-terminal":
        return item.get("runtime_id") == _windows_runtime_id(identity)
    if kind == "konsole":
        return item.get("service") == identity.get("service") and item.get(
            "session"
        ) == identity.get("session")
    return False


def _one(identity: JSON) -> JSON:
    matches = [item for item in _inventory(identity) if _matches(item, identity)]
    if len(matches) != 1:
        raise processes.Refusal("exact terminal target is absent or ambiguous")
    return matches[0]


def _roots(item: JSON) -> list[int]:
    if item["kind"] in {"kitty", "konsole"}:
        values = item["root_pids"]
        return (
            values
            if isinstance(values, list)
            and all(isinstance(value, int) for value in values)
            else []
        )
    pid = item.get("pid")
    return [pid] if isinstance(pid, int) else []


def _windows_runtime_id(identity: JSON) -> str:
    runtime_id = _nonempty(identity.get("runtime_id"))
    if runtime_id:
        return runtime_id
    binding = identity.get("tab_binding")
    if not isinstance(binding, dict):
        raise processes.Refusal("Windows Terminal target needs tab binding evidence")
    tab_id = _nonempty(binding.get("runtime_id"))
    if tab_id:
        return tab_id
    raise processes.Refusal("Windows Terminal tab binding evidence is incomplete")


def _windows_console_association(
    frontend: Mapping[str, object], identity: JSON
) -> bool:
    if os.name != "nt":
        return False
    frontend_pid, _started = processes.valid_identity(frontend)
    probe = Path(__file__).with_name("probe.py").resolve()
    if not probe.is_file():
        return False
    with _adapter() as adapter:
        command = [
            sys.executable,
            "-I",
            "-B",
            str(probe),
            "--frontend-pid",
            str(frontend_pid),
            "--tab-id",
            _windows_runtime_id(identity),
            "--inventory-adapter",
            str(adapter),
        ]
        creationflags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(
            subprocess, "CREATE_NEW_PROCESS_GROUP", 0
        )
        try:
            result = subprocess.run(
                command,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=5,
                check=False,
                creationflags=creationflags,
            )
            value: object = json.loads(result.stdout)
        except (OSError, subprocess.SubprocessError, ValueError):
            return False
    return result.returncode == 0 and value == {"associated": True}


def verify_process_binding(
    identity: Mapping[str, object], process_identity: Mapping[str, object]
) -> None:
    exact = _identity(identity)
    processes.require_live(process_identity, "process identity changed")
    item = _one(exact)
    if exact.get("kind") == "windows-terminal":
        if item.get("runtime_id") != _windows_runtime_id(exact):
            raise processes.Refusal("Windows Terminal tab binding changed")
        if not _windows_console_association(process_identity, exact):
            raise processes.Refusal(
                "Windows Terminal cannot prove frontend belongs to exact tab"
            )
    else:
        roots = [processes.process_info(pid) for pid in _roots(item)]
        actual_roots = [root for root in roots if root is not None]
        if not any(
            processes.descends_from(
                process_identity, {"pid": root.pid, "started_at": root.started_at}
            )
            for root in actual_roots
        ):
            raise processes.Refusal("process is not owned by the exact terminal target")


def native_roots(identity: Mapping[str, object]) -> tuple[int, ...]:
    return tuple(_roots(_one(_identity(identity))))


def verify_binding(target: Target) -> None:
    frontend = target.get("frontend")
    wrapper = target.get("wrapper")
    terminal = target.get("terminal")
    if (
        not isinstance(frontend, Mapping)
        or not isinstance(wrapper, Mapping)
        or not isinstance(terminal, Mapping)
    ):
        raise processes.Refusal("target needs frontend, wrapper, and terminal objects")
    processes.require_live(frontend, "frontend identity changed")
    processes.require_live(wrapper, "wrapper identity changed")
    verify_process_binding(terminal, wrapper)
    if not processes.descends_from(frontend, wrapper):
        raise processes.Refusal("frontend is not owned by the dedicated wrapper")
    if terminal.get("kind") == "windows-terminal":
        verify_process_binding(terminal, frontend)


def close(target: Target, *, before_close: Guard = _no_guard) -> None:
    terminal = target.get("terminal")
    if not isinstance(terminal, Mapping):
        raise processes.Refusal("target needs a terminal object")
    verify_binding(target)
    close_identity(terminal, before_close=before_close)


def close_identity(identity: Mapping[str, object], *, before_close: Guard) -> None:
    exact = _identity(identity)
    _one(exact)
    before_close()
    kind = exact.get("kind")
    if kind == "tmux":
        pane = _nonempty(exact.get("pane_id"))
        if not pane:
            raise processes.Refusal("tmux target needs pane_id")
        result = _run(_tmux_command(exact, "kill-pane", "-t", pane))
    elif kind == "kitty":
        listen, window = (
            _nonempty(exact.get("listen_on")),
            _nonempty(str(exact.get("window_id", ""))),
        )
        if not listen or not window:
            raise processes.Refusal("kitty target needs listen_on and window_id")
        result = _run(
            ["kitty", "@", "--to", listen, "close-window", "--match", "id:" + window]
        )
    elif kind == "windows-terminal":
        with _adapter() as adapter:
            result = _run(
                [
                    "powershell.exe",
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    str(adapter),
                    "-TabId",
                    _windows_runtime_id(exact),
                ]
            )
    else:
        raise processes.Refusal("unsupported terminal identity")
    if result.returncode:
        raise processes.Refusal(
            (result.stderr or result.stdout or "terminal refused close").strip()
        )
    for _attempt in range(CONFIRM_ATTEMPTS):
        try:
            absent = not any(_matches(item, exact) for item in _inventory(exact))
        except processes.Refusal:
            if kind == "tmux" and _tmux_server_disappeared(exact):
                return
            raise
        if absent:
            return
        time.sleep(CONFIRM_DELAY_SECONDS)
    raise processes.Refusal("terminal did not confirm exact tab disappearance")


def focus(identity: Mapping[str, object]) -> None:
    exact = _identity(identity)
    _one(exact)
    kind = exact.get("kind")
    if kind == "tmux":
        pane = _nonempty(exact.get("pane_id"))
        if not pane:
            raise processes.Refusal("tmux target needs pane_id")
        result = _run(_tmux_command(exact, "select-window", "-t", pane))
        if not result.returncode:
            result = _run(_tmux_command(exact, "select-pane", "-t", pane))
        if not result.returncode:
            result = _run(_tmux_command(exact, "switch-client", "-t", pane))
    elif kind == "kitty":
        listen, window = (
            _nonempty(exact.get("listen_on")),
            _nonempty(str(exact.get("window_id", ""))),
        )
        if not listen or not window:
            raise processes.Refusal("kitty target needs listen_on and window_id")
        result = _run(
            ["kitty", "@", "--to", listen, "focus-window", "--match", "id:" + window]
        )
    elif kind == "windows-terminal":
        with _adapter() as adapter:
            result = _run(
                [
                    "powershell.exe",
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    str(adapter),
                    "-TabId",
                    _windows_runtime_id(exact),
                    "-Focus",
                ]
            )
    else:
        raise processes.Refusal("terminal cannot focus an exact tab")
    if result.returncode:
        raise processes.Refusal(
            (result.stderr or result.stdout or "terminal refused focus").strip()
        )


def _tmux_server_disappeared(identity: JSON) -> bool:
    result = _run(_tmux_command(identity, "has-session"))
    message = (result.stderr or result.stdout).lower()
    return result.returncode != 0 and "no server running" in message


__all__ = [
    "Target",
    "close",
    "close_identity",
    "focus",
    "inventory",
    "native_roots",
    "query",
    "verify_binding",
    "verify_process_binding",
]
