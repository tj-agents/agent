import json
import subprocess
import tomllib
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from codex import installed, projects
from fake_machine import environment
from tj_agent.host import Host


@pytest.fixture
def codex_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for key, value in environment(tmp_path).items():
        monkeypatch.setenv(key, value)
    home = tmp_path / "codex"
    home.mkdir()
    return home


def test_an_absent_codex_cache_is_an_empty_inventory(codex_home: Path) -> None:
    assert installed.identities() == set()


def test_a_missing_codex_configuration_has_no_projects(codex_home: Path) -> None:
    assert projects.user_config() == {}


def test_invalid_codex_configuration_is_reported_to_the_caller(
    codex_home: Path,
) -> None:
    (codex_home / "config.toml").write_text("invalid = [", encoding="utf-8")
    with pytest.raises(tomllib.TOMLDecodeError, match="Invalid value"):
        projects.user_config()


def test_an_unreadable_codex_configuration_is_reported_to_the_caller(
    codex_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = codex_home / "config.toml"
    read_text = Path.read_text

    def denied(
        path: Path, encoding: str | None = None, errors: str | None = None
    ) -> str:
        if path == config:
            raise PermissionError(13, "Access is denied", str(path))
        return read_text(path, encoding=encoding, errors=errors)

    monkeypatch.setattr(Path, "read_text", denied)
    with pytest.raises(PermissionError, match="Access is denied"):
        projects.user_config()


@dataclass(slots=True)
class MarketplaceReport:
    roots_output: str
    failures: list[str] = field(default_factory=list)
    commands: list[list[str]] = field(default_factory=list)

    def step(
        self, host: Host, label: str, command: Sequence[str]
    ) -> subprocess.CompletedProcess[str]:
        self.commands.append(list(command))
        output = (
            self.roots_output if label == "marketplace roots" else '{"installed": []}'
        )
        return subprocess.CompletedProcess(command, 0, output, "")

    def fail(self, host: Host, label: str, detail: str) -> None:
        self.failures.append(f"{host}: {label}: {detail}")


@pytest.mark.parametrize("manifest", [None, "invalid json"])
def test_unknown_publication_is_not_reported_as_a_retired_plugin(
    codex_home: Path, manifest: str | None, capsys: pytest.CaptureFixture[str]
) -> None:
    build = codex_home / "plugins/cache/tj-agents/agent/build/.codex-plugin"
    build.mkdir(parents=True)
    (build / "plugin.json").write_text('{"name": "agent"}', encoding="utf-8")
    root = codex_home / "marketplace"
    if manifest is not None:
        path = root / ".agents/plugins/marketplace.json"
        path.parent.mkdir(parents=True)
        path.write_text(manifest, encoding="utf-8")
    report = MarketplaceReport(
        json.dumps(
            {
                "marketplaces": [{"name": "tj-agents", "root": str(root)}],
            }
        )
    )
    installed.refresh(report, ["tj-agents"], lambda *_: False)
    assert len(report.failures) == 1
    assert "unreadable manifest" in report.failures[0]
    assert "no longer published" not in capsys.readouterr().out
    assert all(command[:3] != ["codex", "plugin", "add"] for command in report.commands)


def test_failed_marketplace_discovery_is_not_an_empty_publication_list() -> None:
    report = MarketplaceReport("invalid json")
    with pytest.raises(installed.InventoryError, match="marketplace roots"):
        installed.published_plugins(report)
    assert len(report.failures) == 1


def test_valid_marketplaces_still_identify_published_plugins(tmp_path: Path) -> None:
    path = tmp_path / ".agents/plugins/marketplace.json"
    path.parent.mkdir(parents=True)
    path.write_text('{"plugins": [{"name": "agent"}]}', encoding="utf-8")
    report = MarketplaceReport(
        json.dumps(
            {
                "marketplaces": [{"name": "tj-agents", "root": str(tmp_path)}],
            }
        )
    )
    assert installed.published_plugins(report) == {"agent@tj-agents"}
    assert report.failures == []
