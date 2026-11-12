"""Remove state that retired Agent machinery and tools left on this machine."""

from __future__ import annotations

import hashlib
import os
import shutil
import stat
import subprocess
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from tj_cli import process

Runner = Callable[..., subprocess.CompletedProcess[str]]

TJ_WATCH_SHORTCUT = Path("Microsoft/Windows/Start Menu/Programs/Startup/tj-watch.lnk")
TJ_WATCH_EXECUTABLE = Path("Programs/tj/tj-watch.exe")
_QUERY_LIMITED_INFORMATION = 0x1000
_TERMINATE = 0x0001
_SYNCHRONIZE = 0x00100000


@dataclass(frozen=True, slots=True)
class Removal:
    removed: list[str]
    failures: list[str]


@dataclass(frozen=True, slots=True)
class RunningProcess:
    pid: int
    executable: str


class ProcessTable(Protocol):
    def listing(self) -> list[RunningProcess]: ...

    def terminate(self, running: RunningProcess) -> bool: ...


if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    def _kernel32() -> ctypes.WinDLL:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.K32EnumProcesses.argtypes = [
            ctypes.POINTER(wintypes.DWORD),
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
        ]
        kernel32.K32EnumProcesses.restype = wintypes.BOOL
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.QueryFullProcessImageNameW.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            wintypes.LPWSTR,
            ctypes.POINTER(wintypes.DWORD),
        ]
        kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
        kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
        kernel32.TerminateProcess.restype = wintypes.BOOL
        kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel32.WaitForSingleObject.restype = wintypes.DWORD
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        return kernel32

    def _image(kernel32: ctypes.WinDLL, handle: int) -> str | None:
        size = wintypes.DWORD(32768)
        buffer = ctypes.create_unicode_buffer(size.value)
        if not kernel32.QueryFullProcessImageNameW(
            handle, 0, buffer, ctypes.byref(size)
        ):
            return None
        return buffer.value


