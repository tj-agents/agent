import importlib.util
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
import required_plugins


def load(name):
    spec = importlib.util.spec_from_file_location(name, RUNTIME / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


REFRESH = load("reload_plugins")
IDENTITY = "agent@tj-agents"
DEPENDENCY = "agent-store@tj-agents"


class ClaudeWorktreeInstallations(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(prefix="Agent Claude worktrees ")
        cls.addClassCleanup(cls.temporary.cleanup)
        cls.root = Path(cls.temporary.name).resolve()
        cls.primary = cls.root / "primary"
        cls.primary.mkdir()
        cls.nested = cls.primary / ".worktrees/feature"
        cls.sibling = cls.root / "sibling"
        cls.foreign = cls.primary / "different-repository"
        environment = dict(os.environ, GIT_CONFIG_COUNT="0")
        for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_INDEX_FILE"):
            environment.pop(name, None)
        commands = [
            ["init", "--quiet"],
            ["-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
             "-c", "commit.gpgsign=false", "-c", "core.hooksPath=" + str(cls.root / "no-hooks"),
             "commit", "--quiet", "--allow-empty", "-m", "fixture"],
            ["worktree", "add", "--quiet", "--detach", str(cls.nested), "HEAD"],
            ["worktree", "add", "--quiet", "--detach", str(cls.sibling), "HEAD"],
            ["init", "--quiet", str(cls.foreign)],
        ]
        for arguments in commands:
            subprocess.run(["git", *arguments], cwd=cls.primary, env=environment, check=True,
                           capture_output=True, text=True, timeout=15)
        for project in (cls.primary, cls.nested, cls.sibling):
            settings = project / ".claude/settings.json"
            settings.parent.mkdir()
            settings.write_text(json.dumps({"enabledPlugins": {IDENTITY: True}}), encoding="utf-8")

    def setUp(self):
        environment = mock.patch.dict(os.environ, {"GIT_CONFIG_COUNT": "0", "TJ_AGENTS_SETUP_RECOVERY": "1"})
        environment.start()
        self.addCleanup(environment.stop)
        self.commands = []

    def entry(self, owner, version="old", **extra):
        return {"id": IDENTITY, "scope": "project", "projectPath": str(owner), "version": version,
                "installPath": str(self.root / version), "enabled": True, **extra}

    def native(self, command, cwd):
        self.commands.append((command, Path(cwd)))
        if command[1:3] == ["plugin", "list"]:
            return json.dumps(self.rows)
        if command[1:3] == ["plugin", "install"]:
            self.fail("An existing repository installation must not be installed again per worktree")
        if command[1:3] == ["plugin", "update"]:
            first = next(row for row in self.rows if Path(row["projectPath"]) != self.foreign)
            first["version"] = "new"
            return json.dumps({"updateOutcome": "updated", "newVersion": "new"})
        return ""

    def test_refresh_updates_only_the_first_same_repository_registration(self):
        for first, current in ((self.primary, self.nested), (self.nested, self.primary),
                               (self.sibling, self.nested)):
            with self.subTest(first=first, current=current):
                self.commands = []
                other = self.nested if first != self.nested else self.primary
                self.rows = [self.entry(self.foreign), self.entry(first), self.entry(other)]
                with mock.patch.object(REFRESH, "run", side_effect=self.native), \
                        mock.patch.object(REFRESH, "dependency_installs", return_value=[]):
                    self.assertEqual([IDENTITY], REFRESH.refresh("claude", "claude", current, {}, [IDENTITY]))
                self.assertEqual(["old", "new", "old"], [row["version"] for row in self.rows])
                self.assertEqual([first], [cwd for command, cwd in self.commands if command[2] == "update"])

    def test_broken_winner_does_not_fall_back_to_a_shadowed_registration(self):
        for broken in ({"enabled": False}, {"errors": ["broken hook"]}):
            with self.subTest(broken=broken), mock.patch.object(required_plugins, "readable_payload", return_value=True):
                rows = [self.entry(self.primary, **broken), self.entry(self.nested, "new")]
                self.assertEqual({}, required_plugins.enabled_roots("claude", self.nested, rows))

    def test_local_and_project_scopes_keep_separate_first_registrations(self):
        rows = [self.entry(self.primary), self.entry(self.sibling, "new", scope="local"),
                self.entry(self.nested), self.entry(self.nested, scope="local")]
        self.assertEqual([("local", self.sibling), ("project", self.primary)],
                         REFRESH.update_targets(IDENTITY, self.nested, rows))

    def test_dangling_foreign_worktree_does_not_block_a_healthy_repository(self):
        with tempfile.TemporaryDirectory(dir=self.root) as temporary:
            dangling = Path(temporary)
            (dangling / ".git").write_text("gitdir: " + str(dangling / "deleted-metadata"), encoding="utf-8")
            rows = [self.entry(dangling), self.entry(self.primary, "new"), self.entry(self.nested)]
            self.assertEqual([("project", self.primary)], REFRESH.update_targets(IDENTITY, self.nested, rows))
            with mock.patch.object(required_plugins, "readable_payload", return_value=True):
                self.assertEqual({IDENTITY: self.root / "new"},
                                 required_plugins.enabled_roots("claude", self.nested, rows))
            with self.assertRaises(required_plugins.RequiredPluginsError):
                required_plugins.enabled_roots("claude", dangling, rows)

    def test_dependency_cycle_is_rejected(self):
        dependencies = {IDENTITY: [DEPENDENCY], DEPENDENCY: [IDENTITY]}
        with self.assertRaisesRegex(REFRESH.RefreshError, "Cyclic plugin dependency"):
            REFRESH.missing_dependencies(IDENTITY, "project", self.primary, dependencies, [])


if __name__ == "__main__":
    unittest.main()
