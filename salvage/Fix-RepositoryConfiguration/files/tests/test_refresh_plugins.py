import importlib.util
import io
import json
from pathlib import Path
import os
import shutil
import subprocess
import sys
import tempfile
from typing import Any
import unittest
from unittest import mock

from fake_machine import (
    Started,
    agent_root,
    converge,
    environment,
    install_plugin,
    machine_context,
    pin_plugin,
    record_codex_plugin,
)
import install
import latest_plugins
import machine_state
import plugin_copies
import pytest
from tj_agent.host import Host


ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "plugins/agent/.agents/agent/runtime"
sys.path.insert(0, str(RUNTIME))


def load(name):
    spec = importlib.util.spec_from_file_location(name, RUNTIME / "tj_plugins.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


REFRESH = load("refresh")

from codex import hook_trust as codex_hook_trust  # noqa: E402
from codex import launch as codex_launch  # noqa: E402


@pytest.fixture(autouse=True)
def no_codex_daemon(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(codex_launch, "liveness", lambda **_kwargs: {})
    monkeypatch.setattr(codex_launch, "executable", lambda: "codex")


def completed(
    returncode: int = 0, stdout: str = "", stderr: str = ""
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


class RefreshPluginsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.claude_home = Path(temporary.name) / "claude"
        self.codex_home = Path(temporary.name) / "codex"
        self.state = Path(temporary.name) / "state"
        previous = Path.cwd()
        os.chdir(temporary.name)
        self.addCleanup(os.chdir, previous)
        selection = Path(temporary.name) / ".agents/plugins.json"
        selection.parent.mkdir()
        selection.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "plugins": [
                        "agent@tj-agents",
                        "base@tj-agents",
                        "base@agents",
                    ],
                }
            ),
            encoding="utf-8",
        )
        homes = _patched_homes(Path(temporary.name))
        homes.start()
        self.addCleanup(homes.stop)
        self.started = Started()
        starts = mock.patch.object(REFRESH.jobs.process, "start", self.started)
        starts.start()
        self.addCleanup(starts.stop)
        machine = mock.patch.object(machine_state, "drift", return_value={})
        machine.start()
        self.addCleanup(machine.stop)

    def install(self, plugin, revision, pin, label=None, codex_label=None):
        marketplace = (
            self.claude_home
            / "plugins"
            / "marketplaces"
            / "tj-agents"
            / ".claude-plugin"
        )
        marketplace.mkdir(parents=True, exist_ok=True)
        catalog = marketplace / "marketplace.json"
        entries = (
            json.loads(catalog.read_text(encoding="utf-8"))["plugins"]
            if catalog.exists()
            else []
        )
        catalog.write_text(
            json.dumps(
                {"plugins": [*entries, {"name": plugin, "source": {"sha": pin}}]}
            ),
            encoding="utf-8",
        )
        root = (
            self.claude_home
            / "plugins"
            / "cache"
            / "tj-agents"
            / plugin
            / revision[:12]
        )
        (root / ".codex-plugin").mkdir(parents=True)
        harness = {"plugin": f"tj-agents/{plugin}", "requires": {}}
        (root / "harness.json").write_text(json.dumps(harness), encoding="utf-8")
        if label:
            (root / ".codex-plugin" / "plugin.json").write_text(
                json.dumps({"version": label}), encoding="utf-8"
            )
        registry = self.claude_home / "plugins" / "installed_plugins.json"
        rows = (
            json.loads(registry.read_text(encoding="utf-8"))["plugins"]
            if registry.exists()
            else {}
        )
        rows[f"{plugin}@tj-agents"] = [
            {"scope": "user", "gitCommitSha": revision, "installPath": str(root)}
        ]
        registry.write_text(json.dumps({"plugins": rows}), encoding="utf-8")
        if codex_label:
            build = (
                self.codex_home
                / "plugins"
                / "cache"
                / "tj-agents"
                / plugin
                / codex_label
            )
            shutil.copytree(root, build)
            (build / ".codex-plugin" / "plugin.json").write_text(
                json.dumps({"version": codex_label}), encoding="utf-8"
            )

    def test_one_failing_update_is_reported_and_the_rest_still_run(self):
        listing = json.dumps(
            [
                {"id": "agent@tj-agents", "scope": "user"},
                {"id": "base@tj-agents", "scope": "user"},
            ]
        )

        def run(command, **_kwargs):
            if command[:3] == ["claude", "plugin", "list"]:
                return completed(0, listing)
            if (
                command[:3] == ["claude", "plugin", "update"]
                and "agent@tj-agents" in command
            ):
                return completed(1, "", "update failed")
            if command[:2] == ["claude", "agents"]:
                return completed(
                    0,
                    json.dumps(
                        [
                            {"kind": "background", "id": "live1", "pid": 7},
                            {"kind": "background", "id": "dead1"},
                            {"kind": "interactive", "pid": 8, "name": "tab"},
                        ]
                    ),
                )
            return completed(0)

        with mock.patch.object(REFRESH.process, "run", side_effect=run) as spawned:
            self.assertEqual(1, REFRESH.main(["update", "--host", "claude"]))
        commands = [call.args[0] for call in spawned.call_args_list]
        self.assertIn(
            [
                "claude",
                "plugin",
                "update",
                "agent@tj-agents",
                "--scope",
                "user",
                "--json",
                "-y",
            ],
            commands,
        )
        self.assertIn(
            [
                "claude",
                "plugin",
                "update",
                "base@tj-agents",
                "--scope",
                "user",
                "--json",
                "-y",
            ],
            commands,
        )
        self.assertEqual(
            [],
            [c for c in commands if c[:2] == ["claude", "respawn"]],
        )

    def test_claude_updates_use_scope_user_only_for_user_scope_rows_of_selected_marketplaces(
        self,
    ):
        listing = json.dumps(
            [
                {"id": "agent@tj-agents", "scope": "user"},
                {"id": "agent@tj-agents", "scope": "project"},
                {"id": "other@elsewhere", "scope": "user"},
                {"id": "base@agents", "scope": "user"},
            ]
        )

        def run(command, **_kwargs):
            if command[:3] == ["claude", "plugin", "list"]:
                return completed(0, listing)
            return completed(0)

        with (
            mock.patch.object(REFRESH, "update_standards"),
            mock.patch.object(REFRESH.process, "run", side_effect=run) as spawned,
        ):
            self.assertEqual(0, REFRESH.main(["update", "--host", "claude"]))
        commands = [call.args[0] for call in spawned.call_args_list]
        updates = [
            command
            for command in commands
            if command[:3] == ["claude", "plugin", "update"]
        ]
        self.assertEqual(
            [
                [
                    "claude",
                    "plugin",
                    "update",
                    "agent@tj-agents",
                    "--scope",
                    "user",
                    "--json",
                    "-y",
                ],
                [
                    "claude",
                    "plugin",
                    "update",
                    "base@agents",
                    "--scope",
                    "user",
                    "--json",
                    "-y",
                ],
            ],
            updates,
        )

    def test_claude_skips_plugins_already_at_their_pin(self):
        self.install("agent", "a" * 40, "a" * 40)
        self.install("base", "b" * 40, "c" * 40)
        listing = json.dumps(
            [
                {"id": "agent@tj-agents", "scope": "user"},
                {"id": "base@tj-agents", "scope": "user"},
            ]
        )

        def run(command, **_kwargs):
            return (
                completed(0, listing)
                if command[:3] == ["claude", "plugin", "list"]
                else completed(0, "[]")
            )

        with (
            mock.patch.object(REFRESH, "update_standards"),
            mock.patch.object(REFRESH.process, "run", side_effect=run) as spawned,
        ):
            self.assertEqual(0, REFRESH.main(["update", "--host", "claude"]))
        updates = [
            call.args[0]
            for call in spawned.call_args_list
            if call.args[0][:3] == ["claude", "plugin", "update"]
        ]
        self.assertEqual(
            [
                [
                    "claude",
                    "plugin",
                    "update",
                    "base@tj-agents",
                    "--scope",
                    "user",
                    "--json",
                    "-y",
                ]
            ],
            updates,
        )

    def test_a_held_refresh_lock_makes_a_second_refresh_exit_without_running(self):
        job = REFRESH.jobs.Job.UPDATE
        with (
            REFRESH.jobs.held(self.state, job) as first,
            mock.patch.object(REFRESH.process, "run") as spawned,
        ):
            self.assertTrue(first)
            self.assertEqual(0, REFRESH.main(["update", "--host", "claude"]))
        spawned.assert_not_called()
        self.assertFalse(REFRESH.jobs.lock_path(self.state, job).exists())

    def test_a_refresh_records_its_failures_for_the_stale_check(self):
        def run(command, **_kwargs):
            return (
                completed(1, "", "boom")
                if command[:3] == ["claude", "plugin", "list"]
                else completed(0, "[]")
            )

        with mock.patch.object(REFRESH.process, "run", side_effect=run):
            self.assertEqual(1, REFRESH.main(["update", "--host", "claude"]))
        job = REFRESH.jobs.Job.UPDATE
        self.assertEqual(
            "claude: list: boom", REFRESH.jobs.recent_failure(self.state, job)
        )
        self.assertFalse(REFRESH.jobs.running(self.state, job))


