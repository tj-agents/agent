import importlib.util
import json
from pathlib import Path
import subprocess
import sys
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
        prepare = mock.patch.object(REFRESH.native_runtime, "prepare", return_value=())
        prepare.start()
        self.addCleanup(prepare.stop)

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


if __name__ == "__main__":
    unittest.main()
