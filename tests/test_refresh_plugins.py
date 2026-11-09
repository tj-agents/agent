import importlib.util
import io
import json
from pathlib import Path
import os
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
)
import install
import plugin_copies
from tj_agent.host import Host


ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "plugins/agent/.agents/agent/runtime"
sys.path.insert(0, str(RUNTIME))


def load(name):
    spec = importlib.util.spec_from_file_location(name, RUNTIME / "plugins" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


REFRESH = load("refresh")


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
        homes = _patched_homes(Path(temporary.name))
        homes.start()
        self.addCleanup(homes.stop)
        self.started = Started()
        starts = mock.patch.object(REFRESH.jobs.process, "start", self.started)
        starts.start()
        self.addCleanup(starts.stop)
        machine = mock.patch.object(REFRESH.latest_plugins.machine_state, "drift", return_value={})
        machine.start()
        self.addCleanup(machine.stop)

    def install(self, plugin, revision, pin, label=None, codex_label=None):
        marketplace = self.claude_home / "plugins" / "marketplaces" / "tj-agents" / ".claude-plugin"
        marketplace.mkdir(parents=True, exist_ok=True)
        catalog = marketplace / "marketplace.json"
        entries = json.loads(catalog.read_text(encoding="utf-8"))["plugins"] if catalog.exists() else []
        catalog.write_text(json.dumps({"plugins": [*entries, {"name": plugin, "source": {"sha": pin}}]}),
                           encoding="utf-8")
        root = self.claude_home / "plugins" / "cache" / "tj-agents" / plugin / revision[:12]
        (root / ".codex-plugin").mkdir(parents=True)
        harness = {"plugin": f"tj-agents/{plugin}", "requires": {}}
        (root / "harness.json").write_text(json.dumps(harness), encoding="utf-8")
        if label:
            (root / ".codex-plugin" / "plugin.json").write_text(json.dumps({"version": label}), encoding="utf-8")
        registry = self.claude_home / "plugins" / "installed_plugins.json"
        rows = json.loads(registry.read_text(encoding="utf-8"))["plugins"] if registry.exists() else {}
        rows[f"{plugin}@tj-agents"] = [{"scope": "user", "gitCommitSha": revision, "installPath": str(root)}]
        registry.write_text(json.dumps({"plugins": rows}), encoding="utf-8")
        if codex_label:
            (self.codex_home / "plugins" / "cache" / "tj-agents" / plugin / codex_label).mkdir(parents=True)

    def test_one_failing_update_is_reported_and_the_rest_still_run(self):
        listing = json.dumps([
            {"id": "agent@tj-agents", "scope": "user"},
            {"id": "base@tj-agents", "scope": "user"},
        ])

        def run(command, **_kwargs):
            if command[:3] == ["claude", "plugin", "list"]:
                return completed(0, listing)
            if command[:3] == ["claude", "plugin", "update"] and "agent@tj-agents" in command:
                return completed(1, "", "update failed")
            if command[:2] == ["claude", "agents"]:
                return completed(0, json.dumps([
                    {"kind": "background", "id": "live1", "pid": 7},
                    {"kind": "background", "id": "dead1"},
                    {"kind": "interactive", "pid": 8, "name": "tab"},
                ]))
            return completed(0)

        with mock.patch.object(REFRESH.process, "run", side_effect=run) as spawned:
            self.assertEqual(1, REFRESH.main(["--host", "claude"]))
        commands = [call.args[0] for call in spawned.call_args_list]
        self.assertIn(["claude", "plugin", "update", "agent@tj-agents", "--scope", "user", "--json", "-y"], commands)
        self.assertIn(["claude", "plugin", "update", "base@tj-agents", "--scope", "user", "--json", "-y"], commands)
        self.assertEqual([["claude", "respawn", "live1"]], [c for c in commands if c[:2] == ["claude", "respawn"]])

    def test_claude_updates_use_scope_user_only_for_user_scope_rows_of_selected_marketplaces(self):
        listing = json.dumps([
            {"id": "agent@tj-agents", "scope": "user"},
            {"id": "agent@tj-agents", "scope": "project"},
            {"id": "other@elsewhere", "scope": "user"},
            {"id": "base@agents", "scope": "user"},
        ])

        def run(command, **_kwargs):
            if command[:3] == ["claude", "plugin", "list"]:
                return completed(0, listing)
            return completed(0)

        with mock.patch.object(REFRESH.process, "run", side_effect=run) as spawned:
            self.assertEqual(0, REFRESH.main(["--host", "claude"]))
        commands = [call.args[0] for call in spawned.call_args_list]
        updates = [command for command in commands if command[:3] == ["claude", "plugin", "update"]]
        self.assertEqual([
            ["claude", "plugin", "update", "agent@tj-agents", "--scope", "user", "--json", "-y"],
            ["claude", "plugin", "update", "base@agents", "--scope", "user", "--json", "-y"],
        ], updates)

    def test_codex_upgrades_each_marketplace_then_adds_each_installed_plugin(self):
        listing = json.dumps({"installed": [
            {"pluginId": "agent@tj-agents", "marketplaceName": "tj-agents"},
            {"pluginId": "base@agents", "marketplaceName": "agents"},
            {"pluginId": "other@elsewhere", "marketplaceName": "elsewhere"},
        ]})

        def run(command, **_kwargs):
            if command[:3] == ["codex", "plugin", "list"]:
                return completed(0, listing)
            return completed(0)

        with mock.patch.object(REFRESH.process, "run", side_effect=run) as spawned:
            self.assertEqual(0, REFRESH.main(["--host", "codex"]))
        commands = [call.args[0] for call in spawned.call_args_list]
        command_fn = REFRESH.codex_installed.codex_command
        self.assertEqual(
            [
                command_fn("plugin", "marketplace", "upgrade", "agents"),
                command_fn("plugin", "marketplace", "upgrade", "tj-agents"),
            ],
            [
                command
                for command in commands
                if command[:3] == ["codex", "plugin", "marketplace"]
            ],
        )
        self.assertEqual(
            [
                command_fn("plugin", "add", "agent@tj-agents"),
                command_fn("plugin", "add", "base@agents"),
            ],
            [
                command
                for command in commands
                if command[:3] == ["codex", "plugin", "add"]
            ],
        )


    def test_claude_skips_plugins_already_at_their_pin(self):
        self.install("agent", "a" * 40, "a" * 40)
        self.install("base", "b" * 40, "c" * 40)
        listing = json.dumps([{"id": "agent@tj-agents", "scope": "user"}, {"id": "base@tj-agents", "scope": "user"}])

        def run(command, **_kwargs):
            return completed(0, listing) if command[:3] == ["claude", "plugin", "list"] else completed(0, "[]")

        with mock.patch.object(REFRESH.process, "run", side_effect=run) as spawned:
            self.assertEqual(0, REFRESH.main(["--host", "claude"]))
        updates = [call.args[0] for call in spawned.call_args_list if call.args[0][:3] == ["claude", "plugin", "update"]]
        self.assertEqual([["claude", "plugin", "update", "base@tj-agents", "--scope", "user", "--json", "-y"]], updates)

    def test_codex_skips_only_plugins_whose_installed_build_matches_the_current_label(self):
        self.install("agent", "a" * 40, "a" * 40, label="label1", codex_label="label1")
        self.install("base", "b" * 40, "b" * 40, label="label2", codex_label="old")
        self.install("cli", "c" * 40, "c" * 40)
        listing = json.dumps({"installed": [
            {"pluginId": f"{name}@tj-agents", "marketplaceName": "tj-agents"} for name in ("agent", "base", "cli")
        ]})

        def run(command, **_kwargs):
            return completed(0, listing) if command[:3] == ["codex", "plugin", "list"] else completed(0)

        with mock.patch.object(REFRESH.process, "run", side_effect=run) as spawned:
            self.assertEqual(0, REFRESH.main(["--host", "codex"]))
        adds = [call.args[0] for call in spawned.call_args_list if call.args[0][:3] == ["codex", "plugin", "add"]]
        command_fn = REFRESH.codex_installed.codex_command
        self.assertEqual(
            [
                command_fn("plugin", "add", "base@tj-agents"),
                command_fn("plugin", "add", "cli@tj-agents"),
            ],
            adds,
        )

    def test_a_held_refresh_lock_makes_a_second_refresh_exit_without_running(self):
        job = REFRESH.jobs.Job.REFRESH
        with REFRESH.jobs.held(self.state, job) as first, mock.patch.object(REFRESH.process, "run") as spawned:
            self.assertTrue(first)
            self.assertEqual(0, REFRESH.main(["--host", "claude"]))
        spawned.assert_not_called()
        self.assertFalse(REFRESH.jobs.lock_path(self.state, job).exists())

    def test_a_refresh_records_its_failures_for_the_stale_check(self):
        def run(command, **_kwargs):
            return completed(1, "", "boom") if command[:3] == ["claude", "plugin", "list"] else completed(0, "[]")

        with mock.patch.object(REFRESH.process, "run", side_effect=run):
            self.assertEqual(1, REFRESH.main(["--host", "claude"]))
        job = REFRESH.jobs.Job.REFRESH
        self.assertEqual("claude: list: boom", REFRESH.jobs.recent_failure(self.state, job))
        self.assertFalse(REFRESH.jobs.running(self.state, job))


def _patched_homes(home: Path) -> Any:
    return mock.patch.dict(os.environ, environment(home))


def _current_machine(home: Path) -> Any:
    claude = home / "claude"
    pin_plugin(claude, "agent", AGENT_PIN)
    agent_root(install_plugin(claude, "agent", AGENT_PIN), "agent build")
    context = machine_context(home)
    converge(context, REFRESH.latest_plugins.machine_state.packaged("install.py").parents[3])
    return mock.patch.object(install, "machine_context", return_value=context)


def test_codex_commands_carry_marketplace_overrides_and_no_cwd(tmp_path: Path) -> None:
    listing = json.dumps(
        {"installed": [{"pluginId": "agent@tj-agents", "marketplaceName": "tj-agents"}]}
    )

    def run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        if command[:3] == ["codex", "plugin", "list"]:
            return completed(0, listing)
        return completed(0)

    with (
        _patched_homes(tmp_path),
        mock.patch.object(REFRESH.process, "run", side_effect=run) as spawned,
        mock.patch.object(REFRESH.jobs.process, "start", Started()),
        mock.patch.object(REFRESH.latest_plugins.machine_state, "drift", return_value={}),
    ):
        assert REFRESH.main(["--host", "codex"]) == 0
    overrides = REFRESH.codex_installed.MARKETPLACE_OVERRIDES
    codex_calls = [
        call for call in spawned.call_args_list if call.args[0][0] == "codex"
    ]
    assert codex_calls
    for call in codex_calls:
        assert tuple(call.args[0][-len(overrides) :]) == overrides
        assert call.kwargs.get("cwd") is None


def test_codex_refresh_fails_with_no_tj_agents_rows_after_listing(
    tmp_path: Path,
) -> None:
    listing = json.dumps(
        {"installed": [{"pluginId": "other@elsewhere", "marketplaceName": "elsewhere"}]}
    )

    def run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        if command[:3] == ["codex", "plugin", "list"]:
            return completed(0, listing)
        return completed(0)

    with (
        _patched_homes(tmp_path),
        mock.patch.object(REFRESH.process, "run", side_effect=run),
        mock.patch.object(REFRESH.jobs.process, "start", Started()),
        mock.patch.object(REFRESH.latest_plugins.machine_state, "drift", return_value={}),
    ):
        assert REFRESH.main(["--host", "codex"]) == 1
        failure = REFRESH.jobs.recent_failure(
            tmp_path / "state", REFRESH.jobs.Job.REFRESH
        )
        assert failure == "codex: list: no tj-agents plugins installed"


OLD_PIN = "a" * 40
NEW_PIN = "b" * 40
AGENT_PIN = "c" * 40


def test_refresh_runs_only_plugin_commands_on_both_hosts(tmp_path: Path) -> None:
    claude = tmp_path / "claude"
    pin_plugin(claude, "personal", OLD_PIN)
    install_plugin(claude, "personal", NEW_PIN, converge=True)
    claude_listing = json.dumps([{"id": "personal@tj-agents", "scope": "user"}])
    codex_listing = json.dumps(
        {
            "installed": [
                {"pluginId": "personal@tj-agents", "marketplaceName": "tj-agents"}
            ]
        }
    )

    def run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        if command[:3] == ["claude", "plugin", "list"]:
            return completed(0, claude_listing)
        if command[:3] == ["codex", "plugin", "list"]:
            return completed(0, codex_listing)
        return completed(0, "[]")

    started = Started()
    with _patched_homes(tmp_path), _current_machine(tmp_path), (
        mock.patch.object(REFRESH.process, "run", side_effect=run)
    ) as spawned, mock.patch.object(REFRESH.jobs.process, "start", started):
        assert REFRESH.main([]) == 0
    commands = [call.args[0] for call in spawned.call_args_list]
    assert {command[0] for command in commands} == {"claude", "codex"}
    assert not [command for command in commands if "converge" in command]
    assert started.commands == []


def test_a_pin_moved_by_refresh_is_converged_before_the_next_prompt(
    tmp_path: Path,
) -> None:
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
    with _patched_homes(tmp_path), _current_machine(tmp_path) as current, (
        mock.patch.object(REFRESH.process, "run", side_effect=run)
    ) as spawned, mock.patch.object(REFRESH.jobs.process, "start", started):
        context = current.return_value
        context.run = converge_copies
        script = REFRESH.latest_plugins.machine_state.packaged("install.py")
        copies = [sys.executable, "-B", str(script), "--step", "plugin copies"]
        plugin_copies.record(state, plugin_copies.sources())
        assert REFRESH.latest_plugins.reason(Host.CLAUDE, state, full=True) is None

        assert REFRESH.main(["--host", "claude"]) == 0
        assert {call.args[0][0] for call in spawned.call_args_list} == {"claude"}
        assert started.commands == [copies]

        with REFRESH.jobs.held(state, REFRESH.jobs.Job.INSTALL):
            refusal = REFRESH.latest_plugins.reason(Host.CLAUDE, state, full=True)
        assert refusal is not None
        assert refusal.startswith("Updating tj-agents plugin copies")
        assert started.commands == [copies]

        assert (
            install.run_steps([step], context, False, io.StringIO(), io.StringIO()) == 0
        )
        assert REFRESH.latest_plugins.reason(Host.CLAUDE, state, full=True) is None
    root = (claude / "plugins/cache/tj-agents/personal" / NEW_PIN[:12]).resolve()
    script = [sys.executable, "-B", str(root / "scripts/setup.py"), "converge"]
    assert [(command, cwd.resolve()) for command, cwd in converged] == [(script, root)]
    assert started.commands == [copies]


if __name__ == "__main__":
    unittest.main()
