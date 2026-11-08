from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


class HandyInstallError(RuntimeError):
    pass


def _registered_windows_handy() -> Path | None:
    registry = importlib.import_module("winreg")
    key_name = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\Handy"
    for hive in (registry.HKEY_CURRENT_USER, registry.HKEY_LOCAL_MACHINE):
        for view in (registry.KEY_WOW64_64KEY, registry.KEY_WOW64_32KEY):
            try:
                with registry.OpenKey(
                    hive, key_name, 0, registry.KEY_READ | view
                ) as key:
                    location, _ = registry.QueryValueEx(key, "InstallLocation")
            except FileNotFoundError:
                continue
            if isinstance(location, str) and location:
                executable = Path(location.strip('"')) / "handy.exe"
                if executable.is_file():
                    return executable
    return None


def find_handy() -> Path | None:
    if executable := shutil.which("handy"):
        return Path(executable)
    if platform.system() == "Windows":
        if registered := _registered_windows_handy():
            return registered
        for variable in ("LOCALAPPDATA", "ProgramW6432", "ProgramFiles"):
            if directory := os.environ.get(variable):
                executable_path = Path(directory) / "Handy" / "handy.exe"
                if executable_path.is_file():
                    return executable_path
    return None


def _run(arguments: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        arguments,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=600,
    )


def _install_windows() -> None:
    winget = shutil.which("winget")
    if winget is None:
        raise HandyInstallError(
            "install Windows App Installer (winget), then rerun setup"
        )
    _run(
        [
            winget,
            "install",
            "--id",
            "cjpais.Handy",
            "--exact",
            "--source",
            "winget",
            "--scope",
            "user",
            "--silent",
            "--disable-interactivity",
            "--accept-source-agreements",
            "--accept-package-agreements",
        ]
    )


def _linux_package() -> tuple[str, str]:
    architecture = platform.machine().lower()
    if architecture not in {"amd64", "x86_64", "arm64", "aarch64"}:
        raise HandyInstallError(f"unsupported Handy architecture: {architecture}")
    arm = architecture in {"arm64", "aarch64"}
    if shutil.which("apt-get"):
        return "apt-get", f"_{'arm64' if arm else 'amd64'}.deb"
    if shutil.which("dnf"):
        return "dnf", f".{'aarch64' if arm else 'x86_64'}.rpm"
    raise HandyInstallError(
        "install Handy from https://handy.computer with handy on PATH; "
        "automatic Linux installation requires apt-get or dnf"
    )


def _download_linux_package(gh: str, suffix: str, directory: Path) -> Path:
    result = _run(
        [gh, "api", "--hostname", "github.com", "repos/cjpais/Handy/releases/latest"]
    )
    release = json.loads(result.stdout)
    if not isinstance(release, dict) or not isinstance(release.get("assets"), list):
        raise HandyInstallError("Handy release metadata has no assets")
    assets = [
        asset
        for asset in release["assets"]
        if isinstance(asset, dict)
        and isinstance(asset.get("name"), str)
        and asset["name"].startswith("Handy")
        and asset["name"].endswith(suffix)
    ]
    if len(assets) != 1:
        raise HandyInstallError(
            f"expected one upstream Handy package ending in {suffix}"
        )
    asset = assets[0]
    asset_id = asset.get("id")
    digest = asset.get("digest")
    if (
        type(asset_id) is not int
        or not isinstance(digest, str)
        or not digest.startswith("sha256:")
        or len(digest) != 71
        or any(character not in "0123456789abcdef" for character in digest[7:])
    ):
        raise HandyInstallError("Handy release asset lacks an ID or SHA-256 digest")
    destination = directory / ("handy.deb" if suffix.endswith(".deb") else "handy.rpm")
    with destination.open("wb") as output:
        subprocess.run(
            [
                gh,
                "api",
                "--hostname",
                "github.com",
                f"repos/cjpais/Handy/releases/assets/{asset_id}",
                "--header",
                "Accept: application/octet-stream",
            ],
            stdout=output,
            stderr=subprocess.PIPE,
            check=True,
            timeout=600,
        )
    with destination.open("rb") as contents:
        actual = hashlib.file_digest(contents, "sha256").hexdigest()
    if actual != digest[7:]:
        raise HandyInstallError("Handy download failed SHA-256 verification")
    return destination


def _install_linux() -> None:
    manager, suffix = _linux_package()
    gh = shutil.which("gh")
    if gh is None:
        raise HandyInstallError("install GitHub CLI (gh), then rerun desktop setup")
    prefix: list[str] = []
    if _run(["id", "-u"]).stdout.strip() != "0":
        sudo = shutil.which("sudo")
        if sudo is None:
            raise HandyInstallError("install sudo or install Handy as administrator")
        prefix = [sudo]
    with tempfile.TemporaryDirectory(prefix="tj-handy-") as temporary:
        package = _download_linux_package(gh, suffix, Path(temporary))
        _run([*prefix, manager, "install", "-y", str(package)])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Ensure desktop Handy is installed")
    parser.add_argument("--check", action="store_true")
    arguments = parser.parse_args(argv)
    system = platform.system()
    if os.environ.get("CI", "").lower() == "true":
        print("n/a        Handy (CI)")
        return 0
    if system == "Linux" and not (
        os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")
    ):
        print("n/a        Handy (no desktop session; run setup on the desktop host)")
        return 0
    if system not in {"Windows", "Linux"}:
        print(f"error      Handy setup does not support {system}", file=sys.stderr)
        return 1
    try:
        if installed := find_handy():
            print(f"ok         Handy ({installed})")
            return 0
        if arguments.check:
            print("missing    Handy (rerun setup without --check to install)")
            return 1
        if system == "Windows":
            _install_windows()
        else:
            _install_linux()
        installed = find_handy()
        if installed is None:
            raise HandyInstallError("installer finished but Handy could not be found")
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
        print(f"error      Handy: {error}", file=sys.stderr)
        for output in (error.stdout, error.stderr):
            if output:
                detail = (
                    output.decode("utf-8", errors="replace")
                    if isinstance(output, bytes)
                    else output
                )
                print(detail.rstrip(), file=sys.stderr)
        return 1
    except (
        HandyInstallError,
        OSError,
        subprocess.SubprocessError,
        json.JSONDecodeError,
    ) as error:
        print(f"error      Handy: {error}", file=sys.stderr)
        return 1
    print(f"installed  Handy ({installed})")
    print("setup      Open Handy once to download a model and configure hold-to-talk.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
