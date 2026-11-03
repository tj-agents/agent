import ctypes
import faulthandler
import importlib
import json
import os
import subprocess
import sys
import time
import traceback
from pathlib import Path
from typing import Any


def write(path: Path, value: dict[str, Any]) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value), encoding="utf-8")
    temporary.replace(path)


def visible_windows() -> list[dict[str, Any]]:
    if os.name != "nt":
        return []
    from ctypes import wintypes

    user = ctypes.WinDLL("user32", use_last_error=True)
    user.IsWindowVisible.argtypes = [wintypes.HWND]
    user.IsWindowVisible.restype = wintypes.BOOL
    user.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user.GetWindowThreadProcessId.argtypes = [
        wintypes.HWND,
        ctypes.POINTER(wintypes.DWORD),
    ]
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]
    visible: list[dict[str, Any]] = []

    def collect(handle: int, _parameter: int) -> bool:
        if user.IsWindowVisible(handle):
            class_name = ctypes.create_unicode_buffer(256)
            user.GetClassNameW(handle, class_name, len(class_name))
            pid = wintypes.DWORD()
            user.GetWindowThreadProcessId(handle, ctypes.byref(pid))
            visible.append(
                {"handle": handle, "pid": pid.value, "class": class_name.value}
            )
        return True

    user.EnumWindows(callback_type(collect), 0)
    return visible


def console_state() -> dict[str, Any]:
    if os.name != "nt":
        return {"console": None, "visible": False, "windows": []}
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    user = ctypes.WinDLL("user32", use_last_error=True)
    kernel.GetConsoleWindow.restype = wintypes.HWND
    user.IsWindowVisible.argtypes = [wintypes.HWND]
    user.IsWindowVisible.restype = wintypes.BOOL
    window = int(kernel.GetConsoleWindow() or 0)
    return {
        "console": window,
        "visible": bool(user.IsWindowVisible(window)),
        "windows": [
            entry
            for entry in visible_windows()
            if entry["pid"] == os.getpid() or entry["handle"] == window
        ],
    }


def main() -> None:
    mode, scripts, directory = sys.argv[1:4]
    state = Path(directory)
    from tj_cli import processes

    info = processes.process_info(os.getpid())
    assert info is not None
    identity = {"pid": info.pid, "started_at": info.started_at}
    if mode == "frontend":
        sys.path.insert(0, scripts)
        session_close = importlib.import_module("session_close")
        os.environ["AGENT_STATE_DIRECTORY"] = str(state)
        write(state / "frontend.json", identity)
        session_close.start_detached(
            Path(__file__), ["observer", scripts, directory], stage=False
        )
        deadline = time.monotonic() + 20
        while not (state / "started.json").exists():
            if time.monotonic() >= deadline:
                raise TimeoutError("observer did not acknowledge startup")
            time.sleep(0.05)
        return
    if mode == "child":
        write(state / "child-started.json", identity)
        observations = []
        until = time.monotonic() + 2
        while time.monotonic() < until:
            observations.append(console_state())
            time.sleep(0.05)
        write(state / "child.json", {"observations": observations})
        return
    frontend = json.loads((state / "frontend.json").read_text(encoding="utf-8"))
    observations = [console_state()]
    write(state / "started.json", identity)
    deadline = time.monotonic() + 30
    while processes.matches(frontend):
        if time.monotonic() >= deadline:
            raise TimeoutError("dummy frontend did not exit")
        observations.append(console_state())
        time.sleep(0.05)
    child = subprocess.Popen(
        [sys.executable, str(Path(__file__).resolve()), "child", scripts, directory]
    )
    while child.poll() is None:
        if time.monotonic() >= deadline:
            child.kill()
            child.wait(timeout=5)
            raise TimeoutError("dummy descendant did not exit")
        observations.append(console_state())
        time.sleep(0.05)
    write(
        state / "finished.json",
        {
            "survived_frontend": True,
            "child_returncode": child.returncode,
            "observations": observations,
        },
    )


if __name__ == "__main__":
    with (Path(sys.argv[3]) / (sys.argv[1] + "-traceback.log")).open(
        "w", encoding="utf-8"
    ) as traceback_output:
        faulthandler.dump_traceback_later(10, repeat=True, file=traceback_output)
        try:
            main()
        finally:
            faulthandler.cancel_dump_traceback_later()
            if sys.exception() is not None:
                write(
                    Path(sys.argv[3]) / "failure.json",
                    {"error": traceback.format_exc()},
                )
