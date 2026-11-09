import importlib.util
import json
from pathlib import Path
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "plugins/agent/.agents/agent/runtime"
sys.path.insert(0, str(RUNTIME))


def load(name):
    spec = importlib.util.spec_from_file_location(name, RUNTIME / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


REFRESH = load("refresh")


def completed(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


class RefreshPluginsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.claude_home = Path(temporary.name) / "claude"
        self.codex_home = Path(temporary.name) / "codex"
        self.state = Path(temporary.name) / "state"
        homes = mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(self.claude_home),
                                             "CODEX_HOME": str(self.codex_home),
                                             "AGENT_STATE_DIRECTORY": str(self.state)})
        homes.start()
        self.addCleanup(homes.stop)

    def install(self, plugin, revision, pin, label=None, codex_label=None):
        marketplace = self.claude_home / "plugins" / "marketplaces" / "tj-agents" / ".claude-plugin"
        marketplace.mkdir(parents=True, exist_ok=True)
        catalog = marketplace / "marketplace.json"
        entries = json.loads(catalog.read_text(encoding="utf-8"))["plugins"] if catalog.exists() else []
        catalog.write_text(json.dumps({"plugins": [*entries, {"name": plugin, "source": {"sha": pin}}]}),
                           encoding="utf-8")
        root = self.claude_home / "plugins" / "cache" / "tj-agents" / plugin / revision[:12]
        (root / ".codex-plugin").mkdir(parents=True)
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
        self.assertEqual([
            ["codex", "plugin", "marketplace", "upgrade", "agents"],
            ["codex", "plugin", "marketplace", "upgrade", "tj-agents"],
        ], [command for command in commands if command[:3] == ["codex", "plugin", "marketplace"]])
        self.assertEqual([
            ["codex", "plugin", "add", "agent@tj-agents"],
            ["codex", "plugin", "add", "base@agents"],
        ], [command for command in commands if command[:3] == ["codex", "plugin", "add"]])


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
        self.assertEqual([["codex", "plugin", "add", "base@tj-agents"], ["codex", "plugin", "add", "cli@tj-agents"]], adds)

    def test_a_held_refresh_lock_makes_a_second_refresh_exit_without_running(self):
        with REFRESH.refresh_lock.held() as first, mock.patch.object(REFRESH.process, "run") as spawned:
            self.assertTrue(first)
            self.assertEqual(0, REFRESH.main(["--host", "claude"]))
        spawned.assert_not_called()
        self.assertFalse(REFRESH.refresh_lock.lock_path().exists())

    def test_a_refresh_records_its_failures_for_the_stale_check(self):
        def run(command, **_kwargs):
            return completed(1, "", "boom") if command[:3] == ["claude", "plugin", "list"] else completed(0, "[]")

        with mock.patch.object(REFRESH.process, "run", side_effect=run):
            self.assertEqual(1, REFRESH.main(["--host", "claude"]))
        self.assertEqual("claude: list: boom", REFRESH.refresh_lock.recent_failure())
        self.assertFalse(REFRESH.refresh_lock.running())

    def test_refresh_converges_the_runtime_lifecycle_commands_and_shell_profiles_through_the_installer(self):
        with mock.patch.object(REFRESH.process, "run", return_value=completed(0, "[]")) as spawned:
            self.assertEqual(0, REFRESH.main(["--host", "claude"]))
        commands = [call.args[0] for call in spawned.call_args_list]
        self.assertIn([sys.executable, "-B", str(REFRESH.RUNTIME / "install.py"), "--step", "tj lifecycle commands",
                       "--step", "shell profiles"], commands)

if __name__ == "__main__":
    unittest.main()
