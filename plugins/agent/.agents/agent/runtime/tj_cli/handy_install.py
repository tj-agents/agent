from __future__ import annotations

import argparse
import importlib
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Mapping, Sequence
from enum import Enum
from pathlib import Path

from tj_cli import tool_receipts
from tj_cli.errors import TjCliError
from tj_cli.native_install import default_bin_dir

VERSION = "0.10.0"
RECEIPT = ".tj-handy.json"


class HandyInstallError(TjCliError):
    pass


class LinuxPackageManager(Enum):
    APT = (
        "apt-get",
        {"amd64": "_amd64.deb", "arm64": "_arm64.deb"},
        ("dpkg-deb", "--field", "{package}", "Package"),
        ("dpkg-query", "--show", "--showformat=${Version}", "{name}"),
        ("dpkg-query", "--listfiles", "{name}"),
        (
            "--allow-downgrades",
            "--reinstall",
            "-o",
            "Dpkg::Options::=--force-confdef",
            "-o",
            "Dpkg::Options::=--force-confold",
        ),
        "install",
        "install",
    )
    DNF = (
        "dnf",
        {"amd64": ".x86_64.rpm", "arm64": ".aarch64.rpm"},
        ("rpm", "--query", "--package", "--queryformat", "%{NAME}", "{package}"),
        ("rpm", "--query", "--queryformat", "%{VERSION}", "{name}"),
        ("rpm", "--query", "--list", "{name}"),
        (),
        "reinstall",
        "downgrade",
    )

    def __init__(
        self,
        executable: str,
        suffixes: Mapping[str, str],
        package_name_command: Sequence[str],
        version_query: Sequence[str],
        list_files_command: Sequence[str],
        install_options: Sequence[str],
        reinstall_operation: str,
        downgrade_operation: str,
    ) -> None:
        self.executable = executable
        self.suffixes = suffixes
        self.package_name_command = package_name_command
        self.version_query = version_query
        self.list_files_command = list_files_command
        self.install_options = install_options
        self.reinstall_operation = reinstall_operation
        self.downgrade_operation = downgrade_operation

    def install_command(
        self,
        package: Path,
        current_version: tuple[int, ...] | None,
        expected_version: tuple[int, ...],
    ) -> list[str]:
        operation = "install"
        if current_version == expected_version:
            operation = self.reinstall_operation
        elif current_version is not None and current_version > expected_version:
            operation = self.downgrade_operation
        return [self.executable, operation, "-y", *self.install_options, str(package)]


def pending(bin_dir: Path) -> str | None:
    path = bin_dir / RECEIPT
    receipt = tool_receipts.read(path)
    expected_name = "handy.exe" if platform.system() == "Windows" else "handy"
    if (
        receipt is None
        or len(receipt.hashes) != 1
        or any(
            not Path(name).is_absolute() or Path(name).name != expected_name
            for name in receipt.hashes
        )
    ):
        return f"missing or invalid Handy receipt: {path}"
    return tool_receipts.pending(path, VERSION)


def _registered_windows_handy(*, version: str) -> Path | None:
    registry = importlib.import_module("winreg")
    key_name = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\Handy"
    for hive in (registry.HKEY_CURRENT_USER, registry.HKEY_LOCAL_MACHINE):
        for view in (registry.KEY_WOW64_64KEY, registry.KEY_WOW64_32KEY):
            try:
                with registry.OpenKey(
                    hive, key_name, 0, registry.KEY_READ | view
                ) as key:
                    location, _ = registry.QueryValueEx(key, "InstallLocation")
                    installed_version, _ = registry.QueryValueEx(key, "DisplayVersion")
                    if installed_version != version:
                        continue
            except FileNotFoundError:
                continue
            if isinstance(location, str) and location:
                executable = Path(location.strip('"')) / "handy.exe"
                if executable.is_file():
                    return executable
    return None


