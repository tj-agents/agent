import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / ".agents" / "agent" / "utility" / "plugins" / "scripts"
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

    def test_removal_updates_selection_and_both_native_configurations(self):
        self.write_selection(["agent@tj-agents", "base@tj-agents", "cli@tj-agents"])
        changed = ADOPT.run(self.root, [], [], "write", removals=["cli"])
        self.assertEqual([".agents/plugins.json", ".claude/settings.json", ".codex/config.toml"], changed)
        self.assertEqual(["agent@tj-agents", "base@tj-agents"], self.selected())
        claude = json.loads((self.root / ".claude" / "settings.json").read_text(encoding="utf-8"))
        self.assertNotIn("cli@tj-agents", claude["enabledPlugins"])
        self.assertNotIn('[plugins."cli@tj-agents"]', (self.root / ".codex" / "config.toml").read_text(encoding="utf-8"))

    def test_missing_removal_is_idempotent_and_does_not_restore_defaults(self):
        self.write_selection(["base@tj-agents", "cli@tj-agents"])
        ADOPT.run(self.root, [], [], "write", removals=["cli"])
        self.assertEqual(["base@tj-agents"], self.selected())
        self.assertEqual([], ADOPT.run(self.root, [], [], "check", removals=["cli"]))

    def test_removal_of_remaining_prerequisite_refuses_without_writing(self):
        self.write_selection(["agent@tj-agents", "base@tj-agents", "cli@tj-agents"])
        before = (self.root / ".agents" / "plugins.json").read_bytes()
        with self.assertRaisesRegex(LockError, "prerequisite"):
            ADOPT.run(self.root, [], [], "write", removals=["base"])
        self.assertEqual(before, (self.root / ".agents" / "plugins.json").read_bytes())


if __name__ == "__main__":
    unittest.main()
