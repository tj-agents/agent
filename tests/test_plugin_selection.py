import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest


SCRIPTS = (Path(__file__).resolve().parents[1] / ".agents" /
           "agent" / "utility" / "plugins" / "scripts")
sys.path.insert(0, str(SCRIPTS))


def load(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


plugin_selection = load("plugin_selection")
repo_config = load("repo_config")
LockError = repo_config.lock_io.LockError


class PluginSelectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.selection = self.root / ".agents" / "plugins.json"
        self.selection.parent.mkdir()
        self.document = {"schema_version": 1, "marketplaces": {"tj-agents": {"repository": "tj-agents/marketplace"}}, "plugins": ["base@tj-agents", "engineering@tj-agents", "machine@tj-agents", "dotnet@tj-agents"]}
        self.selection.write_text(json.dumps(self.document), encoding="utf-8")

    def run_mode(self, mode, scope="project"):
        return repo_config.run(self.selection, mode, root=self.root, scope=scope)

    def test_generates_rolling_main_settings_without_a_revision(self):
        self.assertEqual([".claude/settings.json", ".codex/config.toml"], self.run_mode("check"))
        self.run_mode("write")
        self.assertEqual([], self.run_mode("check"))
        claude = json.loads((self.root / ".claude" / "settings.json").read_text(encoding="utf-8"))
        self.assertEqual({"tj-agents": {"source": {"source": "github", "repo": "tj-agents/marketplace"}, "autoUpdate": True}}, claude["extraKnownMarketplaces"])
        codex = (self.root / ".codex" / "config.toml").read_text(encoding="utf-8")
        self.assertIn('source = "https://github.com/tj-agents/marketplace.git"', codex)
        self.assertNotIn("ref =", codex)
        self.assertIn('sparse_paths = [".agents/plugins", ".claude-plugin", "plugins"]', codex)

    def test_selection_allows_only_effective_packages(self):
        self.document["plugins"] = ["dotnet@tj-agents"]
        self.selection.write_text(json.dumps(self.document), encoding="utf-8")
        self.run_mode("write")
        claude = json.loads((self.root / ".claude" / "settings.json").read_text(encoding="utf-8"))
        self.assertEqual({"dotnet@tj-agents": True}, claude["enabledPlugins"])

    def test_invalid_selection_and_legacy_pin_are_rejected_by_settings_generation(self):
        self.document["marketplaces"]["tj-agents"]["revision"] = "v1.2.3"
        with self.assertRaises(LockError):
            plugin_selection.validate(self.document)
        legacy = {"schema_version": 2, "marketplace": {"repository": "tj-agents/marketplace", "revision": "v1.2.3"}, "plugins": ["base"]}
        with self.assertRaises(LockError):
            plugin_selection.validate(legacy)

    def test_marketplace_identity_validation_prevents_ambiguous_or_unowned_sources(self):
        invalid = (
            ({"agents": {"repository": "Other/agents"}}, ["project@agents"]),
            ({"agents": {"repository": "Concertable/agents.git"}}, ["project@agents"]),
            ({"agents": {"repository": "Concertable/agents"}, "alias": {"repository": "Concertable/Agents"}},
             ["project@agents", "project@alias"]),
            ({"agents": {"repository": "Concertable/agents"}}, ["project@missing"]),
            ({"agents": {"repository": "Concertable/agents"}}, ["project@agents", "project@agents"]),
            ({"agents": {"repository": "Concertable/agents"}}, ["project"]),
            ({"alias": {"repository": "tj-agents/marketplace"}}, ["base@alias"]),
            ({"alias": {"repository": "tj-agents/Marketplace"}}, ["base@alias"]),
            ({"tj-agents": {"repository": "tj-agents/Marketplace"}}, ["base@tj-agents"]),
            ({"alias": {"repository": "tj-agents/core"}}, ["base@alias"]),
            ({"core": {"repository": "tj-agents/core"}}, ["base@core"]),
        )
        for marketplaces, identities in invalid:
            with self.subTest(marketplaces=marketplaces, identities=identities), self.assertRaises(LockError):
                plugin_selection.validate({"schema_version": 1, "marketplaces": marketplaces, "plugins": identities})

    def test_local_scope_writes_local_settings(self):
        self.run_mode("write", "local")
        claude = json.loads((self.root / ".claude" / "settings.local.json").read_text(encoding="utf-8"))
        self.assertTrue(claude["extraKnownMarketplaces"]["tj-agents"]["autoUpdate"])
        self.assertEqual([], self.run_mode("check", "local"))

    def test_producer_aliases_cannot_be_newly_declared_or_generated(self):
        adopt_plugins = load("adopt_plugins")
        for name in ("python", "react"):
            for marketplace in (f"{name}-agents", "producer-alias"):
                repository = f"tj-agents/{name}"
                with self.subTest(name=name, marketplace=marketplace):
                    document = {"schema_version": 1,
                                "marketplaces": {marketplace: {"repository": repository}},
                                "plugins": [f"{name}@{marketplace}"]}
                    self.selection.write_text(json.dumps(document), encoding="utf-8")
                    for mode in ("preview", "check", "write"):
                        with self.assertRaisesRegex(LockError, "published through the tj-agents marketplace"):
                            self.run_mode(mode)
                        with self.assertRaisesRegex(LockError, "published through the tj-agents marketplace"):
                            adopt_plugins.run(self.root, [], [], mode, marketplaces=[f"{marketplace}={repository}"])
                    with self.assertRaisesRegex(LockError, "published through the tj-agents marketplace"):
                        adopt_plugins.run(self.root, [], [], "check")
                    with self.assertRaisesRegex(LockError, "published through the tj-agents marketplace"):
                        adopt_plugins.parse_marketplace(f"{marketplace}={repository}")
                    self.assertFalse((self.root / ".claude/settings.json").exists())
                    self.assertFalse((self.root / ".codex/config.toml").exists())

    def test_resolve_names_the_selection_in_agents(self):
        self.assertEqual(self.selection, plugin_selection.resolve(self.root))


if __name__ == "__main__":
    unittest.main()
