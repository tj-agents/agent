import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import tomllib
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / ".agents/agent/runtime"))
import host_permissions


class HostPermissionsTests(unittest.TestCase):
    def test_app_process_detection_distinguishes_native_cli_and_desktop(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cli = root / "vendor/bin/codex.exe"
            desktop = root / "desktop/codex.exe"
            (desktop.parent / "resources").mkdir(parents=True)
            (desktop.parent / "resources/app.asar").write_bytes(b"fixture")
            self.assertFalse(host_permissions.desktop_running([cli]))
            self.assertFalse(host_permissions.desktop_running([desktop.parent / "resources/codex.exe"]))
            self.assertTrue(host_permissions.desktop_running([cli, desktop]))

    def test_app_drops_only_the_local_override_and_converges(self):
        for saved in ("guardian-approvals", "auto", "read-only", "full-access", "custom", None):
            with self.subTest(saved=saved):
                before = {"electron-persisted-atom-state": {
                    "agent-mode-by-host-id": {"local": saved, "remote": "read-only"},
                    "thread-permissions": {"existing": "read-only"}}, "window": {"x": 10}}
                expected = json.loads(json.dumps(before))
                del expected["electron-persisted-atom-state"]["agent-mode-by-host-id"]["local"]
                updated = host_permissions.codex_app_settings(json.dumps(before))
                self.assertEqual(expected, json.loads(updated))
                self.assertEqual(updated, host_permissions.codex_app_settings(updated))
        self.assertEqual("{}", host_permissions.codex_app_settings("{}"))

    def test_native_plan_checks_app_state_before_any_profile_write(self):
        with tempfile.TemporaryDirectory(prefix="app defaults ü ") as directory:
            root = Path(directory)
            claude, codex = root / "claude.json", root / "config.toml"
            desktop = root / ".codex-global-state.json"
            for malformed in ("[", "[]", '{"electron-persisted-atom-state":null}',
                              '{"electron-persisted-atom-state":{"agent-mode-by-host-id":[]}}'):
                desktop.write_text(malformed, encoding="utf-8")
                with self.subTest(malformed=malformed), self.assertRaises(ValueError):
                    host_permissions.plan(claude, codex, "bypass")
                self.assertFalse(claude.exists())
                self.assertFalse(codex.exists())
            desktop.write_text('{"electron-persisted-atom-state":{"agent-mode-by-host-id":{"local":"guardian-approvals"}}}', encoding="utf-8")
            targets = host_permissions.plan(claude, codex, "bypass")
            self.assertIn(desktop, targets)
            self.assertNotIn("local", json.loads(targets[desktop])["electron-persisted-atom-state"]["agent-mode-by-host-id"])

    def test_modes_use_native_host_values_and_preserve_unrelated_settings(self):
        for mode, claude, approval, profile in (
            ("bypass", "bypassPermissions", "never", ":danger-full-access"),
            ("standard", "default", "on-request", ":workspace"),
        ):
            with self.subTest(mode=mode):
                original = {"hooks": {"Stop": []}, "permissions": {"allow": ["Read"], "deny": ["Edit(secret)"]}}
                settings = json.loads(host_permissions.claude_settings(json.dumps(original), mode))
                self.assertEqual(claude, settings["permissions"].pop("defaultMode"))
                self.assertEqual(original, settings)
                text = 'model = "keep"\n[plugins."keep@source"]\nenabled = true\n'
                settings = tomllib.loads(host_permissions.codex_settings(text, mode))
                self.assertEqual(approval, settings.pop("approval_policy"))
                self.assertEqual(profile, settings.pop("default_permissions"))
                self.assertEqual(tomllib.loads(text), settings)

    def test_codex_migrates_legacy_multiline_permissions_without_touching_similar_text(self):
        text = '''approval_policy = { granular = { sandbox_approval = false } }
sandbox_mode = "workspace-write"
description = """Keep this literal:
[sandbox_workspace_write]
network_access = false
"""
[sandbox_workspace_write]
writable_roots = [
  "/keep in the old sandbox only",
]
network_access = false
[projects."sample"]
trust_level = "trusted"
'''
        updated = host_permissions.codex_settings(text, "bypass")
        parsed = tomllib.loads(updated)
        self.assertEqual("never", parsed["approval_policy"])
        self.assertNotIn("sandbox_mode", parsed)
        self.assertNotIn("sandbox_workspace_write", parsed)
        self.assertEqual(tomllib.loads(text)["description"], parsed["description"])
        self.assertEqual("trusted", parsed["projects"]["sample"]["trust_level"])
        self.assertEqual(updated, host_permissions.codex_settings(updated, "bypass"))

    def test_policy_defaults_to_bypass_and_strictly_validates_the_enum(self):
        with tempfile.TemporaryDirectory(prefix="permission policy ü ") as directory:
            root = Path(directory).resolve()
            self.assertEqual("bypass", host_permissions.permissions(root))
            settings = root / ".agents/settings.json"
            settings.parent.mkdir()
            for value in ({}, {"configuration": {"core": {}}}):
                settings.write_text(json.dumps(value), encoding="utf-8")
                self.assertEqual("bypass", host_permissions.permissions(root))
            for mode in ("bypass", "standard"):
                settings.write_text(json.dumps({"configuration": {"core": {"permissions": mode}}}), encoding="utf-8")
                self.assertEqual(mode, host_permissions.permissions(root))
            for mode in (True, False, 1, None, [], {}, "bypassPermissions", "danger-full-access", "unknown"):
                with self.subTest(mode=mode):
                    settings.write_text(json.dumps({"configuration": {"core": {"permissions": mode}}}), encoding="utf-8")
                    with self.assertRaisesRegex(ValueError, "permissions must be"):
                        host_permissions.permissions(root)

    def test_conflicting_or_invalid_native_settings_fail_before_any_write(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            claude, codex = root / "claude.json", root / "codex.toml"
            claude.write_text('{"permissions":{"disableBypassPermissionsMode":"disable"}}', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "disabled"):
                host_permissions.plan(claude, codex, "bypass")
            self.assertFalse(codex.exists())
            claude.write_text("{}", encoding="utf-8")
            codex.write_text("[invalid", encoding="utf-8")
            with self.assertRaises(tomllib.TOMLDecodeError):
                host_permissions.plan(claude, codex, "bypass")
            self.assertEqual("{}", claude.read_text(encoding="utf-8"))

    def test_project_generation_defaults_to_bypass_and_round_trips_standard(self):
        scripts = ROOT / ".agents/agent/utility/plugins/scripts"
        sys.path.insert(0, str(scripts))
        spec = importlib.util.spec_from_file_location("permissions_repo_config", scripts / "repo_config.py")
        config = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(config)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            selection = root / ".agents/plugins.json"
            selection.parent.mkdir()
            selection.write_text(json.dumps({"schema_version": 1,
                "marketplaces": {"tj-agents": {"repository": "tj-agents/marketplace"}},
                "plugins": ["base@tj-agents"]}), encoding="utf-8")
            for scope in ("project", "local"):
                for mode in (None, "standard", "bypass"):
                    with self.subTest(scope=scope, mode=mode):
                        settings = {} if mode is None else {"configuration": {"core": {"permissions": mode}}}
                        (root / ".agents/settings.json").write_text(json.dumps(settings), encoding="utf-8")
                        config.run(selection, "write", root=root, scope=scope)
                        self.assertEqual([], config.run(selection, "check", root=root, scope=scope))
                        name = "settings.json" if scope == "project" else "settings.local.json"
                        claude = json.loads((root / ".claude" / name).read_text(encoding="utf-8"))
                        codex = tomllib.loads((root / ".codex/config.toml").read_text(encoding="utf-8"))
                        if mode == "standard":
                            self.assertEqual("default", claude["permissions"]["defaultMode"])
                        else:
                            self.assertNotIn("defaultMode", claude["permissions"])
                        self.assertEqual(":workspace" if mode == "standard" else ":danger-full-access", codex["default_permissions"])
                (root / ".codex/config.toml").unlink()

    def test_claude_project_scope_does_not_emit_an_ignored_bypass_setting(self):
        text = '{"permissions":{"defaultMode":"bypassPermissions","allow":["Read"]}}'
        project = json.loads(host_permissions.claude_settings(text, "bypass", project=True))
        self.assertEqual({"allow": ["Read"]}, project["permissions"])
        profile = json.loads(host_permissions.claude_settings(text, "bypass"))
        self.assertEqual("bypassPermissions", profile["permissions"]["defaultMode"])


if __name__ == "__main__":
    unittest.main()
