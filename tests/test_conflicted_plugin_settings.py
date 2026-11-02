"""Regression coverage using actual Git stash application conflicts."""

import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import tomllib
import unittest
from unittest import mock

from git_isolation import isolate_git_config


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "plugins/agent/.agents/agent/utility/plugins/scripts"
sys.path.insert(0, str(SCRIPTS))
spec = importlib.util.spec_from_file_location("conflict_adoption", SCRIPTS / "adopt_plugins.py")
ADOPT = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ADOPT)
FILES = (".agents/plugins.json", ".claude/settings.json", ".codex/config.toml")


class ConflictRecoveryTests(unittest.TestCase):
    def setUp(self):
        isolate_git_config()
        temporary = tempfile.TemporaryDirectory(prefix="plugin stash ü ")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        patch = mock.patch.object(ADOPT, "current_roster", side_effect=ADOPT.load_roster)
        patch.start()
        self.addCleanup(patch.stop)
        self.git("init", "--initial-branch=main")
        self.git("config", "user.name", "Fixture")
        self.git("config", "user.email", "fixture@example.invalid")
        self.git("config", "core.hooksPath", "")
        self.git("remote", "add", "origin", "https://github.com/tj-agents/fixture.git")
        ADOPT.run(self.root, [], ["base"], "write")
        self.commit("Base selection")

    def git(self, *arguments, success=True):
        result = subprocess.run(["git", *arguments], cwd=self.root, capture_output=True,
                                text=True, encoding="utf-8", timeout=15)
        if success:
            self.assertEqual(0, result.returncode, result.stderr)
        return result.stdout if success else result

    def commit(self, title):
        self.git("add", ".")
        self.git("commit", "-m", title)

    def stash_conflict(self, user_key=None):
        ADOPT.run(self.root, [], ["python"], "write")
        if user_key:
            self.set_user_key("stashed", user_key)
        self.git("stash", "push", "-m", "Pre-existing plugin configuration")
        ADOPT.run(self.root, [], ["github-actions"], "write")
        if user_key:
            self.set_user_key("upstream", user_key)
        self.commit("Published selection update")
        result = self.git("stash", "apply", success=False)
        self.assertNotEqual(0, result.returncode)
        self.assertIn(".agents/plugins.json", self.git("diff", "--name-only", "--diff-filter=U"))

    def set_user_key(self, value, kind):
        if kind == "json":
            path = self.root / ".claude/settings.json"
            document = json.loads(path.read_text(encoding="utf-8"))
            document["model"] = value
            path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
        else:
            path = self.root / ".codex/config.toml"
            text = path.read_text(encoding="utf-8")
            path.write_text(f'model = "{value}"\n' + text, encoding="utf-8")

    def snapshot(self):
        return {name: (self.root / name).read_bytes() for name in FILES}

    def test_real_stash_conflicts_converge_both_hosts_preserving_user_edits(self):
        self.stash_conflict()
        index = self.git("ls-files", "-u")
        # Edits made after the failed stash application must survive too.
        path = self.root / ".codex/config.toml"
        path.write_text('model = "custom-model"\n' + path.read_text(encoding="utf-8"), encoding="utf-8")
        before = self.snapshot()
        self.assertTrue(ADOPT.reconcile(self.root, "check"))
        self.assertEqual(before, self.snapshot())
        ADOPT.reconcile(self.root, "write")
        selection = json.loads((self.root / FILES[0]).read_text(encoding="utf-8"))["plugins"]
        self.assertIn("python@tj-agents", selection)
        self.assertIn("github-actions@tj-agents", selection)
        claude = json.loads((self.root / FILES[1]).read_text(encoding="utf-8"))
        codex = tomllib.loads((self.root / FILES[2]).read_text(encoding="utf-8"))
        self.assertEqual(set(selection), set(claude["enabledPlugins"]))
        self.assertEqual(set(selection), set(codex["plugins"]))
        self.assertEqual("custom-model", codex["model"])
        self.assertEqual(index, self.git("ls-files", "-u"))
        self.assertEqual([], ADOPT.reconcile(self.root, "write"))

    def test_conflicting_unmanaged_settings_block_the_entire_write(self):
        for kind in ("json", "toml"):
            with self.subTest(kind=kind):
                self.git("reset", "--hard", "HEAD")
                self.stash_conflict(user_key=kind)
                before = self.snapshot()
                with self.assertRaises((ADOPT.lock_io.LockError, ValueError)):
                    ADOPT.reconcile(self.root, "write")
                self.assertEqual(before, self.snapshot())
                self.git("reset", "--hard", "HEAD~1")

    def test_committed_markers_have_no_authority_to_reconstruct_configuration(self):
        self.stash_conflict()
        self.git("add", *FILES)
        self.git("commit", "-m", "Invalid committed conflict markers")
        before = self.snapshot()
        with self.assertRaisesRegex(ADOPT.lock_io.LockError, "no complete Git merge stages"):
            ADOPT.reconcile(self.root, "write")
        self.assertEqual(before, self.snapshot())

    def test_invalid_unmanaged_toml_is_rejected_before_any_write(self):
        self.stash_conflict()
        path = self.root / ".codex/config.toml"
        path.write_text("broken = [\n" + path.read_text(encoding="utf-8"), encoding="utf-8")
        before = self.snapshot()
        with self.assertRaises(ValueError):
            ADOPT.reconcile(self.root, "write")
        self.assertEqual(before, self.snapshot())


if __name__ == "__main__":
    unittest.main()
