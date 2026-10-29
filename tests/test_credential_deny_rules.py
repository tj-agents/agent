import json
from pathlib import Path
import sys
import tempfile
import unittest

if sys.version_info < (3, 11):
    raise unittest.SkipTest("native permission defaults require Python 3.11 or newer")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugins/agent/.agents/agent/runtime"))
from claude import permissions as CLAUDE_PERMISSIONS
from codex import permissions as CODEX_PERMISSIONS
from host import Host
import host_permissions


class CredentialDenyRuleTests(unittest.TestCase):
    def test_user_settings_deny_credential_widening_once_and_keep_existing_rules(self):
        existing = json.dumps({"permissions": {"deny": ["Read(.env)"], "allow": ["Bash(git status)"]}})
        first = CLAUDE_PERMISSIONS.settings(existing, "bypass")
        second = CLAUDE_PERMISSIONS.settings(first, "bypass")
        self.assertEqual(first, second)
        deny = json.loads(first)["permissions"]["deny"]
        self.assertEqual("Read(.env)", deny[0])
        for rule in ("Bash(gh auth refresh *)", "Bash(gh auth login *)",
                     "PowerShell(gh.exe auth refresh *)", "PowerShell(gh auth login *)"):
            self.assertIn(rule, deny)
        self.assertNotIn("Bash(gh auth status *)", deny)

    def test_project_settings_never_carry_the_user_deny_rules(self):
        settings = json.loads(CLAUDE_PERMISSIONS.settings("{}", "standard", project=True))
        self.assertNotIn("deny", settings["permissions"])

    def test_codex_forbidden_rule_file_is_planned_once(self):
        with tempfile.TemporaryDirectory() as directory:
            claude = Path(directory) / "claude" / "settings.json"
            codex = Path(directory) / "codex" / "config.toml"
            targets = host_permissions.plan({Host.CLAUDE: claude, Host.CODEX: codex}, "bypass")
            rules = CODEX_PERMISSIONS.rules_path(codex)
            self.assertIn('decision = "forbidden"', targets[rules])
            for path, text in targets.items():
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text, encoding="utf-8")
            self.assertEqual({}, host_permissions.plan({Host.CLAUDE: claude, Host.CODEX: codex}, "bypass"))


if __name__ == "__main__":
    unittest.main()
