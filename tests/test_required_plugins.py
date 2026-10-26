import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / ".agents/agent/runtime"
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

    def test_explicit_native_inventory_remains_unfiltered_for_cli_capability(self):
        entry = self.entry("cli@tj-agents")
        with mock.patch.object(REQUIRED.subprocess, "run", return_value=subprocess.CompletedProcess(
                [], 0, json.dumps({"installed": [entry]}), "")) as run:
            roots = REQUIRED.inventory_roots(self.project, "codex", "native-codex")
        self.assertIn("cli@tj-agents", roots)
        self.assertEqual(["native-codex", "plugin", "list", "--json"], run.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
