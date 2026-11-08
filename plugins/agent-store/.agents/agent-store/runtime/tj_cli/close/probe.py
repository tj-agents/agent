"""Prove one Windows console frontend is visible in one exact UIA tab."""

from __future__ import annotations

import argparse
import ctypes
import json
import subprocess
import sys
import time
import uuid
from ctypes import wintypes
from pathlib import Path
from typing import Any


def _inventory(adapter: Path) -> list[dict[str, object]]:
    result = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(adapter),
            "-Json",
        ],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=3,
        check=False,
    )
    if result.returncode:
        raise OSError(
            result.stderr or result.stdout or "Windows Terminal inventory failed"
        )
    value = json.loads(result.stdout or "[]")
    if not isinstance(value, list):
        raise OSError("Windows Terminal inventory is malformed")
    return [item for item in value if isinstance(item, dict)]


if sys.platform == "win32":

    def _title(kernel32: Any) -> str:
        buffer = ctypes.create_unicode_buffer(32768)
        length = kernel32.GetConsoleTitleW(buffer, len(buffer))
        if not length:
            raise OSError(ctypes.get_last_error(), "GetConsoleTitleW failed")
        return buffer.value

    def _processes(kernel32: Any) -> set[int]:
        size = 16
        while size <= 4096:
            values = (wintypes.DWORD * size)()
            count = kernel32.GetConsoleProcessList(values, size)
            if not count:
                raise OSError(ctypes.get_last_error(), "GetConsoleProcessList failed")
            if count <= size:
                return {int(values[index]) for index in range(count)}
            size = count
        raise OSError("console process list is unexpectedly large")

    def prove(frontend_pid: int, tab_id: str, adapter: Path) -> bool:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.FreeConsole()
        if not kernel32.AttachConsole(frontend_pid):
            raise OSError(ctypes.get_last_error(), "AttachConsole failed")
        original = _title(kernel32)
        challenge = "tj-cli-binding-" + uuid.uuid4().hex
        try:
            if frontend_pid not in _processes(kernel32):
                return False
            if not kernel32.SetConsoleTitleW(challenge):
                raise OSError(ctypes.get_last_error(), "SetConsoleTitleW failed")
            time.sleep(0.1)
            matches = [
                item
                for item in _inventory(adapter)
                if item.get("runtimeId") == tab_id and item.get("title") == challenge
            ]
            return len(matches) == 1
        finally:
            kernel32.SetConsoleTitleW(original)
            kernel32.FreeConsole()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--frontend-pid", type=int, required=True)
    parser.add_argument("--tab-id", required=True)
    parser.add_argument("--inventory-adapter", type=Path, required=True)
    args = parser.parse_args(argv)
    if sys.platform == "win32":
        if not args.inventory_adapter.is_file():
            associated = False
        else:
            try:
                associated = prove(
                    args.frontend_pid, args.tab_id, args.inventory_adapter
                )
            except (OSError, ValueError, subprocess.SubprocessError):
                associated = False
    else:
        associated = False
    print(json.dumps({"associated": associated}))
    return 0 if associated else 2


if __name__ == "__main__":
    raise SystemExit(main())
