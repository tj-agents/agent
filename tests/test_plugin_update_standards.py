import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import jobs
import pytest
import required_plugins
import standards
import tj_plugins
from claude import installed as claude_installed
from codex import installed as codex_installed
from codex import launch as codex_launch
from fake_machine import recorded_failures
from launch_contract import Liveness
from launch_errors import LaunchError
from tj_agent.host import Host


@pytest.fixture(autouse=True)
def published(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    commands: list[list[str]] = []

    def run(command: list[str]) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(tj_plugins.Report, "run", staticmethod(run))
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
        report: tj_plugins.Report,
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

    def activate(report: tj_plugins.Report) -> None:
        assert "python:modules" in instructions.read_text(encoding="utf-8")
        assert "python:style" not in instructions.read_text(encoding="utf-8")
        events.append("activate")

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(jobs, "state_directory", lambda: tmp_path / "state")
    monkeypatch.setattr(tj_plugins, "already_current", lambda *args: False)
    adapter = claude_installed if host is Host.CLAUDE else codex_installed
    monkeypatch.setattr(adapter, "refresh", download)
    monkeypatch.setattr(adapter, "activate", activate)
    monkeypatch.setattr(required_plugins, "check", roots)
    assert tj_plugins.main(["update", "--host", host.value]) == int(failed)
    assert events == (
        ["download", "inventory"] if failed else ["download", "inventory", "activate"]
    )
    assert "Repository instructions" in instructions.read_text(encoding="utf-8")
    if failed:
        assert instructions.read_text(encoding="utf-8") == original
        failed = False
        monkeypatch.setattr(tj_plugins, "already_current", lambda *args: True)
        assert tj_plugins.main(["update", "--host", host.value]) == 0
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


def test_an_interrupted_update_is_activated_and_published_by_the_next_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, published: list[list[str]]
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(jobs, "state_directory", lambda: tmp_path / "state")
    monkeypatch.setattr(tj_plugins, "already_current", lambda *args: False)

    def interrupted(
        report: tj_plugins.Report,
        marketplaces: tuple[str, ...],
        current: Callable[[str, str], bool],
    ) -> None:
        raise RuntimeError("process interrupted after files converged")

    monkeypatch.setattr(claude_installed, "refresh", interrupted)
    with pytest.raises(RuntimeError, match="process interrupted"):
        tj_plugins.main(["update", "--host", "claude"])
    assert published == []
    events: list[str] = []
    monkeypatch.setattr(tj_plugins, "already_current", lambda *args: True)
    monkeypatch.setattr(required_plugins, "check", lambda *args: None)
    monkeypatch.setattr(
        claude_installed, "activate", lambda *args: events.append("activated")
    )
    assert tj_plugins.main(["update", "--host", "claude"]) == 0
    assert events == ["activated"]
    assert [command[:4] for command in published] == [
        ["tj", "update", "--host", "claude"]
    ]


def test_an_unreadable_codex_daemon_publishes_nothing_until_it_answers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, published: list[list[str]]
) -> None:
    state = tmp_path / "state"
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(jobs, "state_directory", lambda: state)
    monkeypatch.setattr(tj_plugins, "already_current", lambda *args: True)
    monkeypatch.setattr(required_plugins, "check", lambda *args: None)
    failed = True

    def liveness(*, strict: bool = False) -> dict[str, Liveness]:
        assert strict
        if failed:
            raise LaunchError("Cannot determine Codex liveness: connection failed")
        return {}

    monkeypatch.setattr(codex_launch, "liveness", liveness)
    assert tj_plugins.main(["update", "--host", "codex"]) == 1
    assert published == []
    assert any("connection failed" in failure for failure in recorded_failures(state))
    failed = False
    assert tj_plugins.main(["update", "--host", "codex"]) == 0
    assert [command[:4] for command in published] == [
        ["tj", "update", "--host", "codex"]
    ]


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
