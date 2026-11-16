"""Install the pinned portable PowerShell runtime without modifying PATH."""

from __future__ import annotations

import hashlib
import importlib
import io
import os
import shutil
import stat
import tarfile
import tempfile
import time
import urllib.request
import zipfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path, PurePosixPath

from tj_cli import host_os, tool_receipts
from tj_cli.errors import TjCliError

VERSION = "7.7.0"
RECEIPT = ".tj-powershell.json"
ASSETS = {
    ("Windows", "x64"): (
        "PowerShell-7.7.0-win-x64.zip",
        "7c74e03b20a75b59715847439d046a6214b1863ee79b187872980f139bb4a05f",
    ),
    ("Windows", "arm64"): (
        "PowerShell-7.7.0-win-arm64.zip",
        "5dc7440cd98f0ba786a44d7db9205df5058474fd4db143caf7171e8375f34f75",
    ),
    ("Linux", "x64"): (
        "powershell-7.7.0-linux-x64.tar.gz",
        "c89d9c682d2c403dfe726f3df948e359675235a5bd6fab535776de69729ca0cb",
    ),
    ("Linux", "arm64"): (
        "powershell-7.7.0-linux-arm64.tar.gz",
        "e59c89776eaa59790d44b3edd14277435225ab08b6e7603763101b89de83d935",
    ),
}


class PowerShellInstallError(TjCliError):
    pass


def _asset() -> tuple[str, str]:
    system, machine = host_os.system(), host_os.machine().casefold()
    architecture = {
        "amd64": "x64",
        "x86_64": "x64",
        "arm64": "arm64",
        "aarch64": "arm64",
    }.get(machine)
    try:
        if architecture is None:
            raise KeyError(machine)
        return ASSETS[system, architecture]
    except KeyError as error:
        raise PowerShellInstallError(
            f"No pinned PowerShell build for {system} {machine}"
        ) from error


def _root(bin_dir: Path) -> Path:
    return bin_dir / "powershell" / VERSION


def executable(bin_dir: Path) -> Path:
    return _root(bin_dir) / ("pwsh.exe" if host_os.system() == "Windows" else "pwsh")


def _safe_name(name: str) -> bool:
    path = PurePosixPath(name)
    return (
        bool(name)
        and not path.is_absolute()
        and ".." not in path.parts
        and "\\" not in name
        and ":" not in name
    )


def _link(path: Path) -> bool:
    return path.is_symlink() or path.is_junction()


def pending(bin_dir: Path) -> str | None:
    root = _root(bin_dir)
    previous = root.parent / ".previous"
    for path in (previous, _cleanup_receipt(previous), _cleanup_receipt(root)):
        if path.exists() or _link(path):
            return f"interrupted PowerShell installation: {path}"
    return _current_pending(bin_dir)


def _current_pending(bin_dir: Path) -> str | None:
    name, digest = _asset()
    root = _root(bin_dir)
    if _link(root) or _link(root.parent):
        return f"PowerShell directory is a symbolic link: {root}"
    receipt = tool_receipts.read(root / RECEIPT)
    target = executable(bin_dir)
    if (
        receipt is None
        or receipt.version != VERSION
        or receipt.identity != name
        or receipt.archive_digest != digest
    ):
        return f"missing or invalid PowerShell receipt: {root / RECEIPT}"
    if target.name not in receipt.hashes:
        return f"missing PowerShell executable in receipt: {target}"
    for name, expected in receipt.hashes.items():
        if not _safe_name(name):
            return "invalid PowerShell receipt path"
        path = root.joinpath(*PurePosixPath(name).parts)
        if (
            any(
                _link(part)
                for part in (path, *path.parents)
                if part.is_relative_to(root)
            )
            or not path.is_file()
        ):
            return f"missing or invalid PowerShell file: {path}"
        try:
            actual = tool_receipts.sha256(path)
        except OSError:
            return f"cannot read PowerShell file: {path}"
        if actual != expected:
            return f"PowerShell file changed: {path}"
    if host_os.system() != "Windows" and not os.access(target, os.X_OK):
        return f"PowerShell is not executable: {target}"
    return None


def _extract(data: bytes, name: str, destination: Path) -> None:
    if name.endswith(".zip"):
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            for entry in archive.infolist():
                if not _safe_name(entry.filename) or stat.S_ISLNK(
                    entry.external_attr >> 16
                ):
                    raise PowerShellInstallError(
                        f"unsafe archive entry: {entry.filename}"
                    )
            archive.extractall(destination)
    else:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
            for member in archive.getmembers():
                if not _safe_name(member.name) or not (
                    member.isfile() or member.isdir()
                ):
                    raise PowerShellInstallError(f"unsafe archive entry: {member.name}")
            archive.extractall(destination, filter="data")


def _assert_owned(root: Path, receipt_path: Path | None = None) -> None:
    receipt = tool_receipts.read(receipt_path or root / RECEIPT)
    if receipt is None or any(not _safe_name(name) for name in receipt.hashes):
        raise PowerShellInstallError(
            f"Refusing to replace an unowned PowerShell directory: {root}"
        )
    files = set(receipt.hashes) | {RECEIPT}
    directories: set[str] = set()
    for name in files:
        directories.update(
            parent.as_posix()
            for parent in PurePosixPath(name).parents
            if str(parent) != "."
        )
    for path in root.rglob("*"):
        relative = path.relative_to(root).as_posix()
        if (
            _link(path)
            or (path.is_dir() and relative not in directories)
            or (not path.is_dir() and relative not in files)
        ):
            raise PowerShellInstallError(
                f"Refusing to remove an unowned PowerShell path: {path}"
            )


