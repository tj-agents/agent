from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import subprocess
import tempfile
import unittest
from unittest import mock

from git_isolation import isolate_git_config


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugins/agent/.agents/agent/runtime"))
import install
import required_plugins
from tj_agent.host import Host


class SelectionConvergenceTests(unittest.TestCase):
    def setUp(self):
        isolate_git_config()
        temporary = tempfile.TemporaryDirectory(prefix="selection upgrade ü ")
        self.addCleanup(temporary.cleanup)
        self.project = Path(temporary.name)
        source = self.project / "marketplace source"
        source.mkdir()
        snapshot = json.loads((ROOT / "plugins/agent/.agents/agent/utility/plugins/scripts/marketplace_plugins.lock.json")
                              .read_text(encoding="utf-8"))
        (source / "plugins.json").write_text(json.dumps(snapshot["marketplace"]), encoding="utf-8")
        for arguments in (["init", "--initial-branch=main"], ["add", "plugins.json"],
                          ["-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                           "-c", "core.hooksPath=", "commit", "-m", "Marketplace fixture"]):
            result = subprocess.run(["git", "-C", str(source), *arguments], capture_output=True,
                                    text=True, encoding="utf-8", errors="replace", check=False)
            self.assertEqual(0, result.returncode, result.stderr)
        environment = mock.patch.dict(os.environ, {
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "url." + source.as_uri() + ".insteadOf",
            "GIT_CONFIG_VALUE_0": "https://github.com/tj-agents/marketplace.git",
        })
        environment.start()
        self.addCleanup(environment.stop)
        (self.project / ".agents").mkdir()
        (self.project / ".agents/plugins.json").write_text(json.dumps({
            "schema_version": 1,
            "marketplaces": {"tj-agents": {"repository": "tj-agents/marketplace"}},
            "plugins": ["engineering@tj-agents"],
        }), encoding="utf-8")

    def test_installer_check_is_read_only_and_apply_repairs_before_native_inventory(self):
        before = (self.project / ".agents/plugins.json").read_bytes()
        with mock.patch.object(required_plugins, "collect") as collect:
            drift = required_plugins.check_setup(self.project, (Host.CODEX,), {})
        self.assertIn("Drift in:", drift[0])
        collect.assert_not_called()
        self.assertEqual(before, (self.project / ".agents/plugins.json").read_bytes())

        def inventory(project, *_args):
            selection = required_plugins.selected(project)
            self.assertIn("agent@tj-agents", selection)
            self.assertIn("cli@tj-agents", selection)
            self.assertTrue((project / ".codex/config.toml").is_file())
            self.assertTrue((project / ".claude/settings.json").is_file())
            return None

        context = install.Context(home=self.project, environ={}, windows=False, project=self.project)
        with mock.patch.object(required_plugins, "check", side_effect=inventory), \
                mock.patch.object(install, "plugin_setup_executables", return_value={}):
            install.apply_plugin_setup(context)
        self.assertEqual([], required_plugins.reconcile_selection(self.project, check_only=True))

    def test_managed_installed_runtime_converges_without_a_checkout_or_plugin_manifest(self):
        runtime = self.project / "installed"
        install.install_runtime(runtime, ROOT / "plugins/agent")
        self.assertFalse((runtime / ".git").exists())
        self.assertFalse((runtime / ".codex-plugin/plugin.json").exists())
        script = runtime / ".agents/agent/utility/plugins/scripts/adopt_plugins.py"
        result = subprocess.run([sys.executable, "-B", str(script), "--root", str(self.project),
                                 "--converge-existing", "--mode", "write"],
                                capture_output=True, text=True, encoding="utf-8", timeout=15, check=False)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("cli@tj-agents", required_plugins.selected(self.project))


if __name__ == "__main__":
    unittest.main()
