import importlib
from pathlib import Path
import sys
import tempfile
import unittest

if sys.version_info < (3, 11):
    raise unittest.SkipTest("Codex configuration writers require Python 3.11 or newer")

import tomllib


ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "plugins" / "agent" / ".agents" / "agent" / "runtime"
sys.path.insert(0, str(RUNTIME))


from codex import permissions as CODEX_PERMISSIONS
INSTALL = importlib.import_module("install")
AUTOUPDATE = importlib.import_module("plugin_autoupdate")
CONFIG = """model = "gpt-6.1-sol"
approval_policy = "on-request"
model_reasoning_effort = "high"
sandbox_mode = "workspace-write"

[marketplaces.openai-bundled]
source_type = "local"
source = '\\\\?\\C:\\Users\\example\\.codex\\.tmp\\bundled-marketplaces\\openai-bundled'

[marketplaces.cpp-agents]
source_type = "local"
source = '\\\\?\\C:\\Users\\example\\source\\repos\\cpp'

[plugins."browser@openai-bundled"]
enabled = true

[plugins."engineering@tj-agents"]
enabled = true

[plugins."kit@tj-agents"]
enabled = true

[plugins."machine@tj-agents"]
enabled = true

[plugins."template@tj-agents"]
enabled = true

[features]
hooks = true

[shell_environment_policy.set]
GH_TOKEN = "unauthenticated"

[projects.'c:\\users\\example\\source\\repos\\agent']
trust_level = "trusted"

[hooks.state."machine@tj-agents:hooks/codex.json:stop:0:0"]
trusted_hash = "sha256:0000000000000000000000000000000000000000000000000000000000000000"

[tui.model_availability_nux]
"gpt-6.1-sol" = 4
"""


class CodexConfigWriterTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "config.toml"
        self.path.write_bytes(CONFIG.encode("utf-8"))
        tomllib.loads(CONFIG)

    def native_permissions(self):
        text = self.path.read_text(encoding="utf-8-sig")
        updated = CODEX_PERMISSIONS.settings(text, "bypass")
        if updated != text:
            INSTALL.write_profile(self.path, updated, "utf-8", False)

    def remove_retired(self):
        AUTOUPDATE.remove_user_plugins(self.path, {"kit@tj-agents", "machine@tj-agents"})

    def assert_converges(self, writer):
        writer()
        first = self.path.read_bytes()
        writer()
        self.assertEqual(first, self.path.read_bytes())
        self.assertEqual(1, first.decode("utf-8").count("[marketplaces.openai-bundled]"))
        return tomllib.loads(first.decode("utf-8"))

    def test_native_permission_defaults_converge_without_duplicating_tables(self):
        config = self.assert_converges(self.native_permissions)
        self.assertEqual(("never", ":danger-full-access"), (config["approval_policy"], config["default_permissions"]))
        self.assertNotIn("sandbox_mode", config)

    def test_retired_plugin_removal_converges_and_preserves_unrelated_tables(self):
        config = self.assert_converges(self.remove_retired)
        self.assertEqual({"browser@openai-bundled", "engineering@tj-agents", "template@tj-agents"},
                         set(config["plugins"]))
        self.assertIn("machine@tj-agents:hooks/codex.json:stop:0:0", config["hooks"]["state"])

    def test_every_install_writer_in_sequence_converges(self):
        def install_pass():
            self.native_permissions()
            self.remove_retired()

        config = self.assert_converges(install_pass)
        self.assertEqual("gpt-6.1-sol", config["model"])
        self.assertNotIn("kit@tj-agents", config["plugins"])


if __name__ == "__main__":
    unittest.main()
