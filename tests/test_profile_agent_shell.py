from pathlib import Path
from types import SimpleNamespace

import install
import pytest


@pytest.mark.parametrize(
    ("windows", "expected_shell"), [(True, "powershell.exe"), (False, "pwsh")]
)
def test_profile_delivery_selects_the_platform_shell(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    windows: bool,
    expected_shell: str,
) -> None:
    script = tmp_path / "install-codex-agents.ps1"
    monkeypatch.setattr(install, "codex_agents_installer", lambda context: script)
    context = SimpleNamespace(
        windows=windows,
        codex_home=tmp_path,
        which=lambda name: f"resolved-{name}",
    )

    command = install.codex_agents_command(context, "-Verify")

    assert command == [
        f"resolved-{expected_shell}",
        "-NoProfile",
        "-NonInteractive",
        "-File",
        str(script),
        "-CodexHome",
        str(tmp_path),
        "-Verify",
    ]
