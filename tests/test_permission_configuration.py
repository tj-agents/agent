import json
from pathlib import Path
import sys
import tempfile
import unittest

if sys.version_info < (3, 11):
    raise unittest.SkipTest("native permission defaults require Python 3.11 or newer")

import tomllib


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugins/agent/.agents/agent/runtime"))
from claude import permissions as claude_permissions
from codex import permissions as codex_permissions
from host import Host
import host_permissions
import repository_configuration


class PermissionConfigurationTests(unittest.TestCase):
    def test_each_host_plans_only_its_native_configuration(self):
        with tempfile.TemporaryDirectory(prefix="host settings ü ") as directory:
            root = Path(directory)
            claude = root / ".claude/settings.json"
            codex = root / ".codex/config.toml"
            claude.parent.mkdir()
            claude.write_text("[malformed", encoding="utf-8")
            plan = host_permissions.plan({Host.CODEX: codex}, "bypass")
            self.assertEqual({codex, codex_permissions.rules_path(codex)}, set(plan))
            self.assertEqual(":danger-full-access", tomllib.loads(plan[codex])["default_permissions"])
            codex.parent.mkdir()
            codex.write_text("[malformed", encoding="utf-8")
            (codex.parent / ".codex-global-state.json").write_text("[malformed", encoding="utf-8")
            claude.write_text('{"hooks":{"Stop":[]},"permissions":{"allow":["Read"]}}', encoding="utf-8")
            plan = host_permissions.plan({Host.CLAUDE: claude}, "bypass", applying=True)
            self.assertEqual({claude}, set(plan))
            settings = json.loads(plan[claude])
            self.assertEqual("bypassPermissions", settings["permissions"]["defaultMode"])
            self.assertEqual({"Stop": []}, settings["hooks"])
            self.assertEqual(["Read"], settings["permissions"]["allow"])
            self.assertEqual("[malformed", codex.read_text(encoding="utf-8"))

    def test_native_settings_preserve_unrelated_values_and_converge(self):
        for mode in ("bypass", "standard"):
            with self.subTest(mode=mode):
                text = '{"permissions":{"allow":["Read"],"deny":["Edit(secret)"]},"model":"keep"}'
                claude = claude_permissions.settings(text, mode)
                self.assertEqual(claude, claude_permissions.settings(claude, mode))
                self.assertIn("Edit(secret)", json.loads(claude)["permissions"]["deny"])
                self.assertEqual("keep", json.loads(claude)["model"])
                text = 'model = "keep"\n[plugins."keep@source"]\nenabled = true\n'
                codex = codex_permissions.settings(text, mode)
                self.assertEqual(codex, codex_permissions.settings(codex, mode))
                self.assertEqual("keep", tomllib.loads(codex)["model"])
                self.assertTrue(tomllib.loads(codex)["plugins"]["keep@source"]["enabled"])
                project = json.loads(claude_permissions.settings(claude, mode, project=True))
                if mode == "bypass":
                    self.assertNotIn("defaultMode", project["permissions"])
                else:
                    self.assertEqual("default", project["permissions"]["defaultMode"])

    def test_configuration_defaults_and_strict_types(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            self.assertEqual("bypass", repository_configuration.permissions(root))
            self.assertTrue(repository_configuration.trust_hooks(root))
            settings = root / ".agents/settings.json"
            settings.parent.mkdir()
            for value in ({}, {"permissions": "standard", "trust_hooks": False}, {"trust_hooks": True}):
                settings.write_text(json.dumps({"configuration": {"core": value}}), encoding="utf-8")
                self.assertEqual(value.get("permissions", "bypass"), repository_configuration.permissions(root))
                self.assertEqual(value.get("trust_hooks", True), repository_configuration.trust_hooks(root))
            for value in (0, 1, "true", None, [], {}):
                settings.write_text(json.dumps({"configuration": {"core": {"trust_hooks": value}}}), encoding="utf-8")
                with self.subTest(value=value), self.assertRaisesRegex(ValueError, "trust_hooks must be a boolean"):
                    repository_configuration.trust_hooks(root)
            for value in (True, None, "yolo", "bypassPermissions"):
                settings.write_text(json.dumps({"configuration": {"core": {"permissions": value}}}), encoding="utf-8")
                with self.subTest(value=value), self.assertRaisesRegex(ValueError, "permissions must be"):
                    repository_configuration.permissions(root)


if __name__ == "__main__":
    unittest.main()
