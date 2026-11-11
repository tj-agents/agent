import io
import json
import subprocess
from pathlib import Path

import install
import pytest


def powershell(directory: Path) -> Path:
    directory.mkdir(parents=True)
    executable = directory / "pwsh.exe"
    executable.touch()
    return executable


def test_profile_delivery_uses_persisted_path_when_process_path_is_stale(
    tmp_path: Path,
) -> None:
    alias = powershell(tmp_path / "Microsoft" / "WindowsApps")
    executable = powershell(tmp_path / "local app data ü" / "portable shell")
    context = install.Context(
        home=tmp_path,
        environ={
            "PATH": str(alias.parent),
            "LOCALAPPDATA": str(executable.parent.parent),
        },
        windows=True,
        which=lambda _name: str(alias),
        user_environment={"Path": '"%localappdata%/portable shell"'},
    )
    codex = tmp_path / ".codex"
    codex.mkdir()
    package = tmp_path / "engineering"
    installer = package / "scripts" / "install-codex-agents.ps1"
    installer.parent.mkdir(parents=True)
    installer.touch()
    claude = tmp_path / ".claude"
    (claude / "plugins").mkdir(parents=True)
    (claude / "settings.json").write_text(
        json.dumps({"enabledPlugins": {"engineering@tj-agents": True}}),
        encoding="utf-8",
    )
    (claude / "plugins" / "installed_plugins.json").write_text(
        json.dumps(
            {"plugins": {"engineering@tj-agents": [{"installPath": str(package)}]}}
        ),
        encoding="utf-8",
    )
    calls: list[list[str]] = []

    def run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        return subprocess.CompletedProcess(
            command, 0, "VERIFIED: 12 profile agents\n", ""
        )

    context.run = run
    before = {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    assert install.skip_codex_agents(context) is None
    assert install.check_codex_agents(context) == []
    assert install.apply_codex_agents(context) == ["VERIFIED: 12 profile agents"]
    prefix = [
        str(executable),
        "-NoProfile",
        "-NonInteractive",
        "-File",
        str(installer),
        "-CodexHome",
        str(codex),
    ]
    assert calls == [[*prefix, "-Verify"], [*prefix, "-Apply"]]
    assert context.environ["PATH"] == str(alias.parent)
    assert context.user_environment == {"Path": '"%localappdata%/portable shell"'}
    assert before == {
        path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()
    }


def test_profile_delivery_preserves_current_unpacked_powershell(tmp_path: Path) -> None:
    current = powershell(tmp_path / "current")
    persisted = powershell(tmp_path / "persisted")
    context = install.Context(
        home=tmp_path,
        environ={"PATH": str(current.parent)},
        windows=True,
        which=lambda _name: str(current),
        user_environment={"Path": str(persisted.parent)},
    )
    assert install.codex_agents_powershell(context) == str(current)


def test_profile_delivery_searches_beyond_packaged_process_path(tmp_path: Path) -> None:
    alias = powershell(tmp_path / "Microsoft" / "WindowsApps")
    current = powershell(tmp_path / "current")
    persisted = powershell(tmp_path / "persisted")
    context = install.Context(
        home=tmp_path,
        environ={"PATH": f"{alias.parent};{current.parent}"},
        windows=True,
        which=lambda _name: str(alias),
        user_environment={"Path": str(persisted.parent)},
    )
    assert install.codex_agents_powershell(context) == str(current)


@pytest.mark.parametrize("persisted", [None, "", 12, ";relative;\0bad;%UNKNOWN%"])
@pytest.mark.parametrize("package", ["WindowsApps", "wINDOWSaPPS/PowerShell_7"])
def test_profile_delivery_reports_packaged_only_powershell_without_running(
    tmp_path: Path, persisted: object, package: str
) -> None:
    packaged = powershell(tmp_path / package)
    calls: list[list[str]] = []

    def run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, "", "")

    context = install.Context(
        home=tmp_path,
        environ={"PATH": str(packaged.parent)},
        windows=True,
        which=lambda _name: str(packaged),
        user_environment={"Path": persisted},
        run=run,
    )
    before = {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    assert install.codex_agents_powershell(context) is None
    selected = next(
        step for step in install.STEPS if step.name == "Codex profile agents"
    )
    output = io.StringIO()
    assert install.run_steps([selected], context, False, output) == 0
    assert "unpackaged PowerShell 7 is required" in output.getvalue()
    assert calls == []
    assert before == {
        path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()
    }


@pytest.mark.parametrize("executable", [None, "/usr/local/bin/pwsh"])
def test_profile_delivery_keeps_posix_path_resolution(
    tmp_path: Path, executable: str | None
) -> None:
    context = install.Context(
        home=tmp_path,
        environ={},
        windows=False,
        which=lambda _name: executable,
    )
    assert install.codex_agents_powershell(context) == executable


@pytest.mark.parametrize("windows", [False, True])
def test_profile_delivery_refuses_a_command_without_powershell(
    tmp_path: Path, windows: bool
) -> None:
    context = install.Context(
        home=tmp_path, environ={}, windows=windows, which=lambda _name: None
    )
    with pytest.raises(install.InstallError, match="PowerShell 7 is unavailable"):
        install.codex_agents_command(context, "-Verify")
