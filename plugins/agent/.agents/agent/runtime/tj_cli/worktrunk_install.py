from __future__ import annotations

import argparse
import hashlib
import io
import platform
import subprocess
import sys
import tarfile
import urllib.request
import zipfile
from pathlib import Path

from tj_cli.native_install import default_bin_dir

VERSION = "0.81.0"
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


class WorktrunkInstallError(RuntimeError):
    pass


def executable(bin_dir: Path) -> Path:
    return bin_dir / ("git-wt.exe" if platform.system() == "Windows" else "git-wt")


def installed_version(path: Path) -> str | None:
    if not path.is_file():
        return None
    try:
        result = subprocess.run(
            [str(path), "--version"],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except OSError:
        return None
    return result.stdout.strip().removeprefix("wt v") or None


def binary(archive_name: str, contents: bytes) -> bytes:
    if archive_name.endswith(".zip"):
        with zipfile.ZipFile(io.BytesIO(contents)) as archive:
            return archive.read("git-wt.exe")
    with tarfile.open(fileobj=io.BytesIO(contents), mode="r:xz") as archive:
        member = archive.getmember(f"{archive_name.removesuffix('.tar.xz')}/git-wt")
        extracted = archive.extractfile(member)
        if extracted is None:
            raise WorktrunkInstallError("the worktrunk archive has no git-wt")
        return extracted.read()


def download(archive_name: str, digest: str) -> bytes:
    url = f"https://github.com/max-sixty/worktrunk/releases/download/v{VERSION}/{archive_name}"
    with urllib.request.urlopen(url, timeout=600) as response:
        contents: bytes = response.read()
    if hashlib.sha256(contents).hexdigest() != digest:
        raise WorktrunkInstallError(f"{archive_name} failed SHA-256 verification")
    return contents


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=f"Install worktrunk {VERSION} as git-wt"
    )
    parser.add_argument("--bin-dir", type=Path, default=default_bin_dir())
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--uninstall", action="store_true")
    arguments = parser.parse_args(argv)
    target = executable(arguments.bin_dir)
    if arguments.uninstall:
        if target.exists():
            target.unlink()
            print(f"removed    {target}")
        else:
            print("ok         git-wt (not installed)")
        return 0
    if installed_version(target) == VERSION:
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
        contents = binary(archive_name, download(archive_name, digest))
        arguments.bin_dir.mkdir(parents=True, exist_ok=True)
        staged = target.with_name(target.name + ".new")
        staged.write_bytes(contents)
        staged.chmod(0o755)
        staged.replace(target)
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