def _patched_homes(home: Path) -> Any:
    return mock.patch.dict(os.environ, environment(home))


OLD_PIN = "a" * 40
NEW_PIN = "b" * 40
AGENT_PIN = "c" * 40


def _current_machine(home: Path) -> Any:
    claude = home / "claude"
    pin_plugin(claude, "agent", AGENT_PIN)
    agent_root(install_plugin(claude, "agent", AGENT_PIN), "agent build")
    context = machine_context(home)
    converge(context, machine_state.packaged("install.py").parents[3])
    return mock.patch.object(install, "machine_context", return_value=context)


def test_a_downloaded_pin_is_repaired_by_install_on_the_next_prompt(
    tmp_path: Path,
) -> None:
    selection = tmp_path / ".agents/plugins.json"
    selection.parent.mkdir(exist_ok=True)
    selection.write_text(
        json.dumps({"schema_version": 1, "plugins": ["personal@tj-agents"]}),
        encoding="utf-8",
    )
    claude = tmp_path / "claude"
    state = tmp_path / "state"
    pin_plugin(claude, "personal", OLD_PIN)
    install_plugin(claude, "personal", OLD_PIN, converge=True)
    listing = json.dumps([{"id": "personal@tj-agents", "scope": "user"}])

    def run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        if command == ["claude", "plugin", "marketplace", "update", "tj-agents"]:
            pin_plugin(claude, "personal", NEW_PIN)
        elif command[:3] == ["claude", "plugin", "list"]:
            return completed(0, listing)
        elif command[:4] == ["claude", "plugin", "update", "personal@tj-agents"]:
            install_plugin(claude, "personal", NEW_PIN, converge=True)
        return completed(0, "[]")

    converged: list[tuple[list[str], Path]] = []

    def converge_copies(
        command: list[str], **options: Any
    ) -> subprocess.CompletedProcess[str]:
        converged.append((command, options["cwd"]))
        return completed(0)

    started = Started()
    step = next(item for item in install.STEPS if item.name == "plugin copies")
    with (
        _patched_homes(tmp_path),
        _current_machine(tmp_path) as current,
        mock.patch.object(REFRESH, "update_standards"),
        mock.patch.object(REFRESH.process, "run", side_effect=run),
        mock.patch.object(REFRESH.jobs.process, "start", started),
    ):
        context = current.return_value
        context.run = converge_copies
        copies = latest_plugins.install_command(["plugin copies"])
        plugin_copies.record(state, plugin_copies.sources())
        assert latest_plugins.reason(Host.CLAUDE, state, full=True) is None

        pin_plugin(claude, "personal", NEW_PIN)
        assert REFRESH.main(["update", "--host", "claude"]) == 0
        assert started.commands == []

        assert latest_plugins.reason(Host.CLAUDE, state, full=True) is not None
        with REFRESH.jobs.held(state, REFRESH.jobs.Job.INSTALL):
            refusal = latest_plugins.reason(Host.CLAUDE, state, full=True)
        assert refusal is not None
        assert refusal.startswith("Updating tj-agents plugin copies")
        assert started.commands == [copies]

        assert (
            install.run_steps([step], context, False, io.StringIO(), io.StringIO()) == 0
        )
        assert latest_plugins.reason(Host.CLAUDE, state, full=True) is None
    root = (claude / "plugins/cache/tj-agents/personal" / NEW_PIN[:12]).resolve()
    script = [sys.executable, "-B", str(root / "scripts/setup.py"), "converge"]
    assert [(command, cwd.resolve()) for command, cwd in converged] == [(script, root)]
    assert started.commands == [copies]


