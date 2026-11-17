import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from threading import Event

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
PREFETCH_STANDARDS = plugin_update.prefetch_standards


def test_failed_marketplace_still_settles_installed_hook_trust(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[str] = []

    def fail_marketplace(
        report: plugin_update.Report, marketplaces: tuple[str, ...]
    ) -> None:
        report.fail(Host.CODEX, "marketplace agents", "authentication failed")

    def trust_hooks(_report: plugin_update.Report) -> bool:
        events.append("trust")
        return True

    monkeypatch.setattr(plugin_update, "already_current", lambda *_args: True)
    monkeypatch.setattr(codex_installed, "update_marketplaces", fail_marketplace)
    monkeypatch.setattr(codex_installed, "trust_hooks", trust_hooks)
    monkeypatch.setattr(
        codex_installed, "activate", lambda _report: events.append("activate")
    )
    outcome = plugin_update.update(Host.CODEX, Digests(tmp_path / "state"), {})

    assert outcome.failures == ["codex: marketplace agents: authentication failed"]
    assert events == ["trust"]
    assert not outcome.changed
    assert not (tmp_path / "state" / "activated-codex.txt").exists()


def test_failed_native_update_preserves_the_cause_before_its_summary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    diagnostic = (
        "Failed to upgrade marketplace agents: credential helper is missing\n"
        "Error: 1 upgrade failure(s) occurred."
    )
    monkeypatch.setattr(
        plugin_update.Report,
        "run",
        staticmethod(
            lambda command: subprocess.CompletedProcess(command, 1, "", diagnostic)
        ),
    )
    report = plugin_update.Report()

    assert report.step(Host.CODEX, "marketplace agents", ["codex"]) is None
    assert report.failures == [f"codex: marketplace agents: {diagnostic}"]


def update_plugins(host: str) -> int:
    outcome = plugin_update.update(HOSTS[host], Digests(jobs.state_directory()), {})
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
    monkeypatch.setattr(plugin_update, "prefetch_standards", lambda _host: None)
    return commands


@pytest.mark.parametrize("host", list(Host))
def test_plugin_updates_report_standards_drift_without_rewriting_the_repository(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    host: Host,
    capsys: pytest.CaptureFixture[str],
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
        return {"python@tj-agents": installed}

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(jobs, "state_directory", lambda: tmp_path / "state")
    monkeypatch.setattr(plugin_update, "already_current", lambda *args: False)
    adapter = claude_installed if host is Host.CLAUDE else codex_installed
    monkeypatch.setattr(adapter, "refresh", download)
    monkeypatch.setattr(adapter, "activate", lambda _report: events.append("activated"))
    monkeypatch.setattr(required_plugins, "check", roots)
    assert update_plugins(host.value) == 0
    assert events == ["download", "inventory", "activated"]
    assert instructions.read_text(encoding="utf-8") == original
    assert "--write to update repository standards" in capsys.readouterr().err


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


@pytest.mark.parametrize("local", [False, True])
def test_loaded_worktree_standards_preserve_inherited_local_adoption(
    tmp_path: Path,
    plugin_source: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    local: bool,
) -> None:
    (plugin_source / "README.md").write_text("Primary checkout\n", encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(plugin_source), "add", "README.md"], check=True, timeout=15
    )
    subprocess.run(
        ["git", "-C", str(plugin_source), "commit", "-qm", "initial"],
        check=True,
        timeout=15,
    )
    linked = tmp_path / "linked checkout"
    subprocess.run(
        [
            "git",
            "-C",
            str(plugin_source),
            "worktree",
            "add",
            "-q",
            "--detach",
            str(linked),
        ],
        check=True,
        timeout=15,
    )
    selection = plugin_source / ".agents/plugins.json"
    selection.parent.mkdir()
    selection.write_text(
        '{"schema_version": 1, "plugins": ["python@tj-agents"]}', encoding="utf-8"
    )
    if local:
        (selection.parent / "repo-capabilities.local.json").write_text(
            "{}", encoding="utf-8"
        )
    instructions = linked / "AGENTS.md"
    original = b"Owner's instructions\r\n"
    instructions.write_bytes(original)
    installed = tmp_path / "installed"
    standard = installed / ".agents/python/convention/modules/SKILL.md"
    standard.parent.mkdir(parents=True)
    standard.write_text(
        "---\nname: modules\nkind: convention\nprofile: core\n"
        "applicability: Python projects\n---\nFull modules standard\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(jobs, "state_directory", lambda: tmp_path / "state")
    monkeypatch.setattr(
        codex_installed.catalog,
        "recorded",
        lambda _state: {
            "python@tj-agents": codex_installed.catalog.Build("fixture", "test", None)
        },
    )
    monkeypatch.setattr(codex_installed.catalog, "root", lambda *_args: installed)
    monkeypatch.setattr(codex_installed.catalog, "current", lambda *_args: True)

    codex_installed.update_project_standards(linked)

    assert instructions.read_bytes() == original
    diagnostic = capsys.readouterr().err
    if local:
        assert not diagnostic
    else:
        assert "tj prepare --host codex --write" in diagnostic
        assert str(linked) in diagnostic
    assert not (linked / ".agents/repo-capabilities.local.json").exists()


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
        plugin_update.update(Host.CLAUDE, Digests(state), {})
    assert report_commands == []
    events: list[str] = []
    monkeypatch.setattr(plugin_update, "already_current", lambda *args: True)
    monkeypatch.setattr(required_plugins, "check", lambda *args: None)
    monkeypatch.setattr(
        claude_installed, "activate", lambda *args: events.append("activated")
    )
    outcome = plugin_update.update(Host.CLAUDE, Digests(state), {})
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
        monkeypatch.setattr(
            plugin_update, "marketplace_revision", lambda *_, at=revision: at
        )
        assert not plugin_update.update(
            Host.CODEX, Digests(tmp_path / "state"), {}
        ).failures
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
    outcome = plugin_update.update(Host.CODEX, Digests(state), {})
    assert any("connection failed" in failure for failure in outcome.failures)
    assert report_commands == []
    failed = False
    outcome = plugin_update.update(Host.CODEX, Digests(state), {})
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


@pytest.mark.parametrize("host", list(Host))
@pytest.mark.parametrize("change", ["unchanged", "marketplace", "repair"])
@pytest.mark.parametrize("prefetch_fails", [False, True])
def test_inventory_prefetch_is_reused_only_without_marketplace_or_plugin_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    host: Host,
    change: str,
    prefetch_fails: bool,
) -> None:
    selection = tmp_path / ".agents/plugins.json"
    selection.parent.mkdir()
    selection.write_text(
        '{"schema_version": 1, "plugins": ["python@tj-agents"]}', encoding="utf-8"
    )
    instructions = tmp_path / "AGENTS.md"
    instructions.write_text("Keep these instructions\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    builds = {}
    for version in ("before", "after"):
        build = tmp_path / "codex/plugins/cache/tj-agents/python" / version
        for manifest_directory in (".claude-plugin", ".codex-plugin"):
            manifest = build / manifest_directory / "plugin.json"
            manifest.parent.mkdir(parents=True)
            manifest.write_text('{"name": "python"}', encoding="utf-8")
        skill = build / ".agents/python/convention" / version / "SKILL.md"
        skill.parent.mkdir(parents=True)
        skill.write_text(
            f"---\nname: {version}\nkind: convention\nprofile: core\n"
            "applicability: Python projects\n---\n",
            encoding="utf-8",
        )
        builds[version] = build
    standards.update(tmp_path, {"python@tj-agents": builds["before"]})
    original = instructions.read_text(encoding="utf-8")
    inventory_started = Event()
    pins_started = Event()
    heads_started = Event()
    decisions_started = Event()
    inventory_versions: list[str] = []
    remembered: list[str] = []
    mutations: list[str] = []

    def inventory(*_arguments: object, **_options: object) -> list[dict[str, object]]:
        version = "after" if mutations else "before"
        inventory_versions.append(version)
        if len(inventory_versions) == 1:
            inventory_started.set()
            assert heads_started.wait(timeout=3)
            if prefetch_fails:
                raise required_plugins.RequiredPluginsError(
                    "speculative inventory failed"
                )
        else:
            assert decisions_started.is_set()
        return [
            {
                "id": "python@tj-agents",
                "pluginId": "python@tj-agents",
                "name": "python",
                "marketplaceName": "tj-agents",
                "version": version,
                "installPath": str(builds[version]),
                "enabled": True,
                "installed": True,
                "scope": "user",
            }
        ]

    def pins(*_arguments: object) -> bool:
        pins_started.set()
        return change != "repair"

    def behind(_host: Host, name: str, environ: object) -> bool:
        assert environ == {"fixture": "explicit environment"}
        assert inventory_started.wait(timeout=3)
        assert pins_started.wait(timeout=3)
        assert not remembered
        heads_started.set()
        return change == "marketplace" and name == "tj-agents"

    def marketplaces(_report: plugin_update.Report, names: tuple[str, ...]) -> None:
        decisions_started.set()
        assert instructions.read_text(encoding="utf-8") == original
        if names:
            mutations.append("marketplace")

    def repair(*_arguments: object) -> None:
        mutations.append("repair")

    def remember(entries: list[dict[str, object]]) -> None:
        assert decisions_started.is_set()
        remembered.extend(str(entry["version"]) for entry in entries)

    adapter = plugin_update.INSTALLED[host]
    monkeypatch.setattr(adapter, "update_marketplaces", marketplaces)
    monkeypatch.setattr(adapter, "refresh", repair)
    monkeypatch.setattr(adapter, "activate", lambda _report: None)
    monkeypatch.setattr(plugin_update, "already_current", pins)
    monkeypatch.setattr(plugin_update, "behind_remote", behind)
    monkeypatch.setattr(plugin_update, "prefetch_standards", PREFETCH_STANDARDS)
    monkeypatch.setattr(required_plugins, "inventory", inventory)
    monkeypatch.setattr(shutil, "which", lambda _name: "native-host")
    monkeypatch.setattr(codex_installed.catalog, "remember", remember)

    outcome = plugin_update.update(
        host, Digests(tmp_path / "state"), {"fixture": "explicit environment"}
    )

    failed = prefetch_fails and change == "unchanged"
    assert bool(outcome.failures) == failed
    assert inventory_versions == (
        ["before"] if change == "unchanged" else ["before", "after"]
    )
    if failed:
        assert instructions.read_text(encoding="utf-8") == original
        expected = "before" if change == "unchanged" else "after"
        assert remembered == (
            [] if prefetch_fails or host is Host.CLAUDE else [expected]
        )
    else:
        assert instructions.read_text(encoding="utf-8") == original
        expected = "before" if change == "unchanged" else "after"
        assert remembered == ([expected] if host is Host.CODEX else [])
