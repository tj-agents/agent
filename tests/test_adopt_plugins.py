import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "plugins" / "agent" / ".agents" / "agent" / "utility" / "plugins" / "scripts"
sys.path.insert(0, str(SCRIPTS))


def load(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ADOPT = load("adopt_plugins")
LockError = ADOPT.lock_io.LockError


class RemovePluginTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "project"
        (self.root / ".agents").mkdir(parents=True)

    def write_selection(self, plugins):
        (self.root / ".agents" / "plugins.json").write_text(json.dumps({
            "schema_version": 1,
            "marketplaces": {"tj-agents": {"repository": "tj-agents/marketplace"}},
            "plugins": plugins,
        }), encoding="utf-8")

    def selected(self):
        return json.loads((self.root / ".agents" / "plugins.json").read_text(encoding="utf-8"))["plugins"]

    def test_removal_of_agent_cli_prerequisite_refuses_without_writing(self):
        self.write_selection(["agent@tj-agents", "base@tj-agents", "cli@tj-agents"])
        before = (self.root / ".agents" / "plugins.json").read_bytes()
        with self.assertRaisesRegex(LockError, "prerequisite"):
            ADOPT.run(self.root, [], [], "write", removals=["cli"])
        self.assertEqual(before, (self.root / ".agents" / "plugins.json").read_bytes())

    def test_missing_removal_is_idempotent_and_does_not_restore_defaults(self):
        self.write_selection(["base@tj-agents", "cli@tj-agents"])
        ADOPT.run(self.root, [], [], "write", removals=["cli"])
        self.assertEqual(["base@tj-agents"], self.selected())
        self.assertEqual([], ADOPT.run(self.root, [], [], "check", removals=["cli"]))

    def test_unpublished_selections_converge_away_without_a_removal_request(self):
        self.write_selection(["agent@tj-agents", "agent-store@tj-agents", "base@tj-agents", "cli@tj-agents",
                              "kit@tj-agents", "machine@tj-agents"])
        ADOPT.run(self.root, [], [], "write")
        self.assertEqual(["agent-store@tj-agents", "agent@tj-agents", "base@tj-agents", "cli@tj-agents"],
                         self.selected())
        self.assertNotIn("machine@tj-agents", (self.root / ".claude" / "settings.json").read_text(encoding="utf-8"))
        self.assertEqual([], ADOPT.run(self.root, [], [], "check"))

    def test_default_selection_never_selects_the_retired_machine_package(self):
        ADOPT.run(self.root, [], [], "write")
        self.assertIn("cli@tj-agents", self.selected())
        self.assertNotIn("machine@tj-agents", self.selected())

    def test_automatic_reconciliation_checks_then_repairs_both_hosts_and_is_idempotent(self):
        self.write_selection(["engineering@tj-agents", "machine@tj-agents", "python@tj-agents"])
        before = (self.root / ".agents/plugins.json").read_bytes()
        with mock.patch.object(ADOPT, "current_roster", return_value=ADOPT.load_roster()):
            self.assertIn(".agents/plugins.json", ADOPT.reconcile(self.root, "check"))
            self.assertEqual(before, (self.root / ".agents/plugins.json").read_bytes())
            self.assertFalse((self.root / ".codex").exists())
            ADOPT.reconcile(self.root, "write")
        for identity in ("agent@tj-agents", "cli@tj-agents", "python@tj-agents"):
            self.assertIn(identity, self.selected())
        for path in (".agents/plugins.json", ".claude/settings.json", ".codex/config.toml"):
            self.assertNotIn("machine@tj-agents", (self.root / path).read_text(encoding="utf-8"))
        before = {path: (path.read_bytes(), path.stat().st_mtime_ns)
                  for path in self.root.rglob("*") if path.is_file()}
        self.assertEqual([], ADOPT.reconcile(self.root, "write"))
        self.assertEqual(before, {path: (path.read_bytes(), path.stat().st_mtime_ns)
                                  for path in self.root.rglob("*") if path.is_file()})

    def test_automatic_reconciliation_does_not_drop_newer_published_plugins(self):
        self.write_selection(["base@tj-agents", "new-plugin@tj-agents"])
        roster = ADOPT.load_roster()
        roster["plugins"]["new-plugin"] = ["base"]
        with mock.patch.object(ADOPT, "current_roster", return_value=roster) as current:
            ADOPT.reconcile(self.root, "write")
        current.assert_called_once()
        self.assertIn("new-plugin@tj-agents", self.selected())

    def test_unreachable_marketplace_does_not_remove_unknown_selection(self):
        self.write_selection(["base@tj-agents", "machine@tj-agents"])
        before = (self.root / ".agents/plugins.json").read_bytes()
        with mock.patch.object(ADOPT, "current_roster", side_effect=LockError("offline")), \
                self.assertRaisesRegex(LockError, "offline"):
            ADOPT.reconcile(self.root, "write")
        self.assertEqual(before, (self.root / ".agents/plugins.json").read_bytes())
        self.assertFalse((self.root / ".codex").exists())

    def test_automatic_reconciliation_does_not_enroll_an_unconfigured_project(self):
        self.assertEqual([], ADOPT.reconcile(self.root, "write"))
        self.assertFalse((self.root / ".agents/plugins.json").exists())

    def test_removal_of_remaining_prerequisite_refuses_without_writing(self):
        self.write_selection(["agent@tj-agents", "base@tj-agents", "cli@tj-agents"])
        before = (self.root / ".agents" / "plugins.json").read_bytes()
        with self.assertRaisesRegex(LockError, "prerequisite"):
            ADOPT.run(self.root, [], [], "write", removals=["base"])
        self.assertEqual(before, (self.root / ".agents" / "plugins.json").read_bytes())


if __name__ == "__main__":
    unittest.main()
