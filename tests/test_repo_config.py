import importlib.util
import json
import os
import re
import stat
import subprocess
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock


SCRIPT = (Path(__file__).resolve().parents[1] / ".agents" /
          "agent" / "utility" / "plugins" / "scripts" / "repo_config.py")
sys.path.insert(0, str(SCRIPT.parent))
spec = importlib.util.spec_from_file_location("repo_config", SCRIPT)
repo_config = importlib.util.module_from_spec(spec)
spec.loader.exec_module(repo_config)

class RepoConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.lock = self.root / ".agents" / "plugins.json"
        self.lock.parent.mkdir()
        self.plugins = ["base@tj-agents", "engineering@tj-agents", "machine@tj-agents"]
        self.save_lock()

    def save_lock(self):
        document = {"schema_version": 1, "marketplaces": {"tj-agents": {"repository": "tj-agents/marketplace"}}, "plugins": self.plugins}
        self.lock.write_text(json.dumps(document), encoding="utf-8")

    def test_generates_both_hosts_preserving_unrelated_settings_and_checks_drift(self):
        claude = self.root / ".claude" / "settings.json"
        codex = self.root / ".codex" / "config.toml"
        claude.parent.mkdir()
        codex.parent.mkdir()
        claude.write_text(json.dumps({"env": {"MODE": "test"}, "enabledPlugins": {"old@other": True}}), encoding="utf-8")
        codex.write_text('model = "test"\n\n[plugins."old@other"]\nenabled = true\n', encoding="utf-8")
        self.assertEqual([".claude/settings.json", ".codex/config.toml"], repo_config.run(self.lock, "check"))
        repo_config.run(self.lock, "write")
        self.assertEqual([], repo_config.run(self.lock, "check"))
        c = json.loads(claude.read_text(encoding="utf-8"))
        self.assertEqual("test", c["env"]["MODE"])
        self.assertEqual("tj-agents/marketplace", c["extraKnownMarketplaces"]["tj-agents"]["source"]["repo"])
        self.assertNotIn("ref", c["extraKnownMarketplaces"]["tj-agents"]["source"])
        self.assertTrue(c["extraKnownMarketplaces"]["tj-agents"]["autoUpdate"])
        self.assertEqual(3, len(c["enabledPlugins"]))
        self.assertNotIn("old@other", c["enabledPlugins"])
        self.assertEqual([], c["permissions"]["allow"])
        d = codex.read_text(encoding="utf-8")
        self.assertIn('model = "test"', d)
        self.assertIn('source = "https://github.com/tj-agents/marketplace.git"', d)
        self.assertNotIn('ref = ', d)
        self.assertIn('sparse_paths = [".agents/plugins", ".claude-plugin", "plugins"]', d)
        self.assertEqual(3, d.count('[plugins."'))

    def test_overlay_composes_permissions_and_removes_stale_rules(self):
        overlay = self.root / ".agents" / "repository-harness.json"
        rule = {
            "pattern": ["sample", "safe"],
            "justification": "Required for a repository workflow.",
            "match": ["sample safe --check"],
            "not_match": ["sample unsafe"],
        }
        overlay.write_text(json.dumps({
            "schema_version": 1,
            "requires": {
                "marketplaces": [{"id": "tj-agents", "repository": "tj-agents/marketplace"}],
                "plugins": ["tj-agents/base"],
                "hooks": [],
                "permissions": {
                    "claude_allow": ["Bash(sample safe)"],
                    "codex_prefix_rules": [rule],
                },
            },
        }), encoding="utf-8")
        self.assertIn(".codex/rules/agent-harness.rules", repo_config.run(self.lock, "check"))
        repo_config.run(self.lock, "write")
        self.assertEqual([], repo_config.run(self.lock, "check"))
        claude = json.loads((self.root / ".claude/settings.json").read_text(encoding="utf-8"))
        self.assertEqual(1, len(claude["permissions"]["allow"]))
        path = self.root / ".codex/rules/agent-harness.rules"
        self.assertIn('decision = "allow"', path.read_text(encoding="utf-8"))
        overlay.unlink()
        with self.assertRaisesRegex(repo_config.lock_io.LockError, "unmanaged permissions.allow entries: Bash\\(sample safe\\)"):
            repo_config.run(self.lock, "check")
        claude["permissions"]["allow"].remove("Bash(sample safe)")
        (self.root / ".claude/settings.json").write_text(json.dumps(claude), encoding="utf-8")
        self.assertEqual([".claude/settings.json", ".codex/rules/agent-harness.rules"], repo_config.run(self.lock, "check"))
        repo_config.run(self.lock, "write")
        self.assertFalse(path.exists())

    def test_exact_script_approvals_keep_their_argument_scope(self):
        commands = [
            "Bash(python -B .agents/workflows/workflow_ops.py --check)",
            "Bash(pwsh .agents/sync-generated.ps1 -Check)",
            'Bash(python -B "tools/policy runner.py" --name café)',
        ]
        self.overlay(commands)
        repo_config.run(self.lock, "write")
        self.assertEqual([], repo_config.run(self.lock, "check"))
        settings = json.loads((self.root / ".claude/settings.json").read_text(encoding="utf-8"))
        self.assertEqual(set(commands), set(settings["permissions"]["allow"]))
        self.assertTrue(all(":*" not in command for command in settings["permissions"]["allow"]))

    def test_wildcard_script_arguments_keep_the_stable_prefix_guard(self):
        for command in (
            "Bash(python -B .agents/workflows/workflow_ops.py)",
            "Bash(python -B .agents/workflows/workflow_ops.py:*)",
            "Bash(python -B .agents/workflows/workflow_ops.py --mode *)",
        ):
            with self.subTest(command=command):
                overlay = self.overlay([command])
                requires = json.loads(overlay.read_text(encoding="utf-8"))["requires"]
                repo_config.harness_permissions.validate_requires(requires, "fixture")
        for command in (
            "Bash(python -B .agents/workflows/workflow_ops.py --mode * --check)",
            "Bash(python -B .agents/*/workflow_ops.py --check)",
        ):
            with self.subTest(command=command):
                overlay = self.overlay([command])
                requires = json.loads(overlay.read_text(encoding="utf-8"))["requires"]
                with self.assertRaisesRegex(repo_config.lock_io.LockError, r"script arguments require :\*"):
                    repo_config.harness_permissions.validate_requires(requires, "fixture")

    def test_hand_written_allow_rule_blocks_check_and_write_without_mutation(self):
        settings = self.root / ".claude" / "settings.json"
        settings.parent.mkdir()
        original = json.dumps({"permissions": {"allow": ["Bash(hand-written-rule)"]}})
        settings.write_text(original, encoding="utf-8")
        for mode in ("check", "write"):
            with self.subTest(mode=mode):
                with self.assertRaisesRegex(repo_config.lock_io.LockError, "Bash\\(hand-written-rule\\)"):
                    repo_config.run(self.lock, mode)
                self.assertEqual(original, settings.read_text(encoding="utf-8"))
                self.assertFalse((self.root / ".codex").exists())

    def test_overlay_required_plugin_must_be_selected(self):
        overlay = self.root / ".agents" / "repository-harness.json"
        overlay.write_text(json.dumps({
            "schema_version": 1,
            "requires": {
                "marketplaces": [{"id": "tj-agents", "repository": "tj-agents/marketplace"}],
                "plugins": ["tj-agents/react"],
                "hooks": [],
                "permissions": {"claude_allow": [], "codex_prefix_rules": []},
            },
        }), encoding="utf-8")
        with self.assertRaisesRegex(repo_config.lock_io.LockError, "harness-required plugins"):
            repo_config.run(self.lock, "write")

    def test_machine_specific_hook_path_blocks_project_check_and_write_without_mutation(self):
        settings = self.root / ".claude" / "settings.json"
        settings.parent.mkdir()
        hook = {"type": "command", "command": "pwsh -File C:\\Users\\Someone\\session-hook.ps1"}
        original = json.dumps({"hooks": {"SessionStart": [{"hooks": [hook]}]}}, indent=2)
        settings.write_text(original, encoding="utf-8")
        for mode in ("check", "write", "preview"):
            with self.subTest(mode=mode):
                with self.assertRaisesRegex(repo_config.lock_io.LockError, r"\.claude/settings\.json:\d+: .*Someone"):
                    repo_config.run(self.lock, mode)
                self.assertEqual(original, settings.read_text(encoding="utf-8"))
                self.assertFalse((self.root / ".codex").exists())

    def test_machine_specific_codex_hook_and_config_paths_block_project_check(self):
        cases = {
            ".codex/config.toml": 'model = "test"\nnotify = ["/home/someone/bin/notify"]\n',
            ".codex/hooks.json": json.dumps({"hooks": {"Stop": [{"command": "D:/tools/stop.py"}]}}),
        }
        for relative, text in cases.items():
            with self.subTest(path=relative):
                path = self.root / relative
                path.parent.mkdir(exist_ok=True)
                path.write_text(text, encoding="utf-8")
                with self.assertRaisesRegex(repo_config.lock_io.LockError, re.escape(relative) + ":"):
                    repo_config.run(self.lock, "check")
                path.unlink()
        repo_config.run(self.lock, "write")
        self.assertEqual([], repo_config.run(self.lock, "check"))

    def test_local_scope_does_not_reject_machine_paths_in_uncommitted_settings(self):
        settings = self.root / ".claude" / "settings.local.json"
        settings.parent.mkdir()
        settings.write_text(json.dumps({"env": {"TOOL": "C:\\Users\\Someone\\tool.exe"}}), encoding="utf-8")
        self.local()
        self.assertEqual([], self.local("check"))

    def local(self, mode="write", root=None):
        return repo_config.run(self.lock, mode, root=root or self.root, scope="local")

    def git(self, *arguments, root=None):
        return subprocess.run(["git", "-c", "core.hooksPath=none", "-c", "commit.gpgsign=false", "-c", "user.name=Fixture", "-c", "user.email=fixture@example.test", "-C", str(root or self.root), *arguments], check=True, capture_output=True, encoding="utf-8").stdout.strip()

    def test_foreign_owned_repository_refuses_project_scope_and_defaults_to_excluded_local(self):
        self.git("init", "-q")
        self.git("remote", "add", "origin", "https://github.com/Infonetica/cris-auth.git")
        with self.assertRaisesRegex(repo_config.lock_io.LockError, "infonetica"):
            repo_config.plan(self.lock, root=self.root, scope="project")
        repo_config.run(self.lock, "write", root=self.root)
        self.assertFalse((self.root / ".claude/settings.json").exists())
        self.assertTrue((self.root / ".claude/settings.local.json").is_file())
        self.assertEqual("", self.git("status", "--porcelain", "--untracked-files=all"))

    def test_non_github_remote_is_foreign(self):
        self.git("init", "-q")
        self.git("remote", "add", "origin", "https://dev.azure.com/Org/Project/_git/repo")
        with self.assertRaises(repo_config.lock_io.LockError):
            repo_config.plan(self.lock, root=self.root, scope="project")

    def test_personal_repository_defaults_to_project_scope(self):
        self.git("init", "-q")
        self.git("remote", "add", "origin", "git@github.com:tomjseery/Concertable.git")
        self.git("remote", "add", "agents", "https://github.com/tj-agents/core.git")
        self.assertIn(self.root / ".claude/settings.json", repo_config.plan(self.lock, root=self.root))

    def overlay(self, allow):
        path = self.lock.parent / "repository-harness.json"
        path.write_text(json.dumps({"schema_version": 1, "requires": {"marketplaces": [], "plugins": [], "hooks": [], "permissions": {"claude_allow": allow, "codex_prefix_rules": []}}}), encoding="utf-8")
        return path

    def test_local_preview_is_complete_and_non_git_root_is_explicit(self):
        destination = self.root / "aggregate"
        destination.mkdir()
        targets = repo_config.plan(self.lock, root=destination, scope="local")
        self.assertEqual({".claude/settings.local.json", ".codex/config.toml", ".codex/rules/agent-harness.rules", repo_config.LOCAL_STATE}, {path.relative_to(destination.resolve()).as_posix() for path in targets})
        before = self.lock.read_bytes()
        self.local("preview", destination)
        self.assertEqual([], list(destination.iterdir()))
        self.local(root=destination)
        self.assertEqual([], self.local("check", destination))
        self.assertEqual(before, self.lock.read_bytes())
        self.assertFalse((destination / ".git").exists())
        self.assertFalse((destination / ".claude/settings.json").exists())

    def test_local_preserves_foreign_selections_settings_and_approvals(self):
        claude = self.root / ".claude/settings.local.json"
        codex = self.root / ".codex/config.toml"
        claude.parent.mkdir()
        codex.parent.mkdir()
        claude.write_text(json.dumps({"env": {"MODE": "test"}, "enabledPlugins": {"browser@foreign": True}, "extraKnownMarketplaces": {"foreign": {"source": "local"}}, "permissions": {"allow": ["Bash(manual)", "Bash(shared)"], "deny": ["Bash(forbidden)"], "ask": ["Bash(prompt)"]}}), encoding="utf-8")
        prefix = 'model = "test"\n[plugins."browser@foreign"]\nenabled = true\n[marketplaces.foreign]\nsource = "local"\n'
        codex.write_text(prefix, encoding="utf-8")
        overlay = self.overlay(["Bash(shared)", "Bash(owned)"])
        self.local()
        first = {path: path.read_bytes() for path in repo_config.plan(self.lock, root=self.root, scope="local") if path.exists()}
        self.local()
        self.assertEqual(first, {path: path.read_bytes() for path in first})
        overlay.unlink()
        self.local()
        settings = json.loads(claude.read_text(encoding="utf-8"))
        self.assertIs(settings["enabledPlugins"]["browser@foreign"], True)
        self.assertIn("foreign", settings["extraKnownMarketplaces"])
        self.assertEqual({"MODE": "test"}, settings["env"])
        self.assertNotIn("defaultMode", settings["permissions"])
        self.assertEqual({"allow": ["Bash(manual)", "Bash(shared)"], "deny": ["Bash(forbidden)"], "ask": ["Bash(prompt)"]}, settings["permissions"])
        self.assertIn(prefix, codex.read_text(encoding="utf-8"))

    def test_local_restores_prior_value_when_selection_becomes_stale(self):
        claude = self.root / ".claude/settings.local.json"
        claude.parent.mkdir()
        claude.write_text(json.dumps({"enabledPlugins": {"base@tj-agents": False}}), encoding="utf-8")
        self.local()
        state = repo_config.local_state(self.root / repo_config.LOCAL_STATE)
        settings, owned = repo_config.local_claude(claude, {}, {}, [], state["claude"])
        self.assertEqual({"base@tj-agents": False}, json.loads(settings)["enabledPlugins"])
        self.assertEqual({}, owned["enabledPlugins"])
        self.assertEqual({}, json.loads(settings)["extraKnownMarketplaces"])

    def test_local_collision_and_changed_owned_keys_fail_before_any_mutation(self):
        codex = self.root / ".codex/config.toml"
        codex.parent.mkdir()
        text = '[plugins."base@tj-agents"]\nenabled = true\n'
        codex.write_text(text, encoding="utf-8")
        with self.assertRaisesRegex(repo_config.lock_io.LockError, "collision"):
            self.local()
        self.assertFalse((self.root / ".claude").exists())
        self.assertEqual(text, codex.read_text(encoding="utf-8"))
        codex.unlink()
        self.local()
        claude = self.root / ".claude/settings.local.json"
        settings = json.loads(claude.read_text(encoding="utf-8"))
        settings["enabledPlugins"]["base@tj-agents"] = False
        claude.write_text(json.dumps(settings), encoding="utf-8")
        unchanged = codex.read_bytes()
        with self.assertRaisesRegex(repo_config.lock_io.LockError, "owned setting was changed"):
            self.local()
        self.assertEqual(unchanged, codex.read_bytes())

    def test_local_rejects_unowned_or_changed_codex_block_and_rules(self):
        self.local()
        codex = self.root / ".codex/config.toml"
        codex.write_text(codex.read_text(encoding="utf-8").replace('enabled = true', 'enabled = false', 1), encoding="utf-8")
        with self.assertRaisesRegex(repo_config.lock_io.LockError, "not owned or was changed"):
            self.local()
        codex.unlink()
        rules = self.root / ".codex/rules/agent-harness.rules"
        rules.parent.mkdir(exist_ok=True)
        rules.write_text("manual rule", encoding="utf-8")
        with self.assertRaisesRegex(repo_config.lock_io.LockError, "rules are not owned"):
            self.local()
        self.assertFalse(codex.exists())

    def test_local_tracked_target_is_rejected_with_no_changes(self):
        self.git("init", "--quiet")
        codex = self.root / ".codex/config.toml"
        codex.parent.mkdir()
        codex.write_text('model = "keep"\n', encoding="utf-8")
        self.git("add", ".codex/config.toml")
        before = codex.read_bytes()
        with self.assertRaisesRegex(repo_config.lock_io.LockError, "tracked"):
            self.local()
        self.assertEqual(before, codex.read_bytes())
        self.assertFalse((self.root / ".claude").exists())

    def test_git_exclusions_preserve_unrelated_patterns_and_are_exact(self):
        self.git("init", "--quiet")
        exclude = self.root / ".git/info/exclude"
        exclude.write_text("unrelated.log\nliteral\\ \n", encoding="utf-8")
        preview = self.local("preview")
        self.assertIn(".git/info/exclude", preview)
        self.assertEqual("unrelated.log\nliteral\\ \n", exclude.read_text(encoding="utf-8"))
        self.local()
        content = exclude.read_text(encoding="utf-8")
        self.assertTrue(content.startswith("unrelated.log\nliteral\\ \n"))
        for relative in (".claude/settings.local.json", ".codex/config.toml", ".codex/rules/agent-harness.rules", repo_config.LOCAL_STATE):
            self.assertIn("/" + relative + "\n", content)
            self.assertEqual(relative, self.git("check-ignore", relative))
        self.assertNotIn("/.claude/\n", content)
        self.assertEqual([], self.local("check"))

    def test_git_file_worktree_uses_common_info_exclude(self):
        self.git("init", "--quiet")
        self.git("commit", "--allow-empty", "--quiet", "-m", "Fixture")
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary).resolve() / "checkout"
            self.git("worktree", "add", "--quiet", "-b", "fixture-worktree", str(destination))
            self.assertTrue((destination / ".git").is_file())
            targets = repo_config.plan(self.lock, root=destination, scope="local")
            exclude = (self.root / ".git/info/exclude").resolve()
            self.assertIn(exclude, [path.resolve() for path in targets])
            self.local(root=destination)
            self.assertEqual([], self.local("check", destination))
            self.assertEqual(".codex/config.toml", self.git("check-ignore", ".codex/config.toml", root=destination))

    def test_prospective_paths_reject_escape_tracked_and_non_directory_parent(self):
        with self.assertRaisesRegex(repo_config.lock_io.LockError, "escapes"):
            repo_config.preflight_paths(self.root, [Path("../escape.json")])
        (self.root / ".codex").write_text("file", encoding="utf-8")
        with self.assertRaisesRegex(repo_config.lock_io.LockError, "parent is not a directory"):
            self.local()
        self.assertFalse((self.root / ".claude").exists())
        (self.root / ".codex").unlink()
        self.git("init", "--quiet")
        prospective = self.root / "future.json"
        prospective.write_text("tracked", encoding="utf-8")
        self.git("add", "future.json")
        with self.assertRaisesRegex(repo_config.lock_io.LockError, "tracked"):
            repo_config.plan(self.lock, root=self.root, scope="local", prospective_paths=[prospective])
        self.assertFalse((self.root / ".claude").exists())

    def test_untracked_bracket_output_does_not_match_different_indexed_path(self):
        self.git("init", "--quiet")
        tracked = self.root / ".agents/catalog/catalog.json"
        tracked.parent.mkdir()
        tracked.write_text("tracked", encoding="utf-8")
        self.git("add", tracked.relative_to(self.root).as_posix())
        prospective = tracked.with_name("catalo[g].json")
        targets = repo_config.plan(self.lock, root=self.root, scope="local", prospective_paths=[prospective])
        repo_config.write_batch({path: expected for path, expected in targets.items() if repo_config.text_at(path) != expected})
        self.assertEqual("tracked", tracked.read_text(encoding="utf-8"))
        self.assertFalse(prospective.exists())
        self.assertTrue((self.root / ".claude/settings.local.json").is_file())
        self.assertTrue((self.root / repo_config.LOCAL_STATE).is_file())

    def test_exact_indexed_bracket_output_is_rejected_without_writes(self):
        self.git("init", "--quiet")
        prospective = self.root / ".agents/catalog/catalo[g].json"
        prospective.parent.mkdir()
        prospective.write_text("tracked", encoding="utf-8")
        self.git("--literal-pathspecs", "add", prospective.relative_to(self.root).as_posix())
        before = self.snapshot_files()
        with self.assertRaisesRegex(repo_config.lock_io.LockError, r"tracked.*catalo\[g\]\.json"):
            targets = repo_config.plan(self.lock, root=self.root, scope="local", prospective_paths=[prospective])
            repo_config.write_batch({path: expected for path, expected in targets.items() if repo_config.text_at(path) != expected})
        self.assertEqual(before, self.snapshot_files())

    def test_escaping_link_with_missing_leaf_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            outside = Path(temporary).resolve()
            link = self.root / ".codex"
            try:
                link.symlink_to(outside, target_is_directory=True)
            except OSError:
                result = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(outside)], capture_output=True)
                if result.returncode:
                    self.skipTest("Directory links unavailable")
            with self.assertRaisesRegex(repo_config.lock_io.LockError, "escapes"):
                self.local()
            self.assertEqual([], list(outside.iterdir()))
            self.assertFalse((self.root / ".claude").exists())

    def test_malformed_ownership_and_exclusions_are_preflighted(self):
        state = self.root / repo_config.LOCAL_STATE
        state.write_text('{"schema_version":1,"claude":[]}', encoding="utf-8")
        with self.assertRaisesRegex(repo_config.lock_io.LockError, "ownership"):
            self.local()
        self.assertFalse((self.root / ".claude").exists())
        state.unlink()
        self.git("init", "--quiet")
        exclude = self.root / ".git/info/exclude"
        exclude.write_text(repo_config.EXCLUDE_START, encoding="utf-8")
        with self.assertRaisesRegex(repo_config.lock_io.LockError, "malformed managed exclusions"):
            self.local()
        self.assertFalse((self.root / ".claude").exists())

    def test_non_git_subdirectory_cannot_bypass_checkout_tracking(self):
        self.git("init", "--quiet")
        child = self.root / "subdirectory"
        child.mkdir()
        with self.assertRaisesRegex(repo_config.lock_io.LockError, "exact Git checkout"):
            self.local(root=child)
        self.assertEqual([], list(child.iterdir()))

    def test_exclusion_parent_failure_is_detected_before_settings_write(self):
        self.git("init", "--quiet")
        info = self.root / ".git/info"
        (info / "exclude").unlink()
        info.rmdir()
        info.write_text("blocked", encoding="utf-8")
        with self.assertRaisesRegex(repo_config.lock_io.LockError, "parent is not a directory"):
            self.local()
        self.assertFalse((self.root / ".claude").exists())

    def test_prospective_exclusions_escape_pattern_metacharacters(self):
        self.git("init", "--quiet")
        target = self.root / "receipt[1].json"
        targets = repo_config.plan(self.lock, root=self.root, scope="local", prospective_paths=[target])
        exclude = self.root / ".git/info/exclude"
        exclude.write_text(targets[exclude.resolve()], encoding="utf-8")
        self.assertEqual("receipt[1].json", self.git("check-ignore", "receipt[1].json"))
        result = subprocess.run(["git", "-C", str(self.root), "check-ignore", "receipt1.json"], capture_output=True)
        self.assertEqual(1, result.returncode)

    def test_local_stale_owned_rules_are_removed(self):
        overlay = self.overlay([])
        value = json.loads(overlay.read_text(encoding="utf-8"))
        value["requires"]["permissions"]["codex_prefix_rules"] = [{"pattern": ["sample", "safe"], "justification": "Fixture approval", "match": ["sample safe"], "not_match": ["sample unsafe"]}]
        overlay.write_text(json.dumps(value), encoding="utf-8")
        self.local()
        rules = self.root / ".codex/rules/agent-harness.rules"
        self.assertIn('decision = "allow"', rules.read_text(encoding="utf-8"))
        overlay.unlink()
        self.assertIn(".codex/rules/agent-harness.rules", self.local("preview"))
        self.assertTrue(rules.is_file())
        self.local()
        self.assertFalse(rules.exists())
        self.assertEqual([], self.local("check"))

    def test_tracked_config_behind_in_root_directory_link_is_rejected(self):
        self.git("init", "--quiet")
        shared = self.root / "shared"
        shared.mkdir()
        (shared / "config.toml").write_text('model = "keep"', encoding="utf-8")
        self.git("add", "shared/config.toml")
        link = self.root / ".codex"
        try:
            link.symlink_to(shared, target_is_directory=True)
        except OSError:
            result = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(shared)], capture_output=True)
            if result.returncode:
                self.skipTest("Directory links unavailable")
        with self.assertRaisesRegex(repo_config.lock_io.LockError, "tracked"):
            self.local()
        self.assertFalse((self.root / ".claude").exists())
        self.assertEqual('model = "keep"', (shared / "config.toml").read_text(encoding="utf-8"))

    def test_two_host_targets_cannot_alias_the_same_missing_file(self):
        claude = self.root / ".claude/settings.local.json"
        claude.parent.mkdir()
        original = Path.resolve
        codex = self.root / ".codex/config.toml"

        def physical_path(path, *args, **kwargs):
            return original(codex if path == claude else path, *args, **kwargs)

        with mock.patch.object(Path, "resolve", autospec=True, side_effect=physical_path):
            with self.assertRaisesRegex(repo_config.lock_io.LockError, "alias the same file"):
                self.local()
        self.assertFalse((self.root / ".codex").exists())
        self.assertFalse((self.root / repo_config.LOCAL_STATE).exists())

    def snapshot_files(self, root=None):
        root = root or self.root
        return {path.relative_to(root).as_posix(): path.read_bytes() for path in root.rglob("*") if path.is_file()}

    def directory_link(self, path, target):
        try:
            path.symlink_to(target, target_is_directory=True)
        except OSError:
            result = subprocess.run(["cmd", "/c", "mklink", "/J", str(path), str(target)], capture_output=True)
            if result.returncode:
                self.skipTest("Directory links unavailable")

    def test_output_hardlinks_to_tracked_and_outside_files_are_rejected(self):
        self.git("init", "--quiet")
        codex = self.root / ".codex/config.toml"
        codex.parent.mkdir()
        tracked = self.root / "tracked.toml"
        tracked.write_text('model = "keep"\n', encoding="utf-8")
        self.git("add", "tracked.toml")
        with tempfile.TemporaryDirectory() as temporary:
            outside = Path(temporary) / "outside.toml"
            outside.write_bytes(tracked.read_bytes())
            for original in (tracked, outside):
                with self.subTest(original=original):
                    os.link(original, codex)
                    before = self.snapshot_files()
                    with self.assertRaisesRegex(repo_config.lock_io.LockError, "multiple hard links"):
                        self.local()
                    self.assertEqual(before, self.snapshot_files())
                    self.assertEqual(tracked.read_bytes(), original.read_bytes())
                    codex.unlink()

    def test_lock_and_overlay_cannot_be_output_paths(self):
        claude = self.root / ".claude/settings.local.json"
        claude.parent.mkdir()
        for kind in ("lock", "overlay"):
            with self.subTest(kind=kind):
                if kind == "lock":
                    claude.write_bytes(self.lock.read_bytes())
                    lock, prospective = claude, []
                else:
                    lock, prospective = self.lock, [self.overlay([])]
                before = self.snapshot_files()
                with self.assertRaisesRegex(repo_config.lock_io.LockError, "aliases a read input"):
                    repo_config.plan(lock, root=self.root, scope="local", prospective_paths=prospective)
                self.assertEqual(before, self.snapshot_files())

    def test_read_input_canonical_alias_is_rejected_before_writing(self):
        claude = self.root / ".claude/settings.local.json"
        claude.parent.mkdir()
        claude.write_bytes(self.lock.read_bytes())
        alias = self.root / "input-alias"
        self.directory_link(alias, claude.parent)
        before = claude.read_bytes()
        with self.assertRaisesRegex(repo_config.lock_io.LockError, "aliases a read input"):
            repo_config.run(alias / claude.name, "write", root=self.root, scope="local")
        self.assertEqual(before, claude.read_bytes())
        self.assertFalse((self.root / ".codex").exists())
        self.assertFalse((self.root / repo_config.LOCAL_STATE).exists())

    def test_hardlinked_exclusion_is_rejected_before_settings_write(self):
        self.git("init", "--quiet")
        exclude = self.root / ".git/info/exclude"
        with tempfile.TemporaryDirectory() as temporary:
            outside = Path(temporary) / "exclude-copy"
            os.link(exclude, outside)
            before = self.snapshot_files()
            with self.assertRaisesRegex(repo_config.lock_io.LockError, "multiple hard links"):
                self.local()
            self.assertEqual(before, self.snapshot_files())
            self.assertEqual(before[".git/info/exclude"], outside.read_bytes())

    def test_read_input_file_identity_is_checked_even_with_distinct_paths(self):
        alias = self.root / "alias-lock.json"
        os.link(self.lock, alias)
        before = self.snapshot_files()
        with self.assertRaisesRegex(repo_config.lock_io.LockError, "aliases a read input"):
            repo_config.validate_aliases([alias], [self.lock])
        self.assertEqual(before, self.snapshot_files())

    def test_output_alias_of_git_exclusion_is_rejected_before_writing(self):
        self.git("init", "--quiet")
        exclude = self.root / ".git/info/exclude"
        claude = self.root / ".claude/settings.local.json"
        original = Path.resolve
        before = self.snapshot_files()

        def physical_path(path, *args, **kwargs):
            return original(exclude if path == claude else path, *args, **kwargs)

        with mock.patch.object(Path, "resolve", autospec=True, side_effect=physical_path):
            with self.assertRaisesRegex(repo_config.lock_io.LockError, "alias the same file"):
                self.local()
        self.assertEqual(before, self.snapshot_files())

    def test_read_input_alias_of_git_exclusion_is_rejected_before_writing(self):
        self.git("init", "--quiet")
        exclude = self.root / ".git/info/exclude"
        before = self.snapshot_files()
        with self.assertRaisesRegex(repo_config.lock_io.LockError, "aliases a read input"):
            repo_config.plan(exclude, root=self.root, scope="local")
        self.assertEqual(before, self.snapshot_files())

    def test_failed_later_readonly_output_rolls_back_then_retry_removes_owned_grant(self):
        claude = self.root / ".claude/settings.local.json"
        codex = self.root / ".codex/config.toml"
        claude.parent.mkdir()
        codex.parent.mkdir()
        claude.write_text(json.dumps({"permissions": {"allow": ["Bash(manual)"]}}), encoding="utf-8")
        codex.write_text('model = "keep"\n', encoding="utf-8")
        overlay = self.overlay(["Bash(owned)"])
        before = self.snapshot_files()
        codex.chmod(stat.S_IREAD)
        try:
            with self.assertRaises(PermissionError):
                self.local()
            self.assertEqual(before, self.snapshot_files())
        finally:
            codex.chmod(stat.S_IREAD | stat.S_IWRITE)
        self.local()
        state = repo_config.local_state(self.root / repo_config.LOCAL_STATE)
        self.assertEqual(["Bash(owned)"], state["claude"]["allow"])
        overlay.unlink()
        self.local()
        settings = json.loads(claude.read_text(encoding="utf-8"))
        self.assertEqual(["Bash(manual)"], settings["permissions"]["allow"])
        self.assertEqual([], self.local("check"))

    def test_failed_final_exclusion_rolls_back_ownership_and_deleted_rules(self):
        self.git("init", "--quiet")
        overlay = self.overlay(["Bash(owned)"])
        value = json.loads(overlay.read_text(encoding="utf-8"))
        value["requires"]["permissions"]["codex_prefix_rules"] = [{"pattern": ["sample", "safe"], "justification": "Fixture approval", "match": ["sample safe"], "not_match": ["sample unsafe"]}]
        overlay.write_text(json.dumps(value), encoding="utf-8")
        self.local()
        overlay.unlink()
        prospective = self.root / "receipt.json"
        targets = repo_config.plan(self.lock, root=self.root, scope="local", prospective_paths=[prospective])
        changed = {path: expected for path, expected in targets.items() if repo_config.text_at(path) != expected}
        exclude = self.root / ".git/info/exclude"
        before = self.snapshot_files()
        exclude.chmod(stat.S_IREAD)
        try:
            with self.assertRaises(PermissionError):
                repo_config.write_batch(changed)
            self.assertEqual(before, self.snapshot_files())
        finally:
            exclude.chmod(stat.S_IREAD | stat.S_IWRITE)
        self.local()
        self.assertFalse((self.root / ".codex/rules/agent-harness.rules").exists())
        settings = json.loads((self.root / ".claude/settings.local.json").read_text(encoding="utf-8"))
        self.assertEqual([], settings["permissions"]["allow"])

    def test_linked_worktrees_retain_each_others_physical_exclusions(self):
        self.git("init", "--quiet")
        self.git("commit", "--allow-empty", "--quiet", "-m", "Fixture")
        with tempfile.TemporaryDirectory() as temporary:
            other = Path(temporary).resolve() / "checkout"
            self.git("worktree", "add", "--quiet", "-b", "fixture-owned-exclusions", str(other))
            first_physical = self.root / "first-native"
            second_physical = other / "second-native"
            first_physical.mkdir()
            second_physical.mkdir()
            self.directory_link(self.root / ".codex", first_physical)
            self.directory_link(other / ".codex", second_physical)
            self.local()
            self.local(root=other)
            self.local()
            self.local(root=other)
            for root, physical in ((self.root, "first-native/config.toml"), (other, "second-native/config.toml")):
                self.assertEqual(physical, self.git("check-ignore", physical, root=root))
                self.assertEqual([], self.local("check", root))
            exclude = self.root / ".git/info/exclude"
            content = exclude.read_text(encoding="utf-8")
            self.assertIn("/first-native/config.toml\n", content)
            self.assertIn("/second-native/config.toml\n", content)
            self.assertEqual(2, content.count(repo_config.EXCLUDE_START))

    def test_tracked_lexical_config_behind_in_root_junction_is_rejected_without_writes(self):
        self.git("init", "--quiet")
        self.local()
        self.git("add", "--force", ".codex/config.toml")
        codex = self.root / ".codex"
        physical = self.root / "physical-codex"
        codex.rename(physical)
        self.directory_link(codex, physical)
        self.overlay(["Bash(new-owned-grant)"])
        before = self.snapshot_files()
        with self.assertRaisesRegex(repo_config.lock_io.LockError, "tracked.*\\.codex/config\\.toml"):
            self.local()
        self.assertEqual(before, self.snapshot_files())
        self.assertEqual(before["physical-codex/config.toml"], (physical / "config.toml").read_bytes())
        settings = json.loads((self.root / ".claude/settings.local.json").read_text(encoding="utf-8"))
        self.assertNotIn("Bash(new-owned-grant)", settings["permissions"]["allow"])


if __name__ == "__main__":
    unittest.main()
