import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

if sys.version_info < (3, 11):
    raise unittest.SkipTest("Codex configuration parsing requires Python 3.11 or newer")

import tomllib


ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "plugins" / "agent" / ".agents" / "agent" / "runtime"
sys.path.insert(0, str(RUNTIME))


def load(name):
    spec = importlib.util.spec_from_file_location(name, RUNTIME / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


AUTOUPDATE = load("plugin_autoupdate")
from claude import plugins as CLAUDE  # noqa: E402
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

    def refresh_codex(self, listed, recorded=None, added=None):
        config = self.root / "codex" / "config.toml"
        if not config.exists():
            config.parent.mkdir(parents=True)
            config.write_text(
                'model = "gpt"\n\n[marketplaces.tj-agents]\nsource_type = "git"\nsource = "' + MARKETPLACE + '"\n\n'
                '[plugins."base@tj-agents"]\nenabled = true\n\n[plugins."kit@tj-agents"]\nenabled = true\n\n'
                '[projects.\'c:\\users\\fixture\\repo\']\ntrust_level = "trusted"\n', encoding="utf-8")
        added = [] if added is None else added

        def run(command, cwd):
            added.append(command[-1])
            return AUTOUPDATE.RETIRED if command[-1] == "kit@tj-agents" else True

        target = {"tj-agents": {"source": MARKETPLACE, "ref": None, "project": self.root,
                                "plugins": ["base@tj-agents", "kit@tj-agents"], "missing": ["kit@tj-agents"]}}
        sources = {name: (MARKETPLACE, "HEAD", f"plugins/{name}") for name in listed}
        recorded = {} if recorded is None else recorded
        recorded.update({f"codex-plugin:{name}@tj-agents": "b" * 40 for name in listed
                         if f"codex-plugin:{name}@tj-agents" not in recorded})
        outcomes = AUTOUPDATE.refresh_codex("codex", target, recorded, revision=lambda *_: "b" * 40, run=run,
                                            busy=lambda: False, packages=lambda name: [],
                                            preserve=lambda *_: None, sources=lambda *_: sources)
        self.assertTrue(outcomes["tj-agents"].startswith("updated"), outcomes)
        return config

    def test_codex_refresh_removes_a_retired_user_selection_instead_of_retrying_it(self):
        config = self.refresh_codex(["base"])
        parsed = tomllib.loads(config.read_text(encoding="utf-8"))
        self.assertEqual({"base@tj-agents"}, set(parsed["plugins"]))
        self.assertEqual("gpt", parsed["model"])
        self.assertIn("c:\\users\\fixture\\repo", parsed["projects"])
        targets, _ = AUTOUPDATE.declared_targets(AUTOUPDATE.load_toml(config))
        self.assertEqual(["base@tj-agents"], targets["tj-agents"]["plugins"])

    def test_codex_keeps_a_selection_its_refreshed_marketplace_still_lists(self):
        config = self.refresh_codex(["base", "kit"])
        self.assertIn("kit@tj-agents", tomllib.loads(config.read_text(encoding="utf-8"))["plugins"])

    def test_codex_retries_a_still_listed_selection_the_cli_reported_missing(self):
        recorded, added = {}, []
        self.refresh_codex(["base", "kit"], recorded, added)
        self.refresh_codex(["base", "kit"], recorded, added)
        self.assertEqual(2, added.count("kit@tj-agents"))

    def test_codex_keeps_selections_when_the_marketplace_listing_is_unreadable(self):
        config = self.refresh_codex([])
        self.assertIn("kit@tj-agents", tomllib.loads(config.read_text(encoding="utf-8"))["plugins"])

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
        self.assertEqual(["kit@tj-agents", "machine@tj-agents"], CLAUDE.retire_unpublished_user_plugins({"tj-agents"}))
        value = json.loads(settings.read_text(encoding="utf-8"))
        self.assertEqual({"agent@tj-agents": True, "concertable@agents": True}, value["enabledPlugins"])
        self.assertEqual("dark", value["theme"])
        before = settings.read_bytes()
        self.assertEqual([], CLAUDE.retire_unpublished_user_plugins({"tj-agents"}))
        self.assertEqual(before, settings.read_bytes())

    def test_claude_selections_survive_until_their_marketplace_is_refreshed(self):
        self.claude_marketplace("agent")
        settings = self.root / "claude" / "settings.json"
        write(settings, {"enabledPlugins": {"agent@tj-agents": True, "machine@tj-agents": True}})
        before = settings.read_bytes()
        self.assertEqual([], CLAUDE.retire_unpublished_user_plugins(set()))
        self.assertEqual(before, settings.read_bytes())

    def test_claude_selections_survive_when_the_marketplace_manifest_is_unavailable(self):
        write(self.root / "claude" / "plugins" / "known_marketplaces.json", {"tj-agents": {
            "source": {"source": "github", "repo": "tj-agents/marketplace"}, "installLocation": str(self.root / "missing")}})
        settings = self.root / "claude" / "settings.json"
        write(settings, {"enabledPlugins": {"machine@tj-agents": True}})
        before = settings.read_bytes()
        self.assertEqual([], CLAUDE.retire_unpublished_user_plugins({"tj-agents"}))
        self.assertEqual(before, settings.read_bytes())


    def test_claude_installations_the_marketplace_no_longer_publishes_are_uninstalled(self):
        self.claude_marketplace("agent")
        project = self.root / "repo"
        project.mkdir()
        write(self.root / "claude" / "plugins" / "installed_plugins.json", {"plugins": {
            "agent@tj-agents": [{"scope": "user"}],
            "machine@tj-agents": [{"scope": "user"}, {"scope": "project", "projectPath": str(project)},
                                  {"scope": "project", "projectPath": str(self.root / "deleted")}],
            "concertable@agents": [{"scope": "user"}]}})
        commands = []
        with mock.patch.object(CLAUDE.plugin_refresh, "run", lambda command, cwd: commands.append((command, cwd))):
            CLAUDE.uninstall_unpublished("claude", {"tj-agents"})
        self.assertEqual([(["claude", "plugin", "uninstall", "machine@tj-agents", "--scope", "user"], Path.home()),
                          (["claude", "plugin", "uninstall", "machine@tj-agents", "--scope", "project"], project)],
                         commands)

if __name__ == "__main__":
    unittest.main()
