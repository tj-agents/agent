import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import jobs
import latest_plugins
import plugin_activation
import pytest
import required_plugins
import standards
import tj_plugins
from claude import installed as claude_installed
from codex import installed as codex_installed
from tj_agent.host import Host


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

    def activate(report: tj_plugins.Report, managed: bool) -> None:
        assert "python:modules" in instructions.read_text(encoding="utf-8")
        assert "python:style" not in instructions.read_text(encoding="utf-8")
        events.append("activate")

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(jobs, "state_directory", lambda: tmp_path / "state")
    monkeypatch.setattr(tj_plugins, "already_current", lambda *args: False)
    adapter = claude_installed if host is Host.CLAUDE else codex_installed
    monkeypatch.setattr(adapter, "refresh", download)
    monkeypatch.setattr(adapter, "restart", activate)
    monkeypatch.setattr(required_plugins, "check", roots)
    assert tj_plugins.main(["update", "--host", host.value]) == int(failed)
    assert events == (
        ["download", "inventory"] if failed else ["download", "inventory", "activate"]
    )
    assert "Repository instructions" in instructions.read_text(encoding="utf-8")
    if failed:
        assert instructions.read_text(encoding="utf-8") == original
        assert plugin_activation.pending(tmp_path / "state", host, tmp_path)
        failed = False
        monkeypatch.setattr(tj_plugins, "already_current", lambda *args: True)
        monkeypatch.setattr(codex_installed, "repair_hooks", lambda _report: None)
        assert tj_plugins.main(["update", "--host", host.value]) == 0
        assert events == ["download", "inventory", "inventory", "activate"]
    assert not plugin_activation.pending(tmp_path / "state", host, tmp_path)


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


def test_interrupted_update_retains_activation_for_a_fresh_invocation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = tmp_path / "state"
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(jobs, "state_directory", lambda: state)
    monkeypatch.setattr(tj_plugins, "already_current", lambda *args: False)

    def interrupted(
        report: tj_plugins.Report,
        marketplaces: tuple[str, ...],
        current: Callable[[str, str], bool],
    ) -> None:
        assert plugin_activation.pending(state, Host.CLAUDE, tmp_path)
        raise RuntimeError("process interrupted after files converged")

    monkeypatch.setattr(claude_installed, "refresh", interrupted)
    with pytest.raises(RuntimeError, match="process interrupted"):
        tj_plugins.main(["update", "--host", "claude"])
    events: list[str] = []
    monkeypatch.setattr(tj_plugins, "already_current", lambda *args: True)
    monkeypatch.setattr(required_plugins, "check", lambda *args: None)
    monkeypatch.setattr(
        claude_installed, "restart", lambda *args: events.append("activated")
    )
    assert tj_plugins.main(["update", "--host", "claude"]) == 0
    assert events == ["activated"]
    assert not plugin_activation.pending(state, Host.CLAUDE, tmp_path)


@pytest.mark.parametrize("host", list(Host))
def test_pending_activation_survives_other_updates_and_failure_cooldown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, host: Host
) -> None:
    state = tmp_path / "state"
    origin = tmp_path / "origin"
    origin.mkdir()
    other = tmp_path / "other"
    other.mkdir()
    plugin_activation.begin(state, host, origin)
    jobs.record(state, jobs.Job.UPDATE, [f"{host}: repository standards: failed"])
    monkeypatch.setattr(time, "time", lambda: 10**12)
    monkeypatch.setattr(jobs, "state_directory", lambda: state)
    monkeypatch.setattr(tj_plugins, "already_current", lambda *args: True)
    monkeypatch.setattr(codex_installed, "repair_hooks", lambda _report: None)
    monkeypatch.setattr(latest_plugins, "running_behind", lambda _host: None)
    monkeypatch.chdir(other)
    for other_host in Host:
        assert tj_plugins.main(["update", "--host", other_host.value]) == 0
    assert plugin_activation.pending(state, host, origin)
    monkeypatch.chdir(origin)
    monkeypatch.setattr(latest_plugins, "repair_machine", lambda *args, **kwargs: None)
    monkeypatch.setattr(latest_plugins, "installed_behind", lambda _host: [])
    commands: list[list[str]] = []

    def start(directory: Path, job: jobs.Job, command: list[str]) -> bool:
        assert directory == state and job is jobs.Job.UPDATE
        commands.append(command)
        return True

    monkeypatch.setattr(jobs, "start", start)
    assert latest_plugins.reason(host, state, full=False) is not None
    assert commands == [latest_plugins.update_command(host)]
    assert plugin_activation.pending(state, host, origin)


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
