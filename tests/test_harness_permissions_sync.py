import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "plugins/agent/.agents/agent/utility/plugins/scripts/harness_permissions_sync.py"
sys.path.insert(0, str(SCRIPT.parent))
SPEC = importlib.util.spec_from_file_location("harness_permissions_sync", SCRIPT)
SYNC = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SYNC)

RULE = {
    "pattern": [["python"], "-B", "${PLUGIN_ROOT}/run.py"],
    "justification": "test",
    "match": [["python", "-B", "${PLUGIN_ROOT}/run.py"]],
    "not_match": [["python", "${PLUGIN_ROOT}/run.py"]],
}
OLD_RULE = {
    "pattern": [["python"], "-B", "${PLUGIN_ROOT}/old.py"],
    "justification": "test",
    "match": [["python", "-B", "${PLUGIN_ROOT}/old.py"]],
    "not_match": [["python", "${PLUGIN_ROOT}/old.py"]],
}
DEAD_RULE = "Bash(python -B C:/Users/someone/.claude/plugins/cache/base-agents/engineering/0123/hooks/gate.py --clear:*)"


def harness(root: Path, permissions: dict) -> None:
    root.mkdir(parents=True, exist_ok=True)
    value = {"schema_version": 1, "plugin": "tj-agents/test", "source_roots": [],
             "requires": {"marketplaces": [], "plugins": [], "hooks": [], "permissions": permissions}}
    (root / "harness.json").write_text(json.dumps(value), encoding="utf-8")


def snapshot(root: Path) -> dict[str, bytes]:
    return {path.relative_to(root).as_posix(): path.read_bytes() for path in sorted(root.rglob("*")) if path.is_file()}


class HarnessPermissionsSyncTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="harness sync ")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.claude = self.root / "claude"
        self.codex = self.root / "codex"
        self.state = self.root / "state"
        self.environ = {"CLAUDE_CONFIG_DIR": str(self.claude), "CODEX_HOME": str(self.codex),
                        "AGENT_STATE_DIRECTORY": str(self.state)}
        self.settings = self.claude / "settings.json"
        self.rules = self.codex / "rules" / "tj-agents.rules"

    def install_claude(self, permissions, repository="tj-agents/marketplace", identity="engineering@tj-agents"):
        root = self.root / "claude-cache" / identity.replace("@", "-")
        harness(root, permissions)
        plugins = self.claude / "plugins"
        plugins.mkdir(parents=True, exist_ok=True)
        marketplace = identity.rsplit("@", 1)[1]
        (plugins / "installed_plugins.json").write_text(json.dumps(
            {"plugins": {identity: [{"installPath": str(root)}]}}), encoding="utf-8")
        (plugins / "known_marketplaces.json").write_text(json.dumps(
            {marketplace: {"source": {"source": "github", "repo": repository}}}), encoding="utf-8")
        return root

    def install_claude_versions(self, entries, repository="tj-agents/marketplace", identity="engineering@tj-agents"):
        plugins = self.claude / "plugins"
        plugins.mkdir(parents=True, exist_ok=True)
        marketplace = identity.rsplit("@", 1)[1]
        (plugins / "installed_plugins.json").write_text(json.dumps({"plugins": {identity: entries}}), encoding="utf-8")
        (plugins / "known_marketplaces.json").write_text(json.dumps(
            {marketplace: {"source": {"source": "github", "repo": repository}}}), encoding="utf-8")

    def uninstall_claude(self):
        (self.claude / "plugins" / "installed_plugins.json").write_text(json.dumps({"plugins": {}}), encoding="utf-8")

    def install_codex(self, permissions):
        root = self.codex / "plugins" / "cache" / "tj-agents" / "engineering" / "local"
        harness(root, permissions)
        (self.codex / "config.toml").write_text(
            '[marketplaces.tj-agents]\nsource_type = "git"\nsource = "https://github.com/tj-agents/marketplace.git"\n',
            encoding="utf-8")
        return root

    def install_codex_version(self, version, permissions, mtime):
        root = self.codex / "plugins" / "cache" / "tj-agents" / "engineering" / version
        harness(root, permissions)
        os.utime(root, (mtime, mtime))
        (self.codex / "config.toml").write_text(
            '[marketplaces.tj-agents]\nsource_type = "git"\nsource = "https://github.com/tj-agents/marketplace.git"\n',
            encoding="utf-8")
        return root

    def write_settings(self, allow):
        self.settings.parent.mkdir(parents=True, exist_ok=True)
        self.settings.write_text(json.dumps({"model": "keep", "permissions": {"allow": allow}}), encoding="utf-8")

    def allow(self):
        return json.loads(self.settings.read_text(encoding="utf-8"))["permissions"]["allow"]

    def apply(self):
        return SYNC.synchronize(self.environ, "apply")

    def assert_converged(self):
        before = snapshot(self.root)
        changes, _ = SYNC.synchronize(self.environ, "check")
        self.assertEqual([], changes)
        self.apply()
        self.assertEqual(before, snapshot(self.root))

    def test_removes_stale_owned_claude_grants_and_preserves_user_grants(self):
        self.install_claude({"claude_allow": ["Bash(shipped)"], "codex_prefix_rules": []})
        self.write_settings(["Bash(personal)"])
        self.apply()
        self.assertEqual(["Bash(personal)", "Bash(shipped)"], self.allow())
        self.assert_converged()
        self.uninstall_claude()
        self.apply()
        self.assertEqual(["Bash(personal)"], self.allow())
        self.assertEqual("keep", json.loads(self.settings.read_text(encoding="utf-8"))["model"])
        self.assert_converged()

    def test_only_the_current_claude_plugin_version_is_granted_and_stale_versions_are_pruned(self):
        old_root = self.root / "claude-cache" / "engineering-old"
        harness(old_root, {"claude_allow": ["Bash(old)"], "codex_prefix_rules": []})
        self.install_claude_versions([{"installPath": str(old_root), "lastUpdated": "2026-01-01T00:00:00Z"}])
        self.write_settings([])
        self.apply()
        self.assertEqual(["Bash(old)"], self.allow())

        current_root = self.root / "claude-cache" / "engineering-current"
        harness(current_root, {"claude_allow": ["Bash(current)"], "codex_prefix_rules": []})
        self.install_claude_versions([
            {"installPath": str(old_root), "lastUpdated": "2026-01-01T00:00:00Z"},
            {"installPath": str(current_root), "lastUpdated": "2026-02-01T00:00:00Z"},
        ])
        self.apply()
        self.assertEqual(["Bash(current)"], self.allow())
        self.assert_converged()

    def test_only_the_current_codex_plugin_version_is_rendered_and_stale_rules_are_pruned(self):
        self.install_codex_version("old-version", {"claude_allow": [], "codex_prefix_rules": [OLD_RULE]}, mtime=1000)
        self.apply()
        text = self.rules.read_text(encoding="utf-8")
        self.assertIn("old.py", text)
        self.assertNotIn("run.py", text)

        self.install_codex_version("new-version", {"claude_allow": [], "codex_prefix_rules": [RULE]}, mtime=2000)
        self.apply()
        text = self.rules.read_text(encoding="utf-8")
        self.assertIn("run.py", text)
        self.assertNotIn("old.py", text)
        self.assert_converged()

    def test_dead_owned_rules_for_retired_cache_roots_are_removed(self):
        self.install_claude({"claude_allow": ["Bash(shipped)"], "codex_prefix_rules": []})
        self.write_settings(["Bash(personal)", DEAD_RULE])
        self.state.mkdir()
        (self.state / "harness-permissions.json").write_text(json.dumps(
            {"schema_version": 2, "claude": {str(self.settings): [DEAD_RULE]}, "codex": {}}), encoding="utf-8")
        changes, _ = SYNC.synchronize(self.environ, "check")
        self.assertTrue(changes)
        self.assertIn(DEAD_RULE, self.allow())
        self.apply()
        self.assertEqual(["Bash(personal)", "Bash(shipped)"], self.allow())
        sidecar = json.loads((self.state / "harness-permissions.json").read_text(encoding="utf-8"))
        self.assertEqual(["Bash(shipped)"], sidecar["claude"][str(self.settings)])
        self.assert_converged()

    def test_a_user_rule_equal_to_a_declared_rule_is_never_claimed(self):
        self.install_claude({"claude_allow": ["Bash(shipped)"], "codex_prefix_rules": []})
        self.write_settings(["Bash(shipped)"])
        self.apply()
        self.uninstall_claude()
        self.apply()
        self.assertEqual(["Bash(shipped)"], self.allow())

    def test_untrusted_marketplace_roots_are_ignored(self):
        self.install_claude({"claude_allow": ["Bash(foreign)"], "codex_prefix_rules": []}, repository="someone/marketplace",
                            identity="foreign@other")
        self.write_settings([])
        self.apply()
        self.assertEqual([], self.allow())

    def test_codex_rules_are_written_with_a_header_and_personal_rules_are_untouched(self):
        self.install_codex({"claude_allow": [], "codex_prefix_rules": [RULE]})
        personal = self.codex / "rules" / "personal.rules"
        personal.parent.mkdir(parents=True)
        personal.write_text("prefix_rule()\n", encoding="utf-8")
        self.apply()
        self.assertTrue(personal.exists())
        text = self.rules.read_text(encoding="utf-8")
        self.assertTrue(text.startswith(SYNC.CODEX_RULES_HEADER))
        self.assertIn("run.py", text)
        self.assert_converged()

    def test_unowned_codex_rules_are_never_replaced(self):
        self.install_codex({"claude_allow": [], "codex_prefix_rules": [RULE]})
        self.rules.parent.mkdir(parents=True)
        self.rules.write_text("personal", encoding="utf-8")
        with self.assertRaises(SYNC.PermissionSyncError):
            self.apply()
        self.assertEqual("personal", self.rules.read_text(encoding="utf-8"))

    def test_owned_codex_rules_are_removed_when_no_longer_declared(self):
        self.install_codex({"claude_allow": [], "codex_prefix_rules": [RULE]})
        self.apply()
        self.install_codex({"claude_allow": [], "codex_prefix_rules": []})
        self.apply()
        self.assertFalse(self.rules.exists())
        self.assert_converged()

    def test_a_failing_sidecar_write_rolls_back_the_settings_and_rules_writes(self):
        self.install_claude({"claude_allow": ["Bash(shipped)"], "codex_prefix_rules": []})
        self.install_codex({"claude_allow": [], "codex_prefix_rules": [RULE]})
        self.write_settings(["Bash(personal)"])
        before = snapshot(self.root)
        real_write = SYNC.write_text_atomic

        def failing_write(path, text):
            if path.name == "harness-permissions.json":
                raise OSError("disk full")
            real_write(path, text)

        with mock.patch.object(SYNC, "write_text_atomic", side_effect=failing_write):
            with self.assertRaises(SYNC.PermissionSyncError):
                self.apply()
        self.assertEqual(before, snapshot(self.root))

    def test_rollback_restores_exact_bytes_including_bom_and_crlf(self):
        self.install_claude({"claude_allow": ["Bash(shipped)"], "codex_prefix_rules": []})
        self.settings.parent.mkdir(parents=True, exist_ok=True)
        original = (b"\xef\xbb\xbf" + json.dumps({"permissions": {"allow": ["Bash(personal)"]}}, indent=2)
                    .encode("utf-8").replace(b"\n", b"\r\n"))
        self.settings.write_bytes(original)
        real_write = SYNC.write_text_atomic

        def failing_write(path, text):
            if path.name == "harness-permissions.json":
                raise OSError("disk full")
            real_write(path, text)

        with mock.patch.object(SYNC, "write_text_atomic", side_effect=failing_write):
            with self.assertRaises(SYNC.PermissionSyncError):
                self.apply()

        self.assertEqual(original, self.settings.read_bytes())

    def test_rollback_attempts_every_completed_write_and_reports_every_restore_failure(self):
        self.install_claude({"claude_allow": ["Bash(shipped)"], "codex_prefix_rules": []})
        self.install_codex({"claude_allow": [], "codex_prefix_rules": [RULE]})
        self.write_settings(["Bash(personal)"])
        real_write = SYNC.write_text_atomic

        def failing_write(path, text):
            if path.name == "harness-permissions.json":
                raise OSError("disk full")
            real_write(path, text)

        with mock.patch.object(SYNC, "write_text_atomic", side_effect=failing_write), \
                mock.patch.object(SYNC, "_restore", side_effect=OSError("cannot restore")):
            with self.assertRaises(SYNC.PermissionSyncError) as caught:
                self.apply()

        message = str(caught.exception)
        self.assertIn(str(self.settings), message)
        self.assertIn(str(self.rules), message)

    def test_a_failure_before_any_write_completes_attempts_no_restores(self):
        self.install_claude({"claude_allow": ["Bash(shipped)"], "codex_prefix_rules": []})
        self.install_codex({"claude_allow": [], "codex_prefix_rules": [RULE]})
        self.write_settings(["Bash(personal)"])
        before = snapshot(self.root)
        restores = []

        def tracking_restore(path, data):
            restores.append(path)

        def failing_write(path, text):
            raise OSError("disk full")

        with mock.patch.object(SYNC, "write_text_atomic", side_effect=failing_write), \
                mock.patch.object(SYNC, "_restore", side_effect=tracking_restore):
            with self.assertRaises(SYNC.PermissionSyncError):
                self.apply()

        self.assertEqual([], restores)
        self.assertEqual(before, snapshot(self.root))

    def test_check_writes_nothing(self):
        self.install_claude({"claude_allow": ["Bash(shipped)"], "codex_prefix_rules": []})
        self.install_codex({"claude_allow": [], "codex_prefix_rules": [RULE]})
        self.write_settings([])
        before = snapshot(self.root)
        changes, _ = SYNC.synchronize(self.environ, "check")
        self.assertEqual(2, len(changes))
        self.assertEqual(before, snapshot(self.root))

    def test_nothing_installed_creates_no_files(self):
        self.apply()
        self.assertEqual({}, snapshot(self.root))

    def test_opt_out_changes_nothing(self):
        self.install_claude({"claude_allow": ["Bash(shipped)"], "codex_prefix_rules": []})
        before = snapshot(self.root)
        changes, warnings = SYNC.synchronize(dict(self.environ, TJ_AGENTS_HARNESS_PERMISSIONS="off"), "apply")
        self.assertEqual([], changes)
        self.assertTrue(warnings)
        self.assertEqual(before, snapshot(self.root))

    def test_malformed_settings_are_left_untouched(self):
        self.install_claude({"claude_allow": ["Bash(shipped)"], "codex_prefix_rules": []})
        self.settings.write_text("{not json", encoding="utf-8")
        _, warnings = self.apply()
        self.assertTrue(warnings)
        self.assertEqual("{not json", self.settings.read_text(encoding="utf-8"))

    def test_cli_check_reports_drift_until_applied(self):
        self.install_claude({"claude_allow": ["Bash(shipped)"], "codex_prefix_rules": []})
        environ = dict(self.environ)
        with mock.patch.dict("os.environ", environ), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(1, SYNC.main(["--check"]))
            self.assertEqual(0, SYNC.main(["--apply"]))
            self.assertEqual(0, SYNC.main(["--check"]))


if __name__ == "__main__":
    unittest.main()
