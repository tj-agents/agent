import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import jobs
import plugin_update
import pytest
import required_plugins
import standards
from claude import installed as claude_installed
from codex import installed as codex_installed
from codex import launch as codex_launch
from launch_contract import Liveness
from launch_errors import LaunchError
from tj_agent.host import Host
from tree_digest import Digests

HOSTS = {host.value: host for host in Host}


def update_plugins(host: str) -> int:
    outcome = plugin_update.update(HOSTS[host], Digests(jobs.state_directory()))
    return 1 if outcome.failures else 0


@pytest.fixture(autouse=True)
def report_commands(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """Every native command Report.run dispatches while plugin_update.update() runs."""
    commands: list[list[str]] = []

    def run(command: list[str]) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(plugin_update.Report, "run", staticmethod(run))
    for adapter in (claude_installed, codex_installed):
        monkeypatch.setattr(adapter, "update_marketplaces", lambda *_arguments: None)
    monkeypatch.setattr(codex_installed, "trust_hooks", lambda _report: True)
    return commands


@pytest.mark.parametrize("failed", [False, True])
@pytest.mark.parametrize("host", list(Host))
def test_plugin_updates_rebuild_standards_before_activation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failed: bool, host: Host
) -> None:
    selection = tmp_path / ".agents/plugins.json"
    selection.parent.mkdir()
    selection.write_text(
        '{"schema_version": 1, "plugins": ["python@tj-agents"]}', encoding="utf-8"
    )
    instructions = tmp_path / "AGENTS.md"
    original = (
        "<!-- BEGIN tj-agents:standards -->\nPython: `python:style`\n"
        "<!-- END tj-agents:standards -->\n\nRepository instructions\n"
    )
    instructions.write_text(original, encoding="utf-8")
    installed = tmp_path / "installed"
    standard = installed / ".agents/python/convention/modules/SKILL.md"
    standard.parent.mkdir(parents=True)
    standard.write_text(
        "---\nname: modules\nkind: convention\nprofile: core\n"
        "applicability: Python projects\n---\nFull modules standard\n",
        encoding="utf-8",
    )
    events: list[str] = []

    def download(
        report: plugin_update.Report,
        marketplaces: tuple[str, ...],
        current: Callable[[str, str], bool],
    ) -> None:
        events.append("download")

    def roots(directory: Path, selected: Host) -> dict[str, Path]:
        assert directory == tmp_path and selected is host
        events.append("inventory")
        if failed:
            raise required_plugins.RequiredPluginsError("native inventory failed")
        return {"python@tj-agents": installed}

    def activate(report: plugin_update.Report) -> None:
        assert "python:modules" in instructions.read_text(encoding="utf-8")
        assert "python:style" not in instructions.read_text(encoding="utf-8")
        events.append("activate")

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(jobs, "state_directory", lambda: tmp_path / "state")
    monkeypatch.setattr(plugin_update, "already_current", lambda *args: False)
    adapter = claude_installed if host is Host.CLAUDE else codex_installed
    monkeypatch.setattr(adapter, "refresh", download)
    monkeypatch.setattr(adapter, "activate", activate)
    monkeypatch.setattr(required_plugins, "check", roots)
    assert update_plugins(host.value) == int(failed)
    assert events == (
        ["download", "inventory"] if failed else ["download", "inventory", "activate"]
    )
    assert "Repository instructions" in instructions.read_text(encoding="utf-8")
    if failed:
        assert instructions.read_text(encoding="utf-8") == original
        failed = False
        monkeypatch.setattr(plugin_update, "already_current", lambda *args: True)
        assert update_plugins(host.value) == 0
        assert events == ["download", "inventory", "inventory", "activate"]


