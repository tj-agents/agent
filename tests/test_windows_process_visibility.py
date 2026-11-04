"""Observe real native window events and the configured Bash adapter."""

from __future__ import annotations

import ctypes
import json
import os
import shutil
import subprocess
import sys
import time
from ctypes import wintypes
from pathlib import Path

import pytest
from tj_cli import process

ROOT = Path(__file__).resolve().parents[1]
PROBE = ROOT / "tests/fixtures/plugin_console_probe.py"
SCRIPTS = ROOT / "plugins/agent/.agents/agent/utility/plugins/scripts"


@pytest.mark.skipif(sys.platform != "win32", reason="Windows top-level window events")
def test_worker_does_not_show_or_focus_a_terminal(tmp_path: Path) -> None:
    if sys.platform == "win32":
        observe_worker_windows(tmp_path)


if sys.platform == "win32":

    def observe_worker_windows(tmp_path: Path) -> None:
        user = ctypes.WinDLL("user32", use_last_error=True)
        callback_type = ctypes.WINFUNCTYPE(
            None,
            wintypes.HANDLE,
            wintypes.DWORD,
            wintypes.HWND,
            wintypes.LONG,
            wintypes.LONG,
            wintypes.DWORD,
            wintypes.DWORD,
        )
        token = "tj-agent-window-probe-" + tmp_path.name
        observed: list[tuple[int, int, str]] = []
        titled: set[int] = set()

        def correlate_title(window: int) -> None:
            title = ctypes.create_unicode_buffer(1024)
            user.GetWindowTextW(window, title, len(title))
            if token in title.value:
                titled.add(window)

        def on_event(
            hook: int,
            event: int,
            window: int,
            object_id: int,
            child_id: int,
            thread_id: int,
            timestamp: int,
        ) -> None:
            if object_id or child_id:
                return
            name = ctypes.create_unicode_buffer(256)
            user.GetClassNameW(window, name, len(name))
            if (
                name.value == "ConsoleWindowClass" or name.value.startswith("CASCADIA")
            ) and user.IsWindowVisible(window):
                correlate_title(window)
                observed.append((window, event, name.value))

        callback = callback_type(on_event)
        user.SetWinEventHook.argtypes = [
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HMODULE,
            callback_type,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.DWORD,
        ]
        user.SetWinEventHook.restype = wintypes.HANDLE
        user.UnhookWinEvent.argtypes = [wintypes.HANDLE]
        user.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        user.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        user.IsWindowVisible.argtypes = [wintypes.HWND]
        user.PeekMessageW.argtypes = [
            ctypes.POINTER(wintypes.MSG),
            wintypes.HWND,
            wintypes.UINT,
            wintypes.UINT,
            wintypes.UINT,
        ]
        user.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
        user.DispatchMessageW.restype = ctypes.c_ssize_t
        hooks = [
            user.SetWinEventHook(event, event, None, callback, 0, 0, 0)
            for event in (3, 0x8002)
        ]
        assert all(hooks)
        try:
            with subprocess.Popen(
                [
                    getattr(sys, "_base_executable", sys.executable),
                    str(PROBE),
                    str(SCRIPTS),
                    "clone",
                    str(tmp_path),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                env=dict(os.environ, TJ_AGENT_WINDOW_PROBE=token),
                creationflags=getattr(subprocess, "DETACHED_PROCESS", 0),
            ) as worker:
                deadline = time.monotonic() + 20
                while True:
                    message = wintypes.MSG()
                    while user.PeekMessageW(ctypes.byref(message), None, 0, 0, 1):
                        user.DispatchMessageW(ctypes.byref(message))
                    for window, _, _ in observed:
                        correlate_title(window)
                    if worker.poll() is not None:
                        break
                    if time.monotonic() >= deadline:
                        worker.kill()
                        pytest.fail("background window probe did not finish")
                    time.sleep(0.005)
                stdout, stderr = worker.communicate(timeout=5)
                assert worker.returncode == 0, stderr
                report = json.loads(stdout)
                assert not report["child_visible"]
                console_windows = set(report["console_windows"]) - {0}
        finally:
            for hook in hooks:
                if hook:
                    user.UnhookWinEvent(hook)
        owned = [event for event in observed if event[0] in console_windows | titled]
        assert not owned, owned


@pytest.mark.skipif(
    sys.platform != "win32", reason="Windows configured Bash executable"
)
def test_bash_adapter_keeps_unmodified_descendants_hidden(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    compiler = shutil.which("clang.exe") or shutil.which("gcc.exe")
    if not compiler:
        pytest.skip("the Windows Bash adapter requires clang or gcc")
    target = process.run(
        [compiler, "-dumpmachine"],
        capture_output=True,
        text=True,
        timeout=15,
        check=True,
    )
    flags = (
        ["-Wl,/subsystem:windows"]
        if "msvc" in target.stdout
        else ["-municode", "-mwindows"]
    )
    executable = tmp_path / "hidden-bash.exe"
    process.run(
        [
            compiler,
            "-O2",
            *flags,
            str(ROOT / ".agents/agent/resources/shell/hidden-bash.c"),
            "-o",
            str(executable),
        ],
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    monkeypatch.setenv(
        "TJ_AGENTS_REAL_GIT_BASH", getattr(sys, "_base_executable", sys.executable)
    )
    (tmp_path / "tj_hook.exe").write_bytes(b"untrusted working-directory trap")
    result = subprocess.run(
        [str(executable), str(PROBE), "child"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=tmp_path,
        timeout=20,
        check=True,
    )
    observed = json.loads(result.stdout)
    assert observed["attached"]
    assert not observed["visible"]
    assert not observed["descendant_visible"]
