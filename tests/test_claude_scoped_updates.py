import json
import subprocess
import threading
from collections.abc import Mapping
from pathlib import Path

import pytest
import tj_plugins
from fake_machine import environment, pin_plugin, update_claude_plugin, write_json
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
def test_current_repository_plugins_install_nothing_and_publish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scope: str
) -> None:
    home = tmp_path / "claude"
    pin_plugin(home, "python", "a" * 40)
    install_scopes(home, tmp_path, {scope: "a" * 40})
    for name, value in environment(tmp_path).items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)
    monkeypatch.chdir(tmp_path)

    commands: list[list[str]] = []

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(process, "run", run)
    assert tj_plugins.main(["update", "--host", "claude"]) == 0
    assert [command[:2] for command in commands] == [["tj", "update"]]


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
    registry = home / "plugins/installed_plugins.json"
    document = json.loads(registry.read_text(encoding="utf-8"))
    document["plugins"]["rust@tj-agents"] = [
        {"scope": "project", "projectPath": str(tmp_path / "other")}
    ]
    write_json(registry, document)
    commands: list[list[str]] = []

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        if command[:3] == ["claude", "plugin", "update"]:
            update_claude_plugin(home, command)
        return subprocess.CompletedProcess(command, 0, "{}", "")

    monkeypatch.setattr(process, "run", run)
    assert tj_plugins.main(["update", "--host", "claude"]) == 0
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


def stale_plugins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "claude"
    for plugin in ("python", "rust"):
        pin_plugin(home, plugin, "b" * 40)
    registry = home / "plugins/installed_plugins.json"
    write_json(
        registry,
        {
            "plugins": {
                f"{plugin}@tj-agents": [{"scope": "user", "gitCommitSha": "a" * 40}]
                for plugin in ("python", "rust")
            }
        },
    )
    for name, value in environment(tmp_path).items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)
    monkeypatch.chdir(tmp_path)
    return home


def test_distinct_plugins_update_concurrently(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = stale_plugins(tmp_path, monkeypatch)
    together = threading.Barrier(2, timeout=10)

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if command[:3] == ["claude", "plugin", "update"]:
            together.wait()
            update_claude_plugin(home, command)
        return subprocess.CompletedProcess(command, 0, "{}", "")

    monkeypatch.setattr(process, "run", run)
    assert tj_plugins.main(["update", "--host", "claude"]) == 0


def test_an_update_that_misses_its_pin_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    home = stale_plugins(tmp_path, monkeypatch)

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if command[:3] == ["claude", "plugin", "update"] and "rust" not in command[3]:
            update_claude_plugin(home, command)
        return subprocess.CompletedProcess(command, 0, "{}", "")

    monkeypatch.setattr(process, "run", run)
    assert tj_plugins.main(["update", "--host", "claude"]) == 1
    error = capsys.readouterr().err
    assert "rust@tj-agents: user did not reach its marketplace pin" in error
    assert "python@tj-agents" not in error