def test_loaded_repository_standards_require_verified_installed_builds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    selection = tmp_path / ".agents/plugins.json"
    selection.parent.mkdir()
    selection.write_text(
        '{"schema_version": 1, "plugins": ["python@tj-agents"]}', encoding="utf-8"
    )
    instructions = tmp_path / "AGENTS.md"
    instructions.write_text("Existing instructions\n", encoding="utf-8")
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    monkeypatch.setattr(jobs, "state_directory", lambda: tmp_path / "state")
    with pytest.raises(
        standards.StandardsError, match="Cannot verify updated standards"
    ):
        codex_installed.update_project_standards(tmp_path)
    assert instructions.read_text(encoding="utf-8") == "Existing instructions\n"


def test_an_interrupted_update_still_activates_on_the_next_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, report_commands: list[list[str]]
) -> None:
    state = tmp_path / "state"
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(plugin_update, "already_current", lambda *args: False)

    def interrupted(
        report: plugin_update.Report,
        marketplaces: tuple[str, ...],
        current: Callable[[str, str], bool],
    ) -> None:
        raise RuntimeError("process interrupted after files converged")

    monkeypatch.setattr(claude_installed, "refresh", interrupted)
    with pytest.raises(RuntimeError, match="process interrupted"):
        plugin_update.update(Host.CLAUDE, Digests(state))
    assert report_commands == []
    events: list[str] = []
    monkeypatch.setattr(plugin_update, "already_current", lambda *args: True)
    monkeypatch.setattr(required_plugins, "check", lambda *args: None)
    monkeypatch.setattr(
        claude_installed, "activate", lambda *args: events.append("activated")
    )
    outcome = plugin_update.update(Host.CLAUDE, Digests(state))
    assert not outcome.failures
    assert events == ["activated"]
    assert report_commands == []


def test_current_plugins_activate_once_per_marketplace_revision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(plugin_update, "already_current", lambda *args: True)
    monkeypatch.setattr(required_plugins, "check", lambda *args: None)
    events: list[str] = []
    monkeypatch.setattr(
        codex_installed, "activate", lambda *args: events.append("activated")
    )
    for revision in ("a" * 40, "a" * 40, "b" * 40):
        monkeypatch.setattr(plugin_update, "marketplace_revision", lambda *_: revision)
        assert not plugin_update.update(Host.CODEX, Digests(tmp_path / "state")).failures
    assert events == ["activated", "activated"]


def test_a_liveness_check_failure_is_reported_until_it_answers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, report_commands: list[list[str]]
) -> None:
    state = tmp_path / "state"
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(plugin_update, "already_current", lambda *args: True)
    monkeypatch.setattr(required_plugins, "check", lambda *args: None)
    failed = True

    def liveness(*, strict: bool = False) -> dict[str, Liveness]:
        assert strict
        if failed:
            raise LaunchError("Cannot determine Codex liveness: connection failed")
        return {}

    monkeypatch.setattr(codex_launch, "liveness", liveness)
    outcome = plugin_update.update(Host.CODEX, Digests(state))
    assert any("connection failed" in failure for failure in outcome.failures)
    assert report_commands == []
    failed = False
    outcome = plugin_update.update(Host.CODEX, Digests(state))
    assert not outcome.failures
    assert report_commands == []


@pytest.mark.parametrize("broken", [False, True])
def test_loaded_thread_pages_preserve_projects_and_reject_cursor_cycles(
    tmp_path: Path, broken: bool
) -> None:
    @dataclass(frozen=True, slots=True)
    class Server:
        directory: Path
        broken: bool

        def request(self, method: str, params: dict[str, object]) -> dict[str, object]:
            if method == "thread/read":
                return {
                    "thread": {"cwd": str(self.directory / str(params["threadId"]))}
                }
            if params["cursor"] is None:
                return {"data": ["first"], "nextCursor": "second-page"}
            return {
                "data": ["second"],
                "nextCursor": "second-page" if self.broken else None,
            }

    if broken:
        with pytest.raises(RuntimeError, match="invalid loaded-thread cursor"):
            codex_installed.loaded_directories(Server(tmp_path, broken))
    else:
        assert codex_installed.loaded_directories(Server(tmp_path, broken)) == {
            tmp_path / "first",
            tmp_path / "second",
        }
