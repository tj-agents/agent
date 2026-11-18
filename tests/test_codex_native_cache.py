from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest import mock

import pytest
from codex import catalog
from codex import installed as codex_installed
from tj_cli import process


def selection(project: Path, identities: list[str]) -> None:
    path = project / ".agents" / "plugins.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "marketplaces": {"tj-agents": {"repository": "tj-agents/marketplace"}},
                "plugins": identities,
            }
        ),
        encoding="utf-8",
    )


def native_entry(
    version: str, source: str = "https://github.com/tj-agents/marketplace.git"
) -> dict[str, object]:
    return {
        "pluginId": "agent@tj-agents",
        "name": "agent",
        "marketplaceName": "tj-agents",
        "version": version,
        "source": {
            "source": "git-subdir",
            "url": "https://github.com/tj-agents/agent.git",
            "path": "plugins/agent",
            "sha": "a" * 40,
        },
        "marketplaceSource": {"sourceType": "git", "source": source},
    }


def test_acquire_cache_uses_an_isolated_native_home_and_preserves_global_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "work"
    project.mkdir()
    (project / ".git").mkdir()
    selection(project, ["agent@tj-agents"])
    real_home = tmp_path / "real-codex"
    config = real_home / "config.toml"
    original = 'model = "kept"\n[plugins."unrelated@other"]\nenabled = true\n'
    config.parent.mkdir()
    config.write_text(original, encoding="utf-8")
    monkeypatch.setenv("CODEX_HOME", str(real_home))
    monkeypatch.setenv("AGENT_STATE_DIRECTORY", str(tmp_path / "state"))
    calls: list[tuple[list[str], dict[str, object]]] = []

    def native(
        command: list[str], **options: object
    ) -> subprocess.CompletedProcess[str]:
        calls.append((command, options))
        environment = options["env"]
        assert isinstance(environment, dict)
        temporary_home = Path(environment["CODEX_HOME"])
        assert temporary_home != real_home
        if command[:3] == ["codex", "plugin", "add"]:
            payload = temporary_home / "plugins/cache/tj-agents/agent/build"
            (payload / ".codex-plugin").mkdir(parents=True)
            (payload / ".codex-plugin/plugin.json").write_text(
                '{"name":"agent","version":"build"}', encoding="utf-8"
            )
            return subprocess.CompletedProcess(command, 0, "", "")
        if command[:3] == ["codex", "plugin", "list"]:
            return subprocess.CompletedProcess(
                command, 0, json.dumps({"installed": [native_entry("build")]}), ""
            )
        return subprocess.CompletedProcess(command, 0, "", "")

    with mock.patch.object(process, "run", side_effect=native):
        rows = codex_installed.acquire_cache(project, ("agent@tj-agents",))

    assert rows == [native_entry("build")]
    assert config.read_text(encoding="utf-8") == original
    assert (
        real_home / "plugins/cache/tj-agents/agent/build/.codex-plugin/plugin.json"
    ).is_file()
    catalog.remember(rows, verified_install=True)
    assert catalog.current("tj-agents", "agent", catalog.Digests(tmp_path / "state"))
    monkeypatch.chdir(project)
    report = mock.Mock()
    monkeypatch.setattr(
        codex_installed.installer_boundary,
        "acquire_codex_cache",
        lambda *_: pytest.fail("current selected cache must not reacquire"),
    )
    codex_installed.refresh(
        report,
        ("tj-agents",),
        lambda marketplace, plugin: catalog.current(
            marketplace, plugin, catalog.Digests(tmp_path / "state")
        ),
    )
    report.fail.assert_not_called()
    commands = [command for command, _options in calls]
    assert commands[0][:6] == [
        "codex",
        "plugin",
        "marketplace",
        "add",
        "tj-agents/marketplace",
        "--ref",
    ]
    assert commands[1] == ["codex", "plugin", "add", "agent@tj-agents", "--json"]
    assert commands[2] == ["codex", "plugin", "list", "--json"]


def test_acquire_cache_rejects_a_native_payload_from_another_publisher(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "work"
    project.mkdir()
    (project / ".git").mkdir()
    selection(project, ["agent@tj-agents"])
    home = tmp_path / "real-codex"
    monkeypatch.setenv("CODEX_HOME", str(home))

    def native(
        command: list[str], **options: object
    ) -> subprocess.CompletedProcess[str]:
        environment = options["env"]
        assert isinstance(environment, dict)
        temporary_home = Path(environment["CODEX_HOME"])
        if command[:3] == ["codex", "plugin", "add"]:
            payload = temporary_home / "plugins/cache/tj-agents/agent/build"
            (payload / ".codex-plugin").mkdir(parents=True)
            (payload / ".codex-plugin/plugin.json").write_text(
                '{"name":"agent","version":"build"}', encoding="utf-8"
            )
        if command[:3] == ["codex", "plugin", "list"]:
            return subprocess.CompletedProcess(
                command,
                0,
                json.dumps(
                    {
                        "installed": [
                            native_entry(
                                "build", "https://github.com/other/marketplace.git"
                            )
                        ]
                    }
                ),
                "",
            )
        return subprocess.CompletedProcess(command, 0, "", "")

    with (
        mock.patch.object(process, "run", side_effect=native),
        pytest.raises(ValueError, match="publisher differs"),
    ):
        codex_installed.acquire_cache(project, ("agent@tj-agents",))
    assert not (home / "plugins/cache").exists()


def test_refresh_acquires_only_the_calling_repository_selection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "work"
    project.mkdir()
    (project / ".git").mkdir()
    selection(project, ["agent@tj-agents"])
    monkeypatch.chdir(project)
    acquired: list[tuple[Path, tuple[str, ...]]] = []
    remembered: list[tuple[list[dict[str, object]], bool]] = []

    def acquire(root: Path, identities: tuple[str, ...]) -> list[dict[str, object]]:
        acquired.append((root, identities))
        return [native_entry("build")]

    report = mock.Mock()
    monkeypatch.setattr(
        codex_installed.installer_boundary, "acquire_codex_cache", acquire
    )
    monkeypatch.setattr(
        codex_installed.catalog,
        "remember",
        lambda rows, *, verified_install: remembered.append((rows, verified_install)),
    )
    codex_installed.refresh(report, ("tj-agents", "agents"), lambda *_: False)

    assert acquired == [(project, ("agent@tj-agents",))]
    assert remembered == [([native_entry("build")], True)]
    report.fail.assert_not_called()
