"""Close exact terminal objects after proving their explicit process binding."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, cast

import tj_cli.process.identity

CONFIRM_ATTEMPTS = 10
CONFIRM_DELAY_SECONDS = 0.1


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
        raise tj_cli.process.identity.Refusal(
            "terminal control command failed: " + str(error)
        ) from error


def _nonempty(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _tmux_command(identity: dict[str, Any], *arguments: str) -> list[str]:
    command = ["tmux"]
    socket = _nonempty(identity.get("socket"))
    if socket:
        command.extend(["-S", socket])
    return [*command, *arguments]


def _tmux(identity: dict[str, Any]) -> list[dict[str, Any]]:
    result = _run(
        _tmux_command(identity, "list-panes", "-a", "-F", "#{pane_id}\t#{pane_pid}")
    )
    if result.returncode:
        raise tj_cli.process.identity.Refusal(
            (result.stderr or result.stdout or "tmux inventory failed").strip()
        )
    items = []
    for line in result.stdout.splitlines():
        pane, separator, pid = line.partition("\t")
        if separator and pane and pid.isdigit():
            item = {"kind": "tmux", "pane_id": pane, "pid": int(pid)}
            if _nonempty(identity.get("socket")):
                item["socket"] = identity["socket"]
            items.append(item)
    return items


def _kitty(identity: dict[str, Any]) -> list[dict[str, Any]]:
    listen = _nonempty(identity.get("listen_on"))
    if not listen:
        raise tj_cli.process.identity.Refusal("kitty target needs listen_on")
    result = _run(["kitty", "@", "--to", listen, "ls"])
    if result.returncode:
        raise tj_cli.process.identity.Refusal(
            (result.stderr or result.stdout or "kitty inventory failed").strip()
        )
    try:
        windows = json.loads(result.stdout)
    except ValueError as error:
        raise tj_cli.process.identity.Refusal(
            "kitty returned malformed inventory"
        ) from error
    if not isinstance(windows, list):
        raise tj_cli.process.identity.Refusal("kitty returned malformed inventory")
    items = []
    for os_window in windows:
        if not isinstance(os_window, dict):
            continue
        for tab in os_window.get("tabs", []):
            if not isinstance(tab, dict):
                continue
            for window in tab.get("windows", []):
                if not isinstance(window, dict) or not isinstance(
                    window.get("id"), int
                ):
                    continue
                roots = [window.get("pid")]
                roots.extend(
                    item.get("pid")
                    for item in window.get("foreground_processes", [])
                    if isinstance(item, dict)
                )
                items.append(
                    {
                        "kind": "kitty",
                        "listen_on": listen,
                        "window_id": str(window["id"]),
                        "root_pids": [
                            pid for pid in roots if isinstance(pid, int) and pid > 0
                        ],
                    }
                )
    return items


def _windows(identity: dict[str, Any]) -> list[dict[str, Any]]:
    if os.name != "nt":
        raise tj_cli.process.identity.Refusal(
            "Windows Terminal control is available only on Windows"
        )
    adapter = verified_close_adapter_path()
    if not adapter.is_file():
        raise tj_cli.process.identity.Refusal("Windows Terminal adapter is unavailable")
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
        raise tj_cli.process.identity.Refusal(
            (
                result.stderr or result.stdout or "Windows Terminal inventory failed"
            ).strip()
        )
    try:
        values = json.loads(result.stdout or "[]")
    except ValueError as error:
        raise tj_cli.process.identity.Refusal(
            "Windows Terminal returned malformed inventory"
        ) from error
    if not isinstance(values, list):
        raise tj_cli.process.identity.Refusal(
            "Windows Terminal returned malformed inventory"
        )
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


def _inventory(identity: dict[str, Any]) -> list[dict[str, Any]]:
    kind = identity.get("kind")
    if kind == "tmux":
        return _tmux(identity)
    if kind == "kitty":
        return _kitty(identity)
    if kind == "windows-terminal":
        return _windows(identity)
    if kind == "konsole":
        raise tj_cli.process.identity.Refusal(
            "Konsole cannot verify exact tab disappearance"
        )
    raise tj_cli.process.identity.Refusal("unsupported terminal identity")


def _matches(item: dict[str, Any], identity: dict[str, Any]) -> bool:
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
        binding = identity.get("tab_binding")
        return isinstance(binding, dict) and item.get("runtime_id") == binding.get(
            "runtime_id"
        )
    return False


def _one(identity: dict[str, Any]) -> dict[str, Any]:
    matches = [item for item in _inventory(identity) if _matches(item, identity)]
    if len(matches) != 1:
        raise tj_cli.process.identity.Refusal(
            "exact terminal target is absent or ambiguous"
        )
    return matches[0]


def _roots(item: dict[str, Any]) -> list[int]:
    if item["kind"] == "kitty":
        return cast(list[int], item["root_pids"])
    return [item["pid"]]


def _windows_tab_binding(identity: dict[str, Any]) -> str:
    binding = identity.get("tab_binding")
    if not isinstance(binding, dict):
        raise tj_cli.process.identity.Refusal(
            "Windows Terminal target needs tab binding evidence"
        )
    tab_id = _nonempty(binding.get("runtime_id"))
    title = _nonempty(binding.get("observed_title"))
    if (
        not tab_id
        or not title
        or binding.get("evidence") != "windows-uia-unique-title-at-launch"
    ):
        raise tj_cli.process.identity.Refusal(
            "Windows Terminal tab binding evidence is incomplete"
        )
    return tab_id


def _windows_console_association(
    frontend: dict[str, Any], identity: dict[str, Any]
) -> bool:
    if os.name != "nt":
        return False
    probe = Path(__file__).with_name("console_probe.py")
    adapter = verified_close_adapter_path()
    if not probe.is_file() or not adapter.is_file():
        return False
    frontend_pid, _started = tj_cli.process.identity.valid_identity(frontend)
    command = [
        sys.executable,
        "-B",
        str(probe),
        "--frontend-pid",
        str(frontend_pid),
        "--tab-id",
        _windows_tab_binding(identity),
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
        value = json.loads(result.stdout)
    except (OSError, subprocess.SubprocessError, ValueError):
        return False
    return result.returncode == 0 and value == {"associated": True}


def verify_binding(target: dict[str, Any]) -> None:
    frontend = target.get("frontend")
    wrapper = target.get("wrapper")
    terminal = target.get("terminal")
    if not isinstance(terminal, dict):
        raise tj_cli.process.identity.Refusal("target needs a terminal object")
    tj_cli.process.identity.require_live(frontend, "frontend identity changed")
    tj_cli.process.identity.require_live(wrapper, "wrapper identity changed")
    item = _one(terminal)
    if terminal.get("kind") == "windows-terminal":
        if item.get("runtime_id") != _windows_tab_binding(terminal):
            raise tj_cli.process.identity.Refusal(
                "Windows Terminal tab binding changed"
            )
        if not _windows_console_association(cast(dict[str, Any], frontend), terminal):
            raise tj_cli.process.identity.Refusal(
                "Windows Terminal cannot prove frontend belongs to exact tab"
            )
    else:
        roots = [tj_cli.process.identity.process_info(pid) for pid in _roots(item)]
        actual_roots = [root for root in roots if root is not None]
        if not any(
            tj_cli.process.identity.descends_from(
                wrapper, {"pid": root.pid, "started_at": root.started_at}
            )
            for root in actual_roots
        ):
            raise tj_cli.process.identity.Refusal(
                "wrapper is not owned by the exact terminal target"
            )
    if not tj_cli.process.identity.descends_from(frontend, wrapper):
        raise tj_cli.process.identity.Refusal(
            "frontend is not owned by the dedicated wrapper"
        )


def close(target: dict[str, Any]) -> None:
    terminal = target["terminal"]
    verify_binding(target)
    identity = terminal
    kind = identity["kind"]
    if kind == "tmux":
        result = _run(_tmux_command(identity, "kill-pane", "-t", identity["pane_id"]))
    elif kind == "kitty":
        result = _run(
            [
                "kitty",
                "@",
                "--to",
                identity["listen_on"],
                "close-window",
                "--match",
                "id:" + str(identity["window_id"]),
            ]
        )
    elif kind == "windows-terminal":
        tab_id = _windows_tab_binding(identity)
        result = _run(
            [
                "powershell.exe",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(verified_close_adapter_path()),
                "-TabId",
                tab_id,
            ]
        )
    else:
        raise tj_cli.process.identity.Refusal("unsupported terminal identity")
    if result.returncode:
        raise tj_cli.process.identity.Refusal(
            (result.stderr or result.stdout or "terminal refused close").strip()
        )
    for _attempt in range(CONFIRM_ATTEMPTS):
        try:
            absent = not any(_matches(item, identity) for item in _inventory(identity))
        except tj_cli.process.identity.Refusal:
            if kind == "tmux" and _tmux_server_disappeared(identity):
                return
            raise
        if absent:
            return
        time.sleep(CONFIRM_DELAY_SECONDS)
    raise tj_cli.process.identity.Refusal(
        "terminal did not confirm exact tab disappearance"
    )


def _tmux_server_disappeared(identity: dict[str, Any]) -> bool:
    result = _run(_tmux_command(identity, "has-session"))
    message = (result.stderr or result.stdout).lower()
    return result.returncode != 0 and "no server running" in message


def windows_adapter_path() -> Path:
    return Path(__file__).with_name("windows_terminal_uia.ps1")


def verified_close_adapter_path() -> Path:
    return Path(__file__).with_name("windows_verified_close.ps1")
