from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / 'plugins/agent'
FOLLOWUP = '.agents/agent/runtime/codex/codex_followup.py'


class NativeHostBoundaryTests(unittest.TestCase):
    def test_followup_runtime_resolves_inside_codex_package_without_claude_skill_assets(self):
        helper = PACKAGE / FOLLOWUP
        self.assertEqual((ROOT / '.codex/runtime/codex_followup.py').read_bytes(), helper.read_bytes())
        for entry in (ROOT / '.codex/skills/agent-message', PACKAGE / 'codex-skills/message'):
            with self.subTest(entry=entry):
                body = (entry / 'SKILL.md').read_text(encoding='utf-8')
                resources = re.findall(r'<package-root>/([^\s"\']+\.py)', body)
                self.assertEqual({FOLLOWUP}, set(resources))
                self.assertTrue(all((PACKAGE / resource).is_file() for resource in resources))
                self.assertFalse(list(entry.rglob('codex_followup.py')))
        for entry in (ROOT / '.claude/skills/agent-message', PACKAGE / 'skills/message',
                      PACKAGE / '.agents/agent/utility/message'):
            with self.subTest(entry=entry):
                self.assertFalse(list(entry.rglob('*.py')))
                self.assertNotIn('codex_followup.py', (entry / 'SKILL.md').read_text(encoding='utf-8'))


if __name__ == '__main__':
    unittest.main()