def _run(
    arguments: list[str], *, env: Mapping[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        arguments,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=600,
        env=env,
    )


WINDOWS_RUNTIMES = (
    ("KhronosGroup.VulkanRT", "vulkan-1.dll"),
    ("Microsoft.VCRedist.2015+.x64", "vcruntime140.dll"),
)


def _system_library_installed(system_directory: Path, library: str) -> bool:
    return (system_directory / library).is_file()


def _winget_install(winget: str, package: str, *options: str) -> None:
    _run(
        [
            winget,
            "install",
            "--id",
            package,
            "--exact",
            "--source",
            "winget",
            *options,
            "--silent",
            "--disable-interactivity",
            "--accept-source-agreements",
            "--accept-package-agreements",
            "--skip-dependencies",
        ]
    )


def _install_windows(system_directory: Path) -> Path:
    winget = shutil.which("winget")
    if winget is None:
        raise HandyInstallError(
            "install Windows App Installer (winget), then rerun setup"
        )
    for package, library in WINDOWS_RUNTIMES:
        if not _system_library_installed(system_directory, library):
            _winget_install(winget, package)
            if not _system_library_installed(system_directory, library):
                raise HandyInstallError(
                    f"installer finished but {package} runtime {library} is missing"
                )
    _winget_install(
        winget, "cjpais.Handy", "--version", VERSION, "--scope", "user", "--force"
    )
    installed = _registered_windows_handy(version=VERSION)
    if installed is None:
        raise HandyInstallError(
            f"installer finished but Handy {VERSION} could not be found in the registry"
        )
    return installed


def _linux_package() -> LinuxPackageManager:
    for manager in LinuxPackageManager:
        if shutil.which(manager.executable):
            return manager
    raise HandyInstallError("automatic Linux installation requires apt-get or dnf")


def _download_linux_package(gh: str, suffix: str, directory: Path) -> Path:
    result = _run(
        [
            gh,
            "api",
            "--hostname",
            "github.com",
            f"repos/cjpais/Handy/releases/tags/v{VERSION}",
        ]
    )
    release = json.loads(result.stdout)
    if not isinstance(release, dict) or not isinstance(release.get("assets"), list):
        raise HandyInstallError("Handy release metadata has no assets")
    if release.get("tag_name") != f"v{VERSION}" or release.get("draft") is not False:
        raise HandyInstallError(
            f"Handy release does not match pinned version {VERSION}"
        )
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
        or not tool_receipts.is_sha256(digest[7:])
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
    if tool_receipts.sha256(destination) != digest[7:]:
        raise HandyInstallError("Handy download failed SHA-256 verification")
    return destination


def _linux_version(manager: LinuxPackageManager, name: str) -> tuple[int, ...] | None:
    command = [part.replace("{name}", name) for part in manager.version_query]
    try:
        result = _run(command)
    except subprocess.CalledProcessError as error:
        if error.returncode != 1:
            raise
        return None
    parts = result.stdout.strip().split("-", 1)[0].split(".")
    if len(parts) != 3 or any(not part.isdecimal() for part in parts):
        raise HandyInstallError("package manager returned an invalid Handy version")
    return tuple(map(int, parts))


def _linux_executable(manager: LinuxPackageManager, name: str) -> Path:
    command = [part.replace("{name}", name) for part in manager.list_files_command]
    paths = [Path(line) for line in _run(command).stdout.splitlines()]
    binaries = [
        path
        for path in paths
        if path.is_absolute() and path.name == "handy" and path.is_file()
    ]
    if len(binaries) != 1:
        raise HandyInstallError("package manager did not identify one Handy executable")
    return binaries[0]


def _install_linux(*, no_escalate: bool, env: Mapping[str, str]) -> Path:
    manager = _linux_package()
    machine = platform.machine().lower()
    architecture = {"x86_64": "amd64", "aarch64": "arm64"}.get(machine, machine)
    if architecture not in manager.suffixes:
        raise HandyInstallError(f"unsupported Handy architecture: {machine}")
    gh = shutil.which("gh")
    if gh is None:
        raise HandyInstallError("install GitHub CLI (gh), then rerun desktop setup")
    prefix: list[str] = []
    if _run(["id", "-u"]).stdout.strip() != "0":
        if no_escalate:
            raise HandyInstallError(
                f"Handy {VERSION} needs administrator installation; rerun desktop setup"
            )
        sudo = shutil.which("sudo")
        if sudo is None:
            raise HandyInstallError("install sudo or install Handy as administrator")
        prefix = [sudo]
    with tempfile.TemporaryDirectory(prefix="tj-handy-") as temporary:
        package = _download_linux_package(
            gh, manager.suffixes[architecture], Path(temporary)
        )
        command = [
            part.replace("{package}", str(package))
            for part in manager.package_name_command
        ]
        name = _run(command).stdout.strip()
        if not name or name.startswith("-") or any(c.isspace() for c in name):
            raise HandyInstallError("downloaded package has an invalid package name")
        expected_version = tuple(map(int, VERSION.split(".")))
        current_version = _linux_version(manager, name)
        _run(
            [
                *prefix,
                *manager.install_command(package, current_version, expected_version),
            ],
            env=env,
        )
        if _linux_version(manager, name) != expected_version:
            raise HandyInstallError(f"package manager did not install Handy {VERSION}")
        return _linux_executable(manager, name)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Ensure desktop Handy is installed")
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--bin-dir", type=Path)
    parser.add_argument("--no-escalate", action="store_true")
    arguments = parser.parse_args(argv)
    system = platform.system()
    reason = None
    if os.environ.get("CI", "").lower() == "true":
        reason = "CI"
    elif system == "Linux" and not (
        os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")
    ):
        reason = "no desktop session; run setup on the desktop host"
    if reason is not None:
        print(f"n/a        Handy ({reason})")
        return 0
    if system not in {"Windows", "Linux"}:
        print(f"error      Handy setup does not support {system}", file=sys.stderr)
        return 1
    arguments.bin_dir = arguments.bin_dir or default_bin_dir()
    try:
        if pending(arguments.bin_dir) is None:
            print(f"ok         Handy {VERSION}")
            return 0
        if arguments.check:
            print("missing    Handy (rerun setup without --check to install)")
            return 1
        if system == "Windows":
            if arguments.no_escalate:
                raise HandyInstallError(
                    f"Handy {VERSION} requires interactive setup; "
                    "rerun without --no-escalate"
                )
            system_directory = (
                Path(os.environ.get("SYSTEMROOT", r"C:\Windows")) / "System32"
            )
            installed = _install_windows(system_directory)
        else:
            installed = _install_linux(
                no_escalate=arguments.no_escalate,
                env=os.environ | {"DEBIAN_FRONTEND": "noninteractive"},
            )
        tool_receipts.write(
            arguments.bin_dir / RECEIPT,
            tool_receipts.Receipt(
                VERSION, {str(installed.resolve()): tool_receipts.sha256(installed)}
            ),
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
        if arguments.no_escalate:
            print(f"error      Handy: {' '.join(str(error).split())}", file=sys.stderr)
            return 1
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
