import subprocess
import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "plugins" / "agent" / ".agents" / "agent" / "utility" / "plugins" / "scripts"


class PluginScriptEntryTests(unittest.TestCase):
    def test_each_plugin_script_starts_in_a_fresh_interpreter(self):
        for name in ("adopt_plugins", "repo_config"):
            with self.subTest(script=name):
                result = subprocess.run([sys.executable, "-B", str(SCRIPTS / f"{name}.py"), "--help"],
                                        capture_output=True, text=True, timeout=60, check=False)
                self.assertEqual(0, result.returncode, result.stderr)


if __name__ == "__main__":
    unittest.main()
