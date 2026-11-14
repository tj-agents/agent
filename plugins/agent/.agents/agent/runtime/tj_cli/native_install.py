"""Install the paired native ``tj`` executables without shell-specific behavior."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from pathlib import Path
from typing import BinaryIO, Final

from tj_cli import host_os, tool_receipts
from tj_cli.errors import TjCliError

VERSION = "0.2.0"
RECEIPT = ".tj-native.json"
RETIRED = ".tj-retired"
_REPOSITORY: Final = "tj-agents/cli"
_MINIMUM_NATIVE_VERSION: Final = (0, 2, 0)
_VERSION_TIMEOUT: Final = 5.0
_NATIVE_VERSION = re.compile(r"\Atj_hook (0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)\n?\Z")


class NativeInstallError(TjCliError):
    """A native CLI pair could not be safely inspected or installed."""


def pending(bin_dir: Path, *, revision: str | None = None) -> str | None:
    if reason := tool_receipts.pending(
        bin_dir / RECEIPT, VERSION, names=_binary_names()
    ):
        return reason
    receipt = tool_receipts.read(bin_dir / RECEIPT)
    if (
        receipt is None
        or receipt.identity is None
        or len(receipt.identity) != 40
        or any(character not in "0123456789abcdef" for character in receipt.identity)
    ):
        return "native receipt has no immutable source revision"
    if revision is not None and receipt.identity != revision:
        return f"recorded revision {receipt.identity}; expected {revision}"
    return None


def default_bin_dir() -> Path:
    """Return the native CLI directory, honoring ``TJ_BIN_DIR`` when set."""
    if configured := os.environ.get("TJ_BIN_DIR"):
        return Path(configured).expanduser().resolve()
    if host_os.system() == "Windows":
        local_app_data = os.environ.get("LOCALAPPDATA")
        if not local_app_data:
            raise NativeInstallError(
                "LOCALAPPDATA is required to choose the Windows bin directory"
            )
        return Path(local_app_data) / "Programs" / "tj"
    return Path.home() / ".local" / "bin"


def missing_native(directory: Path) -> tuple[str, ...]:
    """Return the paired executable names absent from *directory*, without writing."""
    return tuple(
        name for name in _binary_names() if not _is_executable(directory / name)
    )


def pending_native(directory: Path) -> tuple[str, ...]:
    """Return native names that are missing or do not support the current runtime."""
    if missing := missing_native(directory):
        return missing
    hook = directory.resolve() / _binary_names()[1]
    try:
        result = _run([str(hook), "--version"], timeout=_VERSION_TIMEOUT)
    except (NativeInstallError, UnicodeError):
        return _binary_names()
    version = _NATIVE_VERSION.fullmatch(result.stdout)
    if version is None or tuple(map(int, version.groups())) < _MINIMUM_NATIVE_VERSION:
        return _binary_names()
    return ()


def ensure_native(
    directory: Path,
    *,
    source: Path | None = None,
    revision: str | None = None,
) -> tuple[str, ...]:
    """Install a verified pair and return names replaced; return ``()`` for a no-op."""
    expected_revision = _validate_revision(revision) if revision is not None else None
    binaries: dict[str, bytes] | None
    if source is not None and shutil.which("cargo") is not None:
        identity, binaries = _build_source(source)
        archive_digest = None
    else:
        identity, binaries, archive_digest = _download_release(
            expected_revision, directory
        )
        if binaries is None:
            return ()
    if expected_revision is not None and identity != expected_revision:
        raise NativeInstallError(
            f"requested revision {expected_revision} does not match {identity}"
        )

    expected_hashes = {
        name: _sha256_bytes(contents) for name, contents in binaries.items()
    }
    receipt = tool_receipts.read(directory / RECEIPT)
    if (
        receipt is not None
        and receipt.version == VERSION
        and receipt.identity == identity
        and receipt.hashes == expected_hashes
        and all(
            _is_executable(directory / name)
            and tool_receipts.sha256(directory / name) == digest
            for name, digest in expected_hashes.items()
        )
    ):
        return ()

    directory.mkdir(parents=True, exist_ok=True)
    if not directory.is_dir():
        raise NativeInstallError(f"native bin path is not a directory: {directory}")
    _replace_pair(directory, binaries, identity, expected_hashes, archive_digest)
    return _binary_names()


def uninstall_native(directory: Path) -> tuple[str, ...]:
    """Remove only receipt-owned native files, returning the names removed."""
    receipt = tool_receipts.read(directory / RECEIPT)
    if receipt is None:
        if any((directory / name).exists() for name in _binary_names()):
            raise NativeInstallError(
                "native binaries have no ownership receipt; "
                "run install before uninstall"
            )
        return ()
    hashes = receipt.hashes
    removed: list[str] = []
    for name in _binary_names():
        digest = hashes.get(name)
        target = directory / name
        if (
            digest is not None
            and target.is_file()
            and tool_receipts.sha256(target) == digest
        ):
            target.unlink()
            removed.append(name)
    receipt_path = directory / RECEIPT
    if receipt_path.is_file():
        receipt_path.unlink()
    retired = directory / RETIRED
    if retired.is_dir():
        _prune(retired)
        if not any(retired.iterdir()):
            retired.rmdir()
    return tuple(removed)


def _binary_names() -> tuple[str, str]:
    suffix = ".exe" if host_os.system() == "Windows" else ""
    return (f"tj{suffix}", f"tj_hook{suffix}")


def _asset_name() -> str:
    system = host_os.system()
    if host_os.machine().lower() not in {"amd64", "x86_64"}:
        raise NativeInstallError(
            f"unsupported native architecture: {host_os.machine()}"
        )
    if system == "Windows":
        return "tj-x86_64-pc-windows-msvc.zip"
    if system == "Linux":
        return "tj-x86_64-unknown-linux-gnu.tar.gz"
    raise NativeInstallError(f"unsupported native install platform: {system}")


def _is_executable(path: Path) -> bool:
    return path.is_file() and (
        host_os.system() == "Windows" or os.access(path, os.X_OK)
    )


def _validate_revision(revision: str) -> str:
    if len(revision) != 40 or any(
        character not in "0123456789abcdef" for character in revision
    ):
        raise NativeInstallError(
            "native revision must be a 40-character lowercase SHA-1"
        )
    return revision


def _build_source(source: Path) -> tuple[str, dict[str, bytes]]:
    source = source.resolve()
    manifest = source / "Cargo.toml"
    if not manifest.is_file():
        raise NativeInstallError(f"native source is missing Cargo.toml: {source}")
    identity_result = _run(["git", "-C", str(source), "rev-parse", "HEAD"])
    identity = _validate_revision(identity_result.stdout.strip())
    _run(
        [
            "cargo",
            "build",
            "--release",
            "--locked",
            "--quiet",
            "--manifest-path",
            str(manifest),
        ],
        cwd=source,
    )
    configured_target = os.environ.get("CARGO_TARGET_DIR")
    target = Path(configured_target) if configured_target else source / "target"
    if configured_target and not target.is_absolute():
        target = source / target
    return identity, _read_binary_contents(target / "release")


def _download_release(
    expected_revision: str | None, directory: Path
) -> tuple[str, dict[str, bytes] | None, str]:
    if shutil.which("gh") is None:
        raise NativeInstallError(
            "native install needs cargo with --source or the GitHub CLI (gh)"
        )
    endpoint = f"tags/main-{expected_revision}" if expected_revision else "latest"
    release = _gh_json(f"repos/{_REPOSITORY}/releases/{endpoint}")
    target = release.get("target_commitish")
    if not isinstance(target, str):
        raise NativeInstallError("latest native release has no immutable target SHA")
    identity = _validate_revision(target)
    if (
        release.get("draft") is not False
        or release.get("tag_name") != f"main-{identity}"
    ):
        raise NativeInstallError(
            "native release is not an immutable published main revision"
        )
    if expected_revision is not None and identity != expected_revision:
        raise NativeInstallError(
            f"requested revision {expected_revision} does not match release {identity}"
        )
    asset_name = _asset_name()
    assets = release.get("assets")
    if not isinstance(assets, list):
        raise NativeInstallError("latest native release has no assets")
    asset = next(
        (
            item
            for item in assets
            if isinstance(item, dict) and item.get("name") == asset_name
        ),
        None,
    )
    if asset is None:
        raise NativeInstallError(f"latest native release is missing {asset_name}")
    asset_id = asset.get("id")
    digest = asset.get("digest")
    if not isinstance(asset_id, int) or not isinstance(digest, str):
        raise NativeInstallError(
            f"release asset {asset_name} has no GitHub SHA-256 digest"
        )
    prefix, separator, expected_digest = digest.partition(":")
    if (
        prefix != "sha256"
        or not separator
        or not tool_receipts.is_sha256(expected_digest)
    ):
        raise NativeInstallError(
            f"release asset {asset_name} has an invalid GitHub digest"
        )
    receipt = tool_receipts.read(directory / RECEIPT)
    if (
        receipt is not None
        and receipt.version == VERSION
        and receipt.identity == identity
        and receipt.archive_digest == expected_digest
        and set(receipt.hashes) == set(_binary_names())
        and all(
            _is_executable(directory / name)
            and tool_receipts.sha256(directory / name) == receipt.hashes[name]
            for name in _binary_names()
        )
    ):
        return identity, None, expected_digest
    with tempfile.TemporaryDirectory(prefix="tj-native-") as temporary:
        root = Path(temporary)
        archive = root / asset_name
        _download_asset(asset_id, archive)
        if tool_receipts.sha256(archive) != expected_digest:
            raise NativeInstallError(
                f"release asset {asset_name} failed SHA-256 verification"
            )
        extracted = root / "extracted"
        extracted.mkdir()
        _extract_archive(archive, extracted)
        return (
            identity,
            _read_binary_contents(extracted, require_executable=False),
            expected_digest,
        )


def _gh_json(endpoint: str) -> dict[str, object]:
    result = _run(["gh", "api", "--hostname", "github.com", endpoint])
    try:
        value = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise NativeInstallError("gh returned invalid release metadata") from error
    if not isinstance(value, dict):
        raise NativeInstallError("gh returned malformed release metadata")
    return value


def _download_asset(asset_id: int, destination: Path) -> None:
    command = [
        "gh",
        "api",
        "--hostname",
        "github.com",
        "--header",
        "Accept: application/octet-stream",
        f"repos/{_REPOSITORY}/releases/assets/{asset_id}",
    ]
    with destination.open("wb") as stream:
        _run_download(command, stream)


def _extract_archive(archive: Path, destination: Path) -> None:
    expected = set(_binary_names())
    if archive.suffix == ".zip":
        with zipfile.ZipFile(archive) as contents:
            entries = contents.infolist()
            names = {entry.filename for entry in entries}
            if (
                len(entries) != len(expected)
                or names != expected
                or any(_zip_is_link(entry) for entry in entries)
            ):
                raise NativeInstallError("native zip has unexpected or linked entries")
            contents.extractall(destination)
        return
    with tarfile.open(archive, "r:gz") as contents:
        tar_entries = contents.getmembers()
        names = {entry.name for entry in tar_entries}
        if (
            len(tar_entries) != len(expected)
            or names != expected
            or any(not entry.isfile() for entry in tar_entries)
        ):
            raise NativeInstallError("native tarball has unexpected or linked entries")
        for entry in tar_entries:
            extracted = destination / entry.name
            source = contents.extractfile(entry)
            if source is None:
                raise NativeInstallError(
                    f"native archive entry cannot be read: {entry.name}"
                )
            with source:
                extracted.write_bytes(source.read())


def _zip_is_link(entry: zipfile.ZipInfo) -> bool:
    return stat.S_ISLNK(entry.external_attr >> 16)


def _read_binaries(
    directory: Path, *, require_executable: bool = True
) -> dict[str, Path]:
    binaries: dict[str, Path] = {}
    for name in _binary_names():
        path = directory / name
        if not path.is_file() or (require_executable and not _is_executable(path)):
            raise NativeInstallError(
                f"native binary is missing or not executable: {path}"
            )
        binaries[name] = path
    return binaries


def _read_binary_contents(
    directory: Path, *, require_executable: bool = True
) -> dict[str, bytes]:
    return {
        name: path.read_bytes()
        for name, path in _read_binaries(
            directory, require_executable=require_executable
        ).items()
    }


def _replace_pair(
    directory: Path,
    binaries: dict[str, bytes],
    identity: str,
    hashes: dict[str, str],
    archive_digest: str | None = None,
) -> None:
    # Reserved names are files, never containers for user data. Check every name
    # before moving any target into the disposable transaction directory.
    for name in (*_binary_names(), RECEIPT):
        target = directory / name
        if target.is_symlink() or (target.exists() and not target.is_file()):
            raise NativeInstallError(
                f"native install target must be a regular file: {target}"
            )
    # A running Windows executable can be renamed but not deleted, so old binaries
    # leave through a folder outside the temporary stage and are pruned once free.
    retired = directory / RETIRED
    retired.mkdir(exist_ok=True)
    token = f"{os.getpid()}.{os.urandom(4).hex()}"
    with tempfile.TemporaryDirectory(prefix=".tj-native-", dir=directory) as temporary:
        stage = Path(temporary)
        staged = stage / "new"
        staged.mkdir()
        for name, binary in binaries.items():
            target = staged / name
            target.write_bytes(binary)
            if host_os.system() != "Windows":
                target.chmod(0o755)
        tool_receipts.write(
            staged / RECEIPT,
            tool_receipts.Receipt(VERSION, hashes, identity, archive_digest),
        )
        moved: list[str] = []
        replaced: list[str] = []
        try:
            for name in (*_binary_names(), RECEIPT):
                target = directory / name
                if target.exists():
                    target.replace(retired / f"{name}.{token}")
                    moved.append(name)
            for name in (*_binary_names(), RECEIPT):
                (staged / name).replace(directory / name)
                replaced.append(name)
        except OSError as error:
            for name in reversed(replaced):
                (directory / name).unlink(missing_ok=True)
            for name in moved:
                backup = retired / f"{name}.{token}"
                if backup.exists():
                    backup.replace(directory / name)
            raise NativeInstallError(
                "native pair replacement failed; previous pair restored"
            ) from error
    _prune(retired)


def _prune(retired: Path) -> None:
    for entry in retired.iterdir():
        try:
            entry.unlink()
        except OSError:
            continue


def _sha256_bytes(contents: bytes) -> str:
    return hashlib.sha256(contents).hexdigest()


def _run(
    command: list[str],
    *,
    cwd: Path | None = None,
    timeout: float | None = None,
) -> subprocess.CompletedProcess[str]:
    try:
        if sys.platform == "win32":
            result = subprocess.run(
                command,
                check=True,
                cwd=cwd,
                capture_output=True,
                text=True,
                creationflags=subprocess.CREATE_NO_WINDOW,
                timeout=timeout,
            )
        else:
            result = subprocess.run(
                command,
                check=True,
                cwd=cwd,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        return result
    except subprocess.TimeoutExpired as error:
        raise NativeInstallError(
            f"native install command timed out: {command[0]}"
        ) from error
    except (OSError, subprocess.CalledProcessError) as error:
        raise _command_error(command[0], error) from error


def _run_download(command: list[str], stream: BinaryIO) -> None:
    try:
        if sys.platform == "win32":
            subprocess.run(
                command,
                check=True,
                stdout=stream,
                stderr=subprocess.PIPE,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
        else:
            subprocess.run(command, check=True, stdout=stream, stderr=subprocess.PIPE)
    except (OSError, subprocess.CalledProcessError) as error:
        raise _command_error(command[0], error) from error


def _command_error(
    command: str, error: OSError | subprocess.CalledProcessError
) -> NativeInstallError:
    detail = error.stderr if isinstance(error, subprocess.CalledProcessError) else None
    suffix = f": {detail.strip()}" if isinstance(detail, str) and detail.strip() else ""
    return NativeInstallError(f"native install command failed: {command}{suffix}")


def _main() -> int:
    parser = argparse.ArgumentParser(description="install verified tj native binaries")
    parser.add_argument("--bin-dir", type=Path)
    parser.add_argument("--source", type=Path)
    parser.add_argument("--revision")
    operation = parser.add_mutually_exclusive_group()
    operation.add_argument("--check", action="store_true")
    operation.add_argument("--uninstall", action="store_true")
    arguments = parser.parse_args()
    arguments.bin_dir = arguments.bin_dir or default_bin_dir()
    try:
        if arguments.check:
            pending = pending_native(arguments.bin_dir)
            if pending:
                print(
                    f"native binaries need installation or update: {', '.join(pending)}"
                )
                return 1
            print("ok native binaries")
            return 0
        if arguments.uninstall:
            removed = uninstall_native(arguments.bin_dir)
            print(
                "removed native binaries"
                if removed
                else "ok native binaries (not installed)"
            )
            return 0
        changed = ensure_native(
            arguments.bin_dir, source=arguments.source, revision=arguments.revision
        )
        if pending := pending_native(arguments.bin_dir):
            raise NativeInstallError(
                "installed native binaries remain incompatible: "
                + ", ".join(pending)
                + "; install a compatible release or build the current CLI source"
            )
        print(
            f"installed native binaries: {', '.join(changed)}"
            if changed
            else "ok native binaries"
        )
        return 0
    except NativeInstallError as error:
        print(f"native install failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(_main())