class WindowsProcessTable:
    def listing(self) -> list[RunningProcess]:
        found: list[RunningProcess] = []
        if sys.platform == "win32":
            kernel32 = _kernel32()
            pids = (wintypes.DWORD * 16384)()
            returned = wintypes.DWORD()
            if not kernel32.K32EnumProcesses(
                pids, ctypes.sizeof(pids), ctypes.byref(returned)
            ):
                raise ctypes.WinError(ctypes.get_last_error())
            for pid in pids[: returned.value // ctypes.sizeof(wintypes.DWORD)]:
                handle = kernel32.OpenProcess(_QUERY_LIMITED_INFORMATION, False, pid)
                if not handle:
                    continue
                try:
                    image = _image(kernel32, handle)
                finally:
                    kernel32.CloseHandle(handle)
                if image:
                    found.append(RunningProcess(int(pid), image))
        return found

    def terminate(self, running: RunningProcess) -> bool:
        stopped = False
        if sys.platform == "win32":
            kernel32 = _kernel32()
            access = _TERMINATE | _QUERY_LIMITED_INFORMATION | _SYNCHRONIZE
            handle = kernel32.OpenProcess(access, False, running.pid)
            if handle:
                try:
                    stopped = (
                        _image(kernel32, handle) == running.executable
                        and bool(kernel32.TerminateProcess(handle, 1))
                        and kernel32.WaitForSingleObject(handle, 5000) == 0
                    )
                finally:
                    kernel32.CloseHandle(handle)
        return stopped


RETIRED_ENTRIES = (
    "backups",
    "capacity-inputs",
    "claude-standards-sync.json",
    "client-close",
    "close-assessments",
    "close-lifecycle",
    "codex-plugin-bytes.json",
    "continuations",
    "currency",
    "harness-permissions.json.38kuhs2u",
    "install-recovery.lock",
    "launch-check.lock",
    "listeners",
    "machine-events-cursor.json",
    "machine-events-service.json",
    "machine-events-state.json",
    "machine-events-watch.lock",
    "machine-events.json",
    "machine-events.lock",
    "machine-events.log",
    "machine-events.log.1",
    "machine-events.log.2",
    "machine-fingerprint.json",
    "machine-install.log",
    "migrations",
    "native-cli.lock",
    "orphan-reaper-notice.json",
    "plugin-activation",
    "plugin-autoupdate.json",
    "plugin-autoupdate.lock",
    "plugin-cache-notice.json",
    "plugin-heads.json",
    "plugin-heads.lock",
    "plugin-pins",
    "plugin-refresh.json",
    "plugin-refresh.log",
    "plugin-update.json",
    "plugin-update.lock",
    "plugin-update.log",
    "publish-update-owner-setup-result.json",
    "published-manifest.json",
    "published-repository.git",
    "recovery-launches",
    "refresh-close",
    "repair-backups",
    "session-close",
    "session-relaunch",
    "tj-agents-continuation-stop",
    "bin/agent_cli.py",
    "bin/cli_client.py",
    "bin/codex_hook_trust.py",
    "bin/codex_websocket.py",
    "bin/launch_check.py",
    "bin/launch_status.py",
    "bin/machine_events.py",
    "bin/plugin_autoupdate.py",
    "bin/register_session.py",
    "bin/reload_plugins.py",
    "bin/required_plugins.py",
    "bin/startup_binding.py",
    "bin/terminal_command.py",
    "bin/.agents/agent-store",
    "bin/.agents/agent/runtime/machine_events.py",
    "bin/.agents/agent/runtime/event_service.py",
    "bin/.agents/agent/runtime/event_watch.py",
)


def _watcher_name(state_directory: Path, windows: bool) -> str:
    state = str(state_directory.resolve())
    digest = hashlib.sha256(
        (state.casefold() if windows else state).encode()
    ).hexdigest()[:16]
    return f"tj-agents-machine-events-{digest}"


def _watcher_unit(name: str, home: Path | None) -> Path:
    return (home or Path.home()) / ".config" / "systemd" / "user" / f"{name}.service"


def _watcher_present(
    state_directory: Path, windows: bool, home: Path | None, run: Runner
) -> list[str]:
    name = _watcher_name(state_directory, windows)
    if not windows:
        unit = _watcher_unit(name, home)
        return [f"systemd user unit {unit.name}"] if unit.exists() else []
    try:
        result = run(
            ["schtasks", "/Query", "/TN", name],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except OSError:
        return []
    return [f"scheduled task {name}"] if result.returncode == 0 else []


def _remove_watcher(
    state_directory: Path, windows: bool, home: Path | None, run: Runner
) -> list[str]:
    name = _watcher_name(state_directory, windows)
    options = {"capture_output": True, "text": True, "timeout": 30, "check": False}
    try:
        if windows:
            result = run(["schtasks", "/Delete", "/TN", name, "/F"], **options)
            return [f"scheduled task {name}"] if result.returncode == 0 else []
        unit = _watcher_unit(name, home)
        if not unit.exists():
            return []
        run(["systemctl", "--user", "disable", "--now", unit.name], **options)
        unit.unlink(missing_ok=True)
        run(["systemctl", "--user", "daemon-reload"], **options)
        return [f"systemd user unit {unit.name}"]
    except OSError:
        return []


def _tj_watch_shortcut(environ: Mapping[str, str]) -> Path | None:
    appdata = environ.get("APPDATA")
    return Path(appdata) / TJ_WATCH_SHORTCUT if appdata else None


def _tj_watch_binary(environ: Mapping[str, str]) -> Path | None:
    local = environ.get("LOCALAPPDATA")
    return Path(local) / TJ_WATCH_EXECUTABLE if local else None


def _retire_tj_watch(
    environ: Mapping[str, str], processes: ProcessTable | None
) -> Removal:
    removed = []
    shortcut = _tj_watch_shortcut(environ)
    if shortcut is not None and shortcut.is_file():
        shortcut.unlink()
        removed.append(str(shortcut))
    binary = _tj_watch_binary(environ)
    if binary is None or not binary.is_file():
        return Removal(removed, [])
    table = processes or WindowsProcessTable()
    for running in table.listing():
        if running.executable.casefold() != str(binary).casefold():
            continue
        if table.terminate(running):
            removed.append(f"tj-watch process {running.pid}")
    try:
        binary.unlink()
    except PermissionError as error:
        return Removal(removed, [f"could not remove {binary}: {error}"])
    removed.append(str(binary))
    return Removal(removed, [])


def _retry_writable(
    function: Callable[[str], object], path: str, error: BaseException
) -> None:
    target = Path(path)
    if (
        not isinstance(error, PermissionError)
        or target.is_symlink()
        or target.is_junction()
    ):
        raise error
    target.chmod(target.lstat().st_mode | stat.S_IWRITE)
    function(path)


def _remove_entry(path: Path) -> None:
    if path.is_symlink():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path, onexc=_retry_writable)
    else:
        try:
            path.unlink()
        except PermissionError as error:
            _retry_writable(os.unlink, str(path), error)


def remove(
    state_directory: Path,
    *,
    windows: bool = os.name == "nt",
    home: Path | None = None,
    run: Runner = process.run,
    environ: Mapping[str, str] | None = None,
    processes: ProcessTable | None = None,
) -> Removal:
    """Repeats on purpose: older cached Agent builds re-create some entries until
    they age out."""
    removed = _remove_watcher(state_directory, windows, home, run)
    failures = []
    if windows:
        retired = _retire_tj_watch(
            os.environ if environ is None else environ, processes
        )
        removed += retired.removed
        failures += retired.failures
    for entry in RETIRED_ENTRIES:
        path = state_directory / entry
        if not (path.is_symlink() or path.exists()):
            continue
        try:
            _remove_entry(path)
        except OSError as error:
            failures.append(f"could not remove {path}: {error}")
        else:
            removed.append(str(path))
    return Removal(removed, failures)


def present(
    state_directory: Path,
    *,
    windows: bool = os.name == "nt",
    home: Path | None = None,
    run: Runner = process.run,
    environ: Mapping[str, str] | None = None,
) -> list[str]:
    found = _watcher_present(state_directory, windows, home, run)
    if windows:
        environment = os.environ if environ is None else environ
        tj_watch = (_tj_watch_shortcut(environment), _tj_watch_binary(environment))
        found += [str(path) for path in tj_watch if path is not None and path.is_file()]
    for entry in RETIRED_ENTRIES:
        path = state_directory / entry
        if path.is_symlink() or path.exists():
            found.append(str(path))
    return found
