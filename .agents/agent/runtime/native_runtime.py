"""Make the CLI-owned native pair available before Agent invokes native tools."""

from __future__ import annotations

import os

import plugin_autoupdate
from tj_cli import native_install
from tj_cli.git_environment import native_plugin_environment
from tj_cli.paths import prepend


def missing() -> tuple[str, ...]:
    return native_install.missing_native(native_install.default_bin_dir())


def search_path(value: str) -> str:
    return os.pathsep.join(
        prepend(value.split(os.pathsep), str(native_install.default_bin_dir()))
    )


def prepare_git_environment() -> None:
    """Enable native Git long paths in this process before Git operations."""
    try:
        prepared = native_plugin_environment(os.environ, windows=os.name == "nt")
        for name, value in prepared.items():
            if os.environ.get(name) != value:
                os.environ[name] = value
    except ValueError as error:
        raise RuntimeError(f"invalid process Git configuration: {error}") from error


def expose() -> None:
    """Prepare the current process environment without installing any files."""
    prepare_git_environment()
    if os.environ.get("TJ_BIN_DIR"):
        os.environ["TJ_BIN_DIR"] = str(native_install.default_bin_dir())
    os.environ["PATH"] = search_path(os.environ.get("PATH", os.defpath))


def prepare(*, revision: str | None = None) -> tuple[str, ...]:
    """Repair missing binaries or apply an exact producer revision, then expose them."""
    prepare_git_environment()
    changed: tuple[str, ...] = ()
    if revision is not None or missing():
        directory = plugin_autoupdate.state_directory()
        directory.mkdir(parents=True, exist_ok=True)
        lock = plugin_autoupdate.acquire_lock(directory / "native-cli.lock")
        if lock is None:
            raise RuntimeError(
                "Native CLI installation is already running; retry after it finishes"
            )
        try:
            changed = native_install.ensure_native(
                native_install.default_bin_dir(), revision=revision
            )
        finally:
            lock.close()
    expose()
    return changed
