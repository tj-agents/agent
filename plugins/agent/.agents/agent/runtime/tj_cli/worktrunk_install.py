from __future__ import annotations

import argparse
import platform
import sys
import tarfile
import zipfile
from pathlib import Path

from tj_cli import archive_install, tool_receipts
from tj_cli.errors import TjCliError
from tj_cli.native_install import default_bin_dir

VERSION = "0.81.0"
RECEIPT = ".tj-worktrunk.json"
ARCHIVES = {
    "Windows": (
        "worktrunk-x86_64-pc-windows-msvc.zip",
        "389b595cb67482085419968939e329d61901cca9e959e4b6c288f01387b768b8",
    ),
    "Linux": (
        "worktrunk-x86_64-unknown-linux-musl.tar.xz",
        "0c253f216985ce9cd7b202a11606b5ce6aa934b1019fd1fbb9b7ce2b47b1c74a",
    ),
}


class WorktrunkInstallError(TjCliError):
    pass


def executable(bin_dir: Path) -> Path:
    return bin_dir / ("git-wt.exe" if platform.system() == "Windows" else "git-wt")


def pending(bin_dir: Path) -> str | None:
    return tool_receipts.pending(
        bin_dir / RECEIPT, VERSION, names=(executable(bin_dir).name,)
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=f"Install worktrunk {VERSION} as git-wt"
    )
    parser.add_argument("--bin-dir", type=Path)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--uninstall", action="store_true")
    arguments = parser.parse_args(argv)
    arguments.bin_dir = arguments.bin_dir or default_bin_dir()
    target = executable(arguments.bin_dir)
    if arguments.uninstall:
        (arguments.bin_dir / RECEIPT).unlink(missing_ok=True)
        if target.exists():
            target.unlink()
            print(f"removed    {target}")
        else:
            print("ok         git-wt (not installed)")
        return 0
    if pending(arguments.bin_dir) is None:
        print(f"ok         git-wt {VERSION}")
        return 0
    if arguments.check:
        print(f"missing    git-wt {VERSION}")
        return 1
    system, machine = platform.system(), platform.machine()
    if system not in ARCHIVES or machine.lower() not in {"amd64", "x86_64"}:
        print(
            f"error      git-wt: no pinned worktrunk build for {system} {machine}",
            file=sys.stderr,
        )
        return 1
    try:
        archive_name, digest = ARCHIVES[system]
        archive_install.install(
            url=f"https://github.com/max-sixty/worktrunk/releases/download/v{VERSION}/{archive_name}",
            digest=digest,
            member=(
                target.name
                if system == "Windows"
                else f"{archive_name.removesuffix('.tar.xz')}/{target.name}"
            ),
            target=target,
            receipt_path=arguments.bin_dir / RECEIPT,
            version=VERSION,
            error_type=WorktrunkInstallError,
        )
    except (
        WorktrunkInstallError,
        OSError,
        KeyError,
        tarfile.TarError,
        zipfile.BadZipFile,
    ) as error:
        print(f"error      git-wt: {error}", file=sys.stderr)
        return 1
    print(f"installed  git-wt {VERSION} ({target})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
