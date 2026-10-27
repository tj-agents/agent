import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "plugins/agent/.agents/agent/runtime"
sys.path.insert(0, str(RUNTIME))
import required_plugins as REQUIRED


class RequiredPluginInventoryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="agent-required-plugins-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.project, self.profile = self.root / "project", self.root / "profile"
        (self.project / ".agents").mkdir(parents=True)
        self.environment = mock.patch.dict(os.environ, {"CODEX_HOME": str(self.profile)})
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def entry(self, identity):
        name, marketplace = identity.split("@")
        root = self.profile / "plugins/cache" / marketplace / name / "version"
        (root / ".codex-plugin").mkdir(parents=True)
        (root / ".codex-plugin/plugin.json").write_text(json.dumps({"name": name}), encoding="utf-8")
        return {"name": name, "marketplaceName": marketplace, "version": "version", "enabled": True}

    def select(self, *identities):
        (self.project / ".agents/plugins.json").write_text(json.dumps({"schema_version": 1, "plugins": identities}), encoding="utf-8")

    def test_selected_codex_plugins_query_each_publisher_without_unfiltered_inventory(self):
        first, second = "agent@tj-agents", "cli@other-publisher"
        self.select(first, second)
        entries = {"tj-agents": self.entry(first), "other-publisher": self.entry(second)}

        def response(command, **_kwargs):
            marketplace = command[command.index("--marketplace") + 1]
            return subprocess.CompletedProcess(command, 0, json.dumps({"installed": [entries[marketplace]]}), "")

        with mock.patch.object(REQUIRED.subprocess, "run", side_effect=response) as run:
            result = REQUIRED.check(self.project, "codex", "native-codex")
        self.assertEqual({first, second}, set(result))
        self.assertEqual([
            ["native-codex", "plugin", "list", "--marketplace", "other-publisher", "--json"],
            ["native-codex", "plugin", "list", "--marketplace", "tj-agents", "--json"],
        ], [call.args[0] for call in run.call_args_list])

    def test_native_inventory_can_finish_after_the_old_five_second_limit(self):
        script = self.root / "slow inventory.py"
        script.write_text("import time\ntime.sleep(5.2)\nprint('[]')\n", encoding="utf-8")
        native_run = subprocess.run

        def run(_command, **options):
            return native_run([sys.executable, str(script)], **options)

        with mock.patch.object(REQUIRED.subprocess, "run", side_effect=run):
            self.assertEqual([], REQUIRED.inventory("native-claude", "claude", self.project, ()))

    def test_multiple_marketplaces_share_one_inventory_deadline(self):
        self.select("agent@a", "agent@b", "agent@c")
        clock = iter((0.0, 0.0, 12.0, 21.0))
        with mock.patch.object(REQUIRED.time, "monotonic", side_effect=lambda: next(clock)), \
                mock.patch.object(REQUIRED, "inventory", return_value=[]) as inventory:
            with self.assertRaisesRegex(REQUIRED.RequiredPluginsError, "20-second budget"):
                REQUIRED.check(self.project, "codex", "native-codex")
        self.assertEqual([20.0, 8.0], [call.kwargs["timeout"] for call in inventory.call_args_list])

    def test_explicit_native_inventory_remains_unfiltered_for_cli_capability(self):
        entry = self.entry("cli@tj-agents")
        with mock.patch.object(REQUIRED.subprocess, "run", return_value=subprocess.CompletedProcess(
                [], 0, json.dumps({"installed": [entry]}), "")) as run:
            roots = REQUIRED.inventory_roots(self.project, "codex", "native-codex")
        self.assertIn("cli@tj-agents", roots)
        self.assertEqual(["native-codex", "plugin", "list", "--json"], run.call_args.args[0])

    def test_claude_launch_roots_reuse_overrides_until_the_delimiter(self):
        roots = {"agent@tj-agents": self.root / "override-agent"}
        arguments = [
            "--settings", "override.json", "--setting-sources=project,local",
            "--plugin-dir", "plugins", "--plugin-url=https://example.test/plugin", "--",
            "--settings", "literal prompt",
        ]
        with mock.patch.object(REQUIRED, "check", return_value=roots) as check:
            effective, actual = REQUIRED.check_launch_roots(self.project, "claude", "native-claude", arguments)
        self.assertEqual(self.project.resolve(), effective)
        self.assertIs(roots, actual)
        check.assert_called_once_with(
            self.project.resolve(), "claude", "native-claude",
            inventory_options=(
                "--settings", "override.json", "--setting-sources", "project,local",
                "--plugin-dir", "plugins", "--plugin-url", "https://example.test/plugin",
            ),
        )


if __name__ == "__main__":
    unittest.main()
