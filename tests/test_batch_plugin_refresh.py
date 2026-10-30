import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

from git_isolation import isolate_git_config


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugins/agent/.agents/agent/runtime"))
from claude import plugins as CLAUDE
from codex import plugins as CODEX
import launch_check
import plugin_autoupdate
import required_plugins
from tj_agent.plugins import sources


IDENTITY = "python@tj-agents"
TREE = {".claude-plugin/plugin.json": b'{"name":"python","version":"2.0.0"}', "skill.txt": b"current"}


class BatchPluginRefreshTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="plugin refresh with spaces ")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.project = self.root / "project"
        self.project.mkdir()
        self.other = self.root / "other"
        self.other.mkdir()
        self.sibling = self.root / "sibling"
        self.sibling.mkdir()
        self.config = self.root / "claude"
        self.codex = self.root / "codex"
        self.state = self.root / "state"
        environment = mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(self.config),
            "CODEX_HOME": str(self.codex), "AGENT_STATE_DIRECTORY": str(self.state)})
        environment.start()
        self.addCleanup(environment.stop)
        isolate_git_config()
        self.registry = self.config / "plugins/installed_plugins.json"
        self.registry.parent.mkdir(parents=True)
        self.market = self.root / "market"
        (self.market / ".claude-plugin").mkdir(parents=True)
        self.catalog = {"plugins": [{"name": "python", "source": "./python"}]}
        (self.market / CLAUDE.CATALOG).write_text(json.dumps(self.catalog), encoding="utf-8")
        for project in (self.project, self.sibling):
            (project / ".claude").mkdir()
            (project / ".claude/settings.json").write_text(json.dumps({"enabledPlugins": {IDENTITY: True}}), encoding="utf-8")
        self.source = mock.patch.object(sources, "tree", return_value=TREE)
        self.fetch = self.source.start()
        self.addCleanup(self.source.stop)
        marketplaces = mock.patch.object(CLAUDE, "tj_agents_marketplaces", return_value={"tj-agents": ("tj-agents/marketplace", self.market)})
        marketplaces.start()
        self.addCleanup(marketplaces.stop)
        selection = mock.patch.object(CLAUDE, "selected", side_effect=lambda project: (
            {"tj-agents": "tj-agents/marketplace"}, [] if project == self.other else [IDENTITY]))
        selection.start()
        self.addCleanup(selection.stop)
        common = mock.patch.object(CLAUDE, "git_common_directory", side_effect=lambda project:
            str(self.other / ".git") if project == self.other else str(self.project / ".git"))
        common.start()
        self.addCleanup(common.stop)
        repositories = mock.patch.object(CLAUDE, "repository_projects", side_effect=list)
        repositories.start()
        self.addCleanup(repositories.stop)

    def row(self, project, scope="project"):
        return {"scope": scope, "projectPath": str(project), "installPath": str(self.root / "old"), "version": "1.0.0"}

    def write(self, rows):
        self.registry.write_text(json.dumps({"version": 2, "plugins": {IDENTITY: rows}}), encoding="utf-8")

    def test_one_repository_record_updates_every_worktree_and_preserves_other_repositories(self):
        foreign = self.row(self.other)
        self.write([self.row(self.project), self.row(self.sibling), foreign, self.row(self.root / "deleted")])
        with mock.patch.object(plugin_autoupdate, "write_json", wraps=plugin_autoupdate.write_json) as write:
            self.assertEqual([IDENTITY], CLAUDE.synchronize([self.project, self.sibling]))
        rows = json.loads(self.registry.read_text(encoding="utf-8"))["plugins"][IDENTITY]
        self.assertEqual(2, len(rows))
        self.assertIn(foreign, rows)
        updated = next(row for row in rows if row != foreign)
        self.assertEqual("project", updated["scope"])
        self.assertEqual(TREE, CLAUDE.plugin_refresh.package_tree(Path(updated["installPath"])))
        self.fetch.assert_called_once()
        write.assert_called_once()
        self.fetch.reset_mock()
        self.assertEqual([], CLAUDE.synchronize([self.project, self.sibling]))
        self.fetch.assert_called_once()

    def test_local_source_changes_at_the_same_path_refresh_without_a_new_catalog(self):
        self.write([self.row(self.project)])
        CLAUDE.synchronize([self.project])
        updated = {**TREE, "skill.txt": b"new published content"}
        self.fetch.return_value = updated
        self.assertEqual([IDENTITY], CLAUDE.synchronize([self.project]))
        row = json.loads(self.registry.read_text(encoding="utf-8"))["plugins"][IDENTITY][0]
        self.assertEqual(updated, CLAUDE.plugin_refresh.package_tree(Path(row["installPath"])))

    def test_a_fetched_remote_head_is_remembered_for_offline_launches(self):
        source = {"source": "git-subdir", "url": "https://github.com/tj-agents/python.git",
                  "ref": "main", "path": "plugins/python"}
        self.catalog["plugins"][0]["source"] = source
        (self.market / CLAUDE.CATALOG).write_text(json.dumps(self.catalog))
        self.write([self.row(self.project)])
        with mock.patch.object(sources, "resolved_head", return_value="b" * 40):
            CLAUDE.synchronize([self.project])
        self.fetch.reset_mock()
        self.fetch.side_effect = OSError("offline")
        self.assertEqual([], CLAUDE.synchronize([self.project]))
        self.fetch.assert_not_called()

    def test_all_repository_groups_keep_the_shared_payload_modes(self):
        (self.other / ".claude").mkdir()
        (self.other / ".claude/settings.json").write_text(json.dumps({"enabledPlugins": {IDENTITY: True}}))
        self.write([self.row(self.project), self.row(self.other)])
        source = {"source": "git-subdir", "url": "https://github.com/tj-agents/python.git",
                  "ref": "main", "path": "plugins/python"}
        self.catalog["plugins"][0]["source"] = source
        (self.market / CLAUDE.CATALOG).write_text(json.dumps(self.catalog))
        plugin_autoupdate.record_contents(heads={"tj-agents/python@main": "b" * 40})
        def fetch(*args, **kwargs):
            kwargs["modes"].update({name: 0o666 for name in TREE})
            return TREE
        self.fetch.side_effect = fetch
        with mock.patch.object(CLAUDE, "selected", return_value=({"tj-agents": "tj-agents/marketplace"}, [IDENTITY])):
            CLAUDE.synchronize([self.project, self.other])
        rows = json.loads(self.registry.read_text())["plugins"][IDENTITY]
        self.assertEqual(2, len(rows))
        self.assertTrue(all(row["tjAgentsModes"] == {name: 0o666 for name in TREE} for row in rows))
        self.fetch.assert_called_once()

    def test_local_scope_remains_local(self):
        (self.project / ".claude/settings.json").write_text("{}", encoding="utf-8")
        (self.project / ".claude/settings.local.json").write_text(json.dumps({"enabledPlugins": {IDENTITY: True}}), encoding="utf-8")
        self.write([self.row(self.project, "local"), self.row(self.sibling, "local")])
        self.assertEqual([IDENTITY], CLAUDE.synchronize([self.project]))
        rows = json.loads(self.registry.read_text(encoding="utf-8"))["plugins"][IDENTITY]
        self.assertEqual(["local"], [row["scope"] for row in rows])

    def test_new_dependencies_are_materialized_once_at_the_declared_scope(self):
        self.catalog["plugins"][0]["dependencies"] = ["base"]
        self.catalog["plugins"].append({"name": "base", "source": "./base"})
        (self.market / CLAUDE.CATALOG).write_text(json.dumps(self.catalog), encoding="utf-8")
        self.write([self.row(self.project)])
        def fetch(source, *args, **kwargs):
            name = source.removeprefix("./")
            return {".claude-plugin/plugin.json": json.dumps({"name": name, "version": "2.0.0"}).encode()}
        self.fetch.side_effect = fetch
        self.assertEqual(["base@tj-agents", IDENTITY], CLAUDE.synchronize([self.project, self.sibling]))
        plugins = json.loads(self.registry.read_text(encoding="utf-8"))["plugins"]
        self.assertEqual({IDENTITY, "base@tj-agents"}, set(plugins))
        self.assertTrue(all(row["scope"] == "project" for rows in plugins.values() for row in rows))
        self.assertEqual(2, self.fetch.call_count)
        for project in (self.project, self.sibling):
            self.assertTrue(json.loads((project / ".claude/settings.json").read_text())["enabledPlugins"]["base@tj-agents"])
        self.assertFalse((self.config / "settings.json").exists())

    def test_a_new_project_installs_the_selected_plugin_without_a_publish_event(self):
        self.write([])
        self.assertEqual([IDENTITY], CLAUDE.synchronize([self.project]))
        rows = json.loads(self.registry.read_text(encoding="utf-8"))["plugins"][IDENTITY]
        self.assertEqual(["project"], [row["scope"] for row in rows])
        self.assertEqual("2.0.0", rows[0]["version"])

    def test_a_registry_containing_only_deleted_repositories_is_pruned(self):
        self.write([self.row(self.root / "deleted")])
        CLAUDE.update_registered()
        self.assertEqual({}, json.loads(self.registry.read_text(encoding="utf-8"))["plugins"])

    @unittest.skipIf(os.name == "nt", "Unix executable permission")
    def test_materialization_preserves_executable_permissions(self):
        destination = CLAUDE.materialize(self.root / "cache", {"run.sh": b"#!/bin/sh\nexit 0\n"}, {"run.sh": 0o755})
        self.assertEqual(0o755, (destination / "run.sh").stat().st_mode & 0o777)

    @unittest.skipIf(os.name == "nt", "Unix executable permission")
    def test_local_source_mode_changes_and_installed_mode_corruption_are_repaired(self):
        self.write([self.row(self.project)])
        mode = 0o644
        def fetch(*args, **kwargs):
            kwargs["modes"].update({name: mode for name in TREE})
            return TREE
        self.fetch.side_effect = fetch
        CLAUDE.synchronize([self.project])
        mode = 0o755
        self.assertEqual([IDENTITY], CLAUDE.synchronize([self.project]))
        row = json.loads(self.registry.read_text())["plugins"][IDENTITY][0]
        script = Path(row["installPath"]) / "skill.txt"
        self.assertEqual(mode, script.stat().st_mode & 0o777)
        script.chmod(0o644)
        self.assertEqual([IDENTITY], CLAUDE.synchronize([self.project]))

    def test_corrupt_bytes_at_a_recorded_head_are_repaired_without_mutating_the_loaded_cache(self):
        self.write([self.row(self.project)])
        CLAUDE.synchronize([self.project])
        first = json.loads(self.registry.read_text(encoding="utf-8"))["plugins"][IDENTITY][0]
        loaded = Path(first["installPath"])
        (loaded / "skill.txt").write_bytes(b"corrupt")
        self.assertEqual([IDENTITY], CLAUDE.synchronize([self.project]))
        repaired = json.loads(self.registry.read_text(encoding="utf-8"))["plugins"][IDENTITY][0]
        self.assertNotEqual(first["installPath"], repaired["installPath"])
        self.assertEqual(b"corrupt", (loaded / "skill.txt").read_bytes())
        self.assertEqual(TREE, CLAUDE.plugin_refresh.package_tree(Path(repaired["installPath"])))

    def test_failed_fetch_preserves_all_registration_bytes(self):
        self.write([self.row(self.project), self.row(self.sibling)])
        original = self.registry.read_bytes()
        self.fetch.side_effect = RuntimeError("publisher offline")
        with self.assertRaisesRegex(RuntimeError, "publisher offline"):
            CLAUDE.synchronize([self.project])
        self.assertEqual(original, self.registry.read_bytes())

    def test_dependency_cycle_never_changes_registration_bytes(self):
        self.catalog["plugins"][0]["dependencies"] = ["base"]
        self.catalog["plugins"].append({"name": "base", "source": "./base", "dependencies": ["python"]})
        (self.market / CLAUDE.CATALOG).write_text(json.dumps(self.catalog), encoding="utf-8")
        self.write([self.row(self.project)])
        original = self.registry.read_bytes()
        with self.assertRaisesRegex(RuntimeError, "Cyclic plugin dependency"):
            CLAUDE.synchronize([self.project])
        self.assertEqual(original, self.registry.read_bytes())

    def test_native_concurrent_registry_write_is_preserved(self):
        self.write([self.row(self.project)])
        changed = {"version": 2, "plugins": {"other@external": [{"scope": "user"}]}}
        def fetch(*args, **kwargs):
            self.registry.write_text(json.dumps(changed), encoding="utf-8")
            return TREE
        self.fetch.side_effect = fetch
        with self.assertRaisesRegex(RuntimeError, "during refresh"):
            CLAUDE.synchronize([self.project])
        self.assertEqual(changed, json.loads(self.registry.read_text(encoding="utf-8")))


