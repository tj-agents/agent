import json
import subprocess
from collections.abc import Mapping
from pathlib import Path

import pytest
import tj_plugins
from claude import installed as claude_installed
from fake_machine import environment, pin_plugin, write_json
from tj_cli import process


def install_scopes(home: Path, project: Path, versions: Mapping[str, str]) -> None:
    rows = []
    for scope, revision in versions.items():
        root = home / "plugins/cache/tj-agents/python" / revision[:12]
        root.mkdir(parents=True, exist_ok=True)
        rows.append(
            {
                "scope": scope,
                "projectPath": str(project),
                "gitCommitSha": revision,
                "installPath": str(root),
            }
        )
    write_json(
        home / "plugins/installed_plugins.json", {"plugins": {"python@tj-agents": rows}}
    )


@pytest.mark.parametrize("scope", ["project", "local"])
def test_current_repository_plugins_need_no_native_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scope: str
) -> None:
    home = tmp_path / "claude"
    pin_plugin(home, "python", "a" * 40)
    install_scopes(home, tmp_path, {scope: "a" * 40})
    for name, value in environment(tmp_path).items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)
    monkeypatch.chdir(tmp_path)

    def unexpected(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        raise AssertionError("A current repository must not invoke a native CLI")

    monkeypatch.setattr(process, "run", unexpected)
    assert tj_plugins.main(["update", "--host", "claude"]) == 0


@pytest.mark.parametrize("stale_scope", ["user", "project", "local"])
def test_update_changes_only_the_stale_applicable_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stale_scope: str
) -> None:
    home = tmp_path / "claude"
    pin_plugin(home, "python", "b" * 40)
    install_scopes(
        home,
        tmp_path,
        {
            scope: ("a" if scope == stale_scope else "b") * 40
            for scope in ("user", "project", "local")
        },
    )
    for name, value in environment(tmp_path).items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)
    monkeypatch.chdir(tmp_path)
    rows = [
        {"id": "python@tj-agents", "scope": scope, "projectPath": str(tmp_path)}
        for scope in ("user", "project", "local")
    ]
    rows.append(
        {
            "id": "rust@tj-agents",
            "scope": "project",
            "projectPath": str(tmp_path / "other"),
        }
    )
    commands: list[list[str]] = []

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        output = (
            json.dumps(rows) if command[:3] == ["claude", "plugin", "list"] else "{}"
        )
        return subprocess.CompletedProcess(command, 0, output, "")

    monkeypatch.setattr(process, "run", run)
    assert tj_plugins.main(["update", "--host", "claude"]) == 0
    assert claude_installed.behind(("tj-agents",))
    updates = [
        command for command in commands if command[:3] == ["claude", "plugin", "update"]
    ]
    assert updates == [
        [
            "claude",
            "plugin",
            "update",
            "python@tj-agents",
            "--scope",
            stale_scope,
            "--json",
            "-y",
        ]
    ]