@contextmanager
def _locked(parent: Path) -> Iterator[None]:
    parent.mkdir(parents=True, exist_ok=True)
    with (parent / ".install.lock").open("a+b") as lock:
        if lock.tell() == 0:
            lock.write(b"\0")
            lock.flush()
        deadline = time.monotonic() + 30
        while True:
            try:
                lock.seek(0)
                if os.name == "nt":
                    msvcrt = importlib.import_module("msvcrt")
                    msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    fcntl = importlib.import_module("fcntl")
                    fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except (BlockingIOError, PermissionError):
                if time.monotonic() >= deadline:
                    raise PowerShellInstallError(
                        "Timed out waiting for PowerShell installation lock"
                    ) from None
                time.sleep(0.05)
        try:
            yield
        finally:
            lock.seek(0)
            if os.name == "nt":
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def _cleanup_receipt(root: Path) -> Path:
    return root.parent / f"{root.name}.cleanup-receipt"


def _clear_marker(marker: Path) -> None:
    if _link(marker):
        raise PowerShellInstallError(f"Unsafe PowerShell cleanup receipt: {marker}")
    if marker.exists():
        if tool_receipts.read(marker) is None:
            raise PowerShellInstallError(
                f"Invalid PowerShell cleanup receipt: {marker}"
            )
        marker.unlink()


def _remove_previous(previous: Path, parent: Path) -> None:
    if (
        _link(previous)
        or _link(parent)
        or previous.resolve().parent != parent.resolve()
    ):
        raise PowerShellInstallError(
            f"Unsafe previous PowerShell directory: {previous}"
        )
    marker = _cleanup_receipt(previous)
    if _link(marker):
        raise PowerShellInstallError(f"Unsafe PowerShell cleanup receipt: {marker}")
    if not marker.exists():
        _assert_owned(previous)
        receipt = tool_receipts.read(previous / RECEIPT)
        if receipt is None:
            raise PowerShellInstallError(
                f"Missing PowerShell ownership receipt: {previous}"
            )
        tool_receipts.write(marker, receipt)
    _assert_owned(previous, marker)
    shutil.rmtree(previous)
    marker.unlink()


def _recover(bin_dir: Path, root: Path) -> None:
    previous = root.parent / ".previous"
    previous_marker = _cleanup_receipt(previous)
    root_marker = _cleanup_receipt(root)
    if _link(previous_marker) or _link(root_marker):
        raise PowerShellInstallError("Unsafe PowerShell cleanup receipt")
    if _link(previous):
        raise PowerShellInstallError(
            f"Unsafe previous PowerShell directory: {previous}"
        )
    if not previous.exists():
        if previous_marker.exists() or root_marker.exists():
            if _current_pending(bin_dir) is not None:
                raise PowerShellInstallError(
                    "Cannot finish interrupted PowerShell cleanup "
                    "without a healthy runtime"
                )
            _clear_marker(previous_marker)
            _clear_marker(root_marker)
        return
    _assert_owned(previous, previous_marker if previous_marker.exists() else None)
    if root.exists():
        if _current_pending(bin_dir) is None:
            _remove_previous(previous, root.parent)
            _clear_marker(root_marker)
            return
        _remove_previous(root, root.parent)
    if previous_marker.exists():
        raise PowerShellInstallError(
            "Cannot restore a partially removed PowerShell runtime"
        )
    previous.rename(root)
    _clear_marker(root_marker)


def ensure(bin_dir: Path) -> Path:
    """Install the verified portable runtime, or return the current executable."""
    root = _root(bin_dir)
    if _link(root) or _link(root.parent):
        raise PowerShellInstallError(f"PowerShell directory is a symbolic link: {root}")
    with _locked(root.parent):
        _recover(bin_dir, root)
        return _ensure_locked(bin_dir)


def _ensure_locked(bin_dir: Path) -> Path:
    if pending(bin_dir) is None:
        return executable(bin_dir)
    name, digest = _asset()
    url = (
        f"https://github.com/PowerShell/PowerShell/releases/download/v{VERSION}/{name}"
    )
    with urllib.request.urlopen(url, timeout=600) as response:
        data = response.read()
    if hashlib.sha256(data).hexdigest() != digest:
        raise PowerShellInstallError(f"{name} failed SHA-256 verification")
    root = _root(bin_dir)
    if _link(root) or _link(root.parent):
        raise PowerShellInstallError(f"PowerShell directory is a symbolic link: {root}")
    root.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=root.parent, prefix=".staged-") as temporary:
        staged = Path(temporary) / VERSION
        staged.mkdir()
        _extract(data, name, staged)
        binary = staged / executable(bin_dir).name
        if not binary.is_file():
            raise PowerShellInstallError(f"{name} has no {binary.name}")
        if host_os.system() != "Windows":
            binary.chmod(binary.stat().st_mode | stat.S_IXUSR)
        hashes = {
            path.relative_to(staged).as_posix(): tool_receipts.sha256(path)
            for path in staged.rglob("*")
            if path.is_file()
        }
        tool_receipts.write(
            staged / RECEIPT, tool_receipts.Receipt(VERSION, hashes, name, digest)
        )
        old = root.parent / ".previous"
        if root.exists():
            _assert_owned(root)
            root.rename(old)
        try:
            staged.rename(root)
        except OSError:
            if old.exists():
                try:
                    old.rename(root)
                except OSError as restore_error:
                    raise PowerShellInstallError(
                        "PowerShell replacement failed; previous runtime "
                        f"preserved at {old}"
                    ) from restore_error
            raise
        if old.exists():
            _remove_previous(old, root.parent)
    return executable(bin_dir)
