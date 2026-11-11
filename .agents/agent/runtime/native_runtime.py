"""Make the CLI-owned native pair available before Agent invokes native tools."""

from __future__ import annotations

import os

from tj_cli import native_install
from tj_cli.paths import prepend

NATIVE_REVISION = "3f61c4be63cfba44973d38616a13580e04cf465e"


def pending(*, revision: str | None = None) -> tuple[str, ...]:
    directory = native_install.default_bin_dir()
    if missing := native_install.pending_native(directory):
        return missing
    if revision is not None and (
        reason := native_install.pending(directory, revision=revision)
    ):
        return (reason,)
    return ()


def search_path(value: str) -> str:
    return os.pathsep.join(
        prepend(value.split(os.pathsep), str(native_install.default_bin_dir()))
    )


def expose() -> None:
    """Prepare the current process environment without installing any files."""
    if os.environ.get("TJ_BIN_DIR"):
        os.environ["TJ_BIN_DIR"] = str(native_install.default_bin_dir())
    os.environ["PATH"] = search_path(os.environ.get("PATH", os.defpath))


def prepare(*, revision: str | None = None) -> tuple[str, ...]:
    """Converge compatible binaries or an exact producer revision before admission."""
    changed: tuple[str, ...] = ()
    if revision is not None or pending():
        changed = native_install.ensure_native(
            native_install.default_bin_dir(), revision=revision
        )
        if remaining := pending():
            raise RuntimeError(
                "Native CLI installation is not compatible: "
                + ", ".join(remaining)
                + "; wait for a compatible published CLI release, then retry"
            )
    expose()
    return changed