class CodexStaleBytesTests(unittest.TestCase):
    def test_a_fetched_remote_head_is_remembered_for_offline_reconciliation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            installed = root / "local"
            installed.mkdir()
            tree = {"skill.txt": b"published"}
            (installed / "skill.txt").write_bytes(tree["skill.txt"])
            source = {"source": "git-subdir", "url": "https://github.com/tj-agents/python.git",
                      "ref": "main", "path": "plugins/python"}
            with mock.patch.dict(os.environ, {"AGENT_STATE_DIRECTORY": str(root / "state")}), \
                    mock.patch.object(CODEX, "selection", return_value=({"tj-agents": "tj-agents/marketplace"}, [IDENTITY])), \
                    mock.patch.object(plugin_autoupdate, "marketplace_manifest", return_value={"plugins": [{"name": "python", "source": source}]}), \
                    mock.patch.object(required_plugins, "inventory_roots", return_value={IDENTITY: installed}) as inventory, \
                    mock.patch.object(sources, "tree", return_value=tree) as fetch, \
                    mock.patch.object(sources, "resolved_head", return_value="b" * 40):
                self.assertEqual([], CODEX.reconcile(root, executable="codex"))
                inventory.assert_called_once_with(root, CODEX.HOST, "codex", identities=[IDENTITY])
                fetch.reset_mock()
                fetch.side_effect = OSError("offline")
                self.assertEqual([], CODEX.reconcile(root, executable="codex"))
                fetch.assert_not_called()
    def test_launch_repairs_python_bytes_even_when_published_revision_was_recorded_current(self):
        with tempfile.TemporaryDirectory(prefix="codex stale bytes ") as temporary:
            root = Path(temporary)
            installed = root / "plugins/cache/tj-agents/python/local"
            installed.mkdir(parents=True)
            (installed / "skill.txt").write_bytes(b"old")
            tree = {"skill.txt": b"published"}
            def repair(executable, project, identities):
                self.assertEqual([IDENTITY], identities)
                (installed / "skill.txt").write_bytes(b"published")
            state = root / "state"
            state.mkdir()
            plugin_autoupdate.write_json(state / "machine-events.json", {"topic": "t" * 40})
            plugin_autoupdate.write_json(state / "plugin-autoupdate.json", {
                "revisions": {"codex:tj-agents": "a" * 40, "codex-plugin:" + IDENTITY: "b" * 40}})
            envelope = {"event": "message", "id": "published", "message": json.dumps({"v": 1, "repo": "tj-agents/python"})}
            with mock.patch.dict(os.environ, {"CODEX_HOME": str(root), "AGENT_STATE_DIRECTORY": str(root / "state")}), \
                    mock.patch.object(CODEX, "resolve_executable", return_value="codex"), \
                    mock.patch.object(CODEX, "selection", return_value=({"tj-agents": "tj-agents/marketplace"}, [IDENTITY])), \
                    mock.patch.object(plugin_autoupdate, "marketplace_manifest", return_value={"plugins": [{"name": "python", "source": {
                        "source": "git-subdir", "url": "https://github.com/tj-agents/python.git", "ref": "main", "path": "plugins/python"}}]}), \
                    mock.patch.object(plugin_autoupdate, "read_contents", return_value={"heads": {"tj-agents/python@main": "b" * 40}}), \
                    mock.patch.object(required_plugins, "inventory_roots", return_value={IDENTITY: installed}), \
                    mock.patch.object(sources, "tree", return_value=tree) as fetch, \
                    mock.patch.object(plugin_autoupdate, "main", return_value=0) as memoized_update, \
                    mock.patch.object(CODEX, "install_missing", side_effect=repair) as update:
                self.assertEqual("updated", launch_check.check(root, hosts=(CODEX.HOST,),
                    fetch=lambda _: json.dumps(envelope).encode(), recover=False))
                self.assertEqual(b"published", (installed / "skill.txt").read_bytes())
                self.assertEqual("current", launch_check.settle(root, (CODEX.HOST,)))
                update.assert_called_once()
                fetch.assert_called_once()
                memoized_update.assert_called_once()

    def test_local_publisher_changes_at_the_same_path_invalidate_the_byte_receipt(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            installed = root / "local"
            installed.mkdir()
            (installed / "skill.txt").write_bytes(b"old")
            tree = {"skill.txt": b"old"}
            def repair(*args):
                (installed / "skill.txt").write_bytes(tree["skill.txt"])
            with mock.patch.dict(os.environ, {"AGENT_STATE_DIRECTORY": str(root / "state")}), \
                    mock.patch.object(CODEX, "selection", return_value=({"tj-agents": "tj-agents/marketplace"}, [IDENTITY])), \
                    mock.patch.object(plugin_autoupdate, "marketplace_manifest", return_value={"plugins": [{"name": "python", "source": "./python"}]}), \
                    mock.patch.object(required_plugins, "inventory_roots", return_value={IDENTITY: installed}), \
                    mock.patch.object(sources, "tree", side_effect=lambda *args: dict(tree)), \
                    mock.patch.object(CODEX, "install_missing", side_effect=repair) as update:
                self.assertEqual([], CODEX.reconcile(root, executable="codex"))
                tree["skill.txt"] = b"new"
                self.assertEqual([IDENTITY], CODEX.reconcile(root, executable="codex"))
                update.assert_called_once()

    def test_explicit_refresh_resolves_a_new_head_before_verifying_native_bytes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            installed = root / "local"
            installed.mkdir()
            (installed / "skill.txt").write_bytes(b"new published")
            source = {"source": "git-subdir", "url": "https://github.com/tj-agents/python.git",
                      "ref": "main", "path": "plugins/python"}
            key = "tj-agents/python@main"
            def heads(*args, **kwargs):
                plugin_autoupdate.record_contents(heads={key: "b" * 40})
                return {key: "b" * 40}
            with mock.patch.dict(os.environ, {"AGENT_STATE_DIRECTORY": str(root / "state")}), \
                    mock.patch.object(CODEX, "selection", return_value=({"tj-agents": "tj-agents/marketplace"}, [IDENTITY])), \
                    mock.patch.object(plugin_autoupdate, "marketplace_manifest", return_value={"plugins": [{"name": "python", "source": source}]}), \
                    mock.patch.object(plugin_autoupdate, "refresh_contents", side_effect=heads), \
                    mock.patch.object(required_plugins, "inventory_roots", return_value={IDENTITY: installed}), \
                    mock.patch.object(sources, "tree", return_value={"skill.txt": b"new published"}) as fetch, \
                    mock.patch.object(CODEX, "install_missing") as update:
                plugin_autoupdate.record_contents(heads={key: "a" * 40})
                CODEX.refresh_plugins("codex", root, {"tj-agents": "tj-agents/marketplace"}, [IDENTITY])
                self.assertEqual("b" * 40, fetch.call_args.args[0]["sha"])
                update.assert_called_once()

    def test_failed_native_update_cannot_receive_a_current_byte_receipt(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            installed = root / "local"
            installed.mkdir()
            (installed / "skill.txt").write_bytes(b"old")
            with mock.patch.dict(os.environ, {"AGENT_STATE_DIRECTORY": str(root / "state")}), \
                    mock.patch.object(CODEX, "selection", return_value=({"tj-agents": "tj-agents/marketplace"}, [IDENTITY])), \
                    mock.patch.object(plugin_autoupdate, "marketplace_manifest", return_value={"plugins": [{"name": "python", "source": "./python"}]}), \
                    mock.patch.object(required_plugins, "inventory_roots", return_value={IDENTITY: installed}), \
                    mock.patch.object(sources, "tree", return_value={"skill.txt": b"published"}), \
                    mock.patch.object(CODEX, "install_missing"):
                with self.assertRaisesRegex(RuntimeError, "bytes differ from marketplace after update"):
                    CODEX.reconcile(root, executable="codex")
                self.assertFalse((root / "state/codex-plugin-bytes.json").exists())


if __name__ == "__main__":
    unittest.main()
