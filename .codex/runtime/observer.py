"""Start the daemon-scoped Codex plugin update observer when it is needed."""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

from tj_cli import native_install, process


def socket_path(codex_home: Path) -> Path:
    return codex_home / "app-server-control" / "app-server-control.sock"


def command(codex_home: Path, cwd: Path, action: str) -> list[str]:
    executable = native_install.default_bin_dir() / (
        "tj.exe" if os.name == "nt" else "tj"
    )
    return [
        str(executable),
        "observe-codex-updates",
        "--socket",
        str(socket_path(codex_home)),
        "--cwd",
        str(cwd),
        action,
    ]


def status(
    codex_home: Path,
    cwd: Path,
    *,
    run: Callable[..., subprocess.CompletedProcess[Any]] = process.run,
) -> bool:
    if not socket_path(codex_home).exists():
        return True
    result = run(
        command(codex_home, cwd, "--status"),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=10,
        check=False,
    )
    if result.returncode in (0, 3):
        return True
    if result.returncode == 1:
        return False
    detail = (result.stderr or result.stdout).strip()
    raise RuntimeError(f"Codex update observer status failed: {detail}")


def ensure(
    codex_home: Path,
    cwd: Path,
    *,
    run: Callable[..., subprocess.CompletedProcess[Any]] = process.run,
) -> bool:
    if status(codex_home, cwd, run=run):
        return False
    result = run(
        command(codex_home, cwd, "--start"),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=35,
        check=False,
        preserve_descendants=True,
    )
    if result.returncode == 3:
        return False
    if result.returncode:
        detail = (result.stderr or result.stdout).strip()
        raise RuntimeError(f"Codex update observer failed to start: {detail}")
    if not status(codex_home, cwd, run=run):
        raise RuntimeError("Codex update observer did not become ready")
    return True
