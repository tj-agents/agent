import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import tomllib
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / ".agents" / "agent" / "runtime"
sys.path.insert(0, str(RUNTIME))


def load(name):
    spec = importlib.util.spec_from_file_location(name, RUNTIME / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


AUTOUPDATE = load("plugin_autoupdate")
REFRESH = load("reload_plugins")
MARKETPLACE = "https://github.com/tj-agents/marketplace.git"


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


class RetiredPluginTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        patcher = mock.patch.dict(os.environ, {"CODEX_HOME": str(self.root / "codex"),
                                               "CLAUDE_CONFIG_DIR": str(self.root / "claude"),
                                               "AGENT_STATE_DIRECTORY": str(self.root / "state")})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_codex_refresh_removes_a_retired_user_selection_instead_of_retrying_it(self):
        config = self.root / "codex" / "config.toml"
        config.parent.mkdir(parents=True)
        config.write_text(
            'model = "gpt"\n\n[marketplaces.tj-agents]\nsource_type = "git"\nsource = "' + MARKETPLACE + '"\n\n'
            '[plugins."base@tj-agents"]\nenabled = true\n\n[plugins."kit@tj-agents"]\nenabled = true\n\n'
            '[projects.\'c:\\users\\fixture\\repo\']\ntrust_level = "trusted"\n', encoding="utf-8")
        added = []

        def run(command, cwd):
            added.append(command[-1])
            return AUTOUPDATE.RETIRED if command[-1] == "kit@tj-agents" else True

        target = {"tj-agents": {"source": MARKETPLACE, "ref": None, "project": self.root,
                                "plugins": ["base@tj-agents", "kit@tj-agents"]}}
        outcomes = AUTOUPDATE.refresh_codex("codex", target, {}, revision=lambda *_: "b" * 40, run=run,
                                            busy=lambda: False, packages=lambda name: [],
                                            preserve=lambda *_: None, sources=lambda *_: {})
        self.assertTrue(outcomes["tj-agents"].startswith("updated"), outcomes)
        parsed = tomllib.loads(config.read_text(encoding="utf-8"))
        self.assertEqual({"base@tj-agents"}, set(parsed["plugins"]))
        self.assertEqual("gpt", parsed["model"])
        self.assertIn("c:\\users\\fixture\\repo", parsed["projects"])
        targets, _ = AUTOUPDATE.declared_targets(AUTOUPDATE.load_toml(config))
        self.assertEqual(["base@tj-agents"], targets["tj-agents"]["plugins"])

    def claude_marketplace(self, *published):
        location = self.root / "market"
        write(self.root / "claude" / "plugins" / "known_marketplaces.json", {"tj-agents": {
            "source": {"source": "github", "repo": "tj-agents/marketplace"}, "installLocation": str(location)}})
        write(location / ".claude-plugin" / "marketplace.json", {"plugins": [{"name": name} for name in published]})

    def test_claude_user_selections_the_marketplace_no_longer_publishes_are_removed(self):
        self.claude_marketplace("agent", "base", "cli")
        settings = self.root / "claude" / "settings.json"
        write(settings, {"theme": "dark", "enabledPlugins": {
            "agent@tj-agents": True, "kit@tj-agents": True, "machine@tj-agents": True, "concertable@agents": True}})
        self.assertEqual(["kit@tj-agents", "machine@tj-agents"], REFRESH.retire_unpublished_user_plugins())
        value = json.loads(settings.read_text(encoding="utf-8"))
        self.assertEqual({"agent@tj-agents": True, "concertable@agents": True}, value["enabledPlugins"])
        self.assertEqual("dark", value["theme"])
        before = settings.read_bytes()
        self.assertEqual([], REFRESH.retire_unpublished_user_plugins())
        self.assertEqual(before, settings.read_bytes())

    def test_claude_selections_survive_when_the_marketplace_manifest_is_unavailable(self):
        write(self.root / "claude" / "plugins" / "known_marketplaces.json", {"tj-agents": {
            "source": {"source": "github", "repo": "tj-agents/marketplace"}, "installLocation": str(self.root / "missing")}})
        settings = self.root / "claude" / "settings.json"
        write(settings, {"enabledPlugins": {"machine@tj-agents": True}})
        before = settings.read_bytes()
        self.assertEqual([], REFRESH.retire_unpublished_user_plugins())
        self.assertEqual(before, settings.read_bytes())


if __name__ == "__main__":
    unittest.main()