@pytest.mark.parametrize("host", list(Host))
def test_refresh_selects_the_calling_host(
    host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    for candidate in Host:
        monkeypatch.delenv(candidate.session_variable, raising=False)
    monkeypatch.setenv(host.session_variable, "session")
    assert REFRESH.selected_host(None, os.environ) is host


def test_refresh_requires_a_host_when_session_context_is_ambiguous(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for host in Host:
        monkeypatch.setenv(host.session_variable, "session")
    with pytest.raises(ValueError, match="--host"):
        REFRESH.selected_host(None, os.environ)
    assert REFRESH.selected_host(Host.CLAUDE, os.environ) is Host.CLAUDE


def test_hook_refresh_pins_the_requested_host() -> None:
    assert latest_plugins.update_command(Host.CODEX)[-2:] == ["--host", "codex"]


def test_claude_requests_only_the_calling_wrapper_to_resume(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TJ_RUN_LAUNCH_ID", "this-tab")
    report = mock.Mock()
    REFRESH.claude_installed.restart(report, True)
    command = report.step.call_args.args[2]
    assert command[:3] == ["tj", "restart", "--reason"]
    report.run.assert_not_called()


@pytest.mark.parametrize("host", list(Host))
def test_current_plugins_spawn_no_subprocess(host: Host, tmp_path: Path) -> None:
    selection = tmp_path / ".agents/plugins.json"
    selection.parent.mkdir(exist_ok=True)
    selection.write_text(
        json.dumps({"schema_version": 1, "plugins": ["agent@tj-agents"]}),
        encoding="utf-8",
    )

    claude = tmp_path / "claude"
    pin_plugin(claude, "agent", AGENT_PIN)
    root = install_plugin(claude, "agent", AGENT_PIN)
    (root / ".codex-plugin").mkdir(parents=True, exist_ok=True)
    (root / ".codex-plugin/plugin.json").write_text(
        json.dumps({"name": "agent", "version": "current"}), encoding="utf-8"
    )
    shutil.copytree(root, tmp_path / "codex/plugins/cache/tj-agents/agent/current")
    with _patched_homes(tmp_path), mock.patch.object(REFRESH.process, "run") as spawned:
        record_codex_plugin(tmp_path / "codex", "agent", AGENT_PIN, "current")
        assert REFRESH.main(["update", "--host", host.value]) == 0
    spawned.assert_not_called()


def test_codex_hook_receipts_detect_content_changes_with_unchanged_commands(
    tmp_path: Path,
) -> None:
    build = tmp_path / "codex/plugins/cache/tj-agents/agent/local"
    (build / ".codex-plugin").mkdir(parents=True)
    (build / ".codex-plugin/plugin.json").write_text(
        json.dumps({"name": "agent", "hooks": "hooks.json"}), encoding="utf-8"
    )
    (build / "hooks.json").write_text(
        json.dumps(
            {
                "hooks": {
                    "SessionStart": [
                        {"hooks": [{"type": "command", "command": "python startup.py"}]}
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    script = build / "startup.py"
    script.write_text("print('before')", encoding="utf-8")
    state = tmp_path / "state"
    state.mkdir()
    with _patched_homes(tmp_path):
        (state / codex_hook_trust.RECEIPT).write_text(
            json.dumps(codex_hook_trust.published_hooks()), encoding="utf-8"
        )
        assert codex_hook_trust.pending(state) == []
        script.write_text("print('changed content')", encoding="utf-8")
        assert codex_hook_trust.pending(state) == ["trust agent@tj-agents hooks"]


def test_current_downloads_activate_an_older_managed_claude_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    selection = tmp_path / ".agents/plugins.json"
    selection.parent.mkdir(exist_ok=True)
    selection.write_text(
        json.dumps({"schema_version": 1, "plugins": ["agent@tj-agents"]}),
        encoding="utf-8",
    )
    claude = tmp_path / "claude"
    pin_plugin(claude, "agent", AGENT_PIN)
    install_plugin(claude, "agent", AGENT_PIN)
    monkeypatch.setenv("TJ_RUN_LAUNCH_ID", "fixture")
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(tmp_path / "older-agent"))
    with (
        _patched_homes(tmp_path),
        mock.patch.object(REFRESH, "update_standards"),
        mock.patch.object(REFRESH.process, "run", return_value=completed()) as run,
    ):
        assert REFRESH.main(["update", "--host", "claude"]) == 0
    assert len(run.call_args_list) == 1
    assert run.call_args.args[0][:3] == ["tj", "restart", "--reason"]


def test_a_missing_claude_install_cannot_be_current(tmp_path: Path) -> None:
    selection = tmp_path / ".agents/plugins.json"
    selection.parent.mkdir(exist_ok=True)
    selection.write_text(
        json.dumps({"schema_version": 1, "plugins": ["agent@tj-agents"]}),
        encoding="utf-8",
    )

    claude = tmp_path / "claude"
    pin_plugin(claude, "agent", AGENT_PIN)
    root = install_plugin(claude, "agent", AGENT_PIN)
    shutil.rmtree(root)
    with _patched_homes(tmp_path):
        assert not REFRESH.already_current(
            Host.CLAUDE, REFRESH.Digests(tmp_path / "state")
        )


def test_an_unknown_host_is_an_argument_error() -> None:
    with pytest.raises(SystemExit, match="2"):
        REFRESH.main(["update", "--host", "other"])
