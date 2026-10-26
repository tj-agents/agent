import contextlib
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest


SCRIPT = (Path(__file__).resolve().parents[1] / ".agents" /
          "agent" / "utility" / "plugins" / "scripts" / "machine_paths.py")
sys.path.insert(0, str(SCRIPT.parent))
spec = importlib.util.spec_from_file_location("machine_paths", SCRIPT)
machine_paths = importlib.util.module_from_spec(spec)
spec.loader.exec_module(machine_paths)


class MachinePathTests(unittest.TestCase):
    def test_rejects_absolute_machine_paths_in_json_and_toml_text(self):
        values = [
            "C:\\Users\\Someone\\cli-session-hook.ps1",
            "d:/repos/tool.py",
            "file:///C:/tools/x",
            "\\\\server\\share\\hook.ps1",
            "/home/someone/.local/bin/hook",
            "/Users/someone/hook.sh",
            "Read(//c/Users/someone/**)",
            "/c/Users/someone/bin/hook",
            "/mnt/c/tools/hook",
        ]
        for value in values:
            with self.subTest(value=value):
                self.assertEqual(1, len(machine_paths.findings(json.dumps({"command": value}))))
                self.assertEqual(1, len(machine_paths.findings(f"command = '{value}'\n")))

    def test_allows_portable_values(self):
        values = [
            "Edit(/.agents/**)",
            "Bash(git log:*)",
            "https://github.com/tj-agents/core.git",
            "python -B .agents/hooks/run.py",
            "/usr/bin/env python3",
            "~/.claude/hooks/run.py",
            "$HOME/bin/hook",
            "%USERPROFILE%\\bin\\hook.ps1",
            ".\\scripts\\hook.ps1",
            "docs/Users/guide.md",
        ]
        for value in values:
            with self.subTest(value=value):
                self.assertEqual([], machine_paths.findings(json.dumps({"command": value})))

    def test_scan_reports_committed_settings_with_line_numbers_and_cli_exit_status(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / ".claude").mkdir()
            (root / ".claude" / "settings.json").write_text('{\n  "x": "C:\\\\Users\\\\a"\n}\n', encoding="utf-8")
            (root / ".claude" / "settings.local.json").write_text('{"x": "C:\\\\Users\\\\a"}', encoding="utf-8")
            self.assertEqual(['.claude/settings.json:2: "x": "C:\\\\Users\\\\a"'], machine_paths.scan(root))
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(1, machine_paths.main(["--root", str(root)]))
            self.assertIn(".claude/settings.json:2:", output.getvalue())
            (root / ".claude" / "settings.json").write_text("{}\n", encoding="utf-8")
            self.assertEqual(0, machine_paths.main(["--root", str(root)]))


if __name__ == "__main__":
    unittest.main()
