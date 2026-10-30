from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock


SCRIPTS = Path(__file__).resolve().parents[1] / "plugins/agent/.agents/agent/runtime"
sys.path.insert(0, str(SCRIPTS))
import required_plugins


SCRIPT = """import os
import sys
from pathlib import Path
state = Path(os.environ['PLUGIN_SETUP_STATE'])
if sys.argv[-1] == '--check':
    raise SystemExit(0 if state.exists() else 1)
state.write_text(sys.argv[-1], encoding='utf-8')
"""


class PluginSetupTests(unittest.TestCase):
    def test_setup_repairs_a_declared_missing_plugin_then_verifies_its_runtime(self):
        import reload_plugins

        missing = required_plugins.MissingRequiredPluginsError('missing', self.project, ['selected@publisher'])
        with mock.patch.object(required_plugins, 'check', side_effect=[missing, {'selected@publisher': self.runtime}]), \
                mock.patch.object(reload_plugins, 'repair_missing') as repair:
            self.assertEqual([], required_plugins.converge_setup(self.project, ('codex',), {'codex': None}))
        repair.assert_called_once_with('codex', self.project, ('selected@publisher',), None)
        self.assertTrue(self.state.exists())

    def setUp(self):
        reconciliation = mock.patch.object(required_plugins, "reconcile_selection", return_value=[])
        reconciliation.start()
        self.addCleanup(reconciliation.stop)
        temporary = tempfile.TemporaryDirectory(prefix="plugin setup ")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.project = self.root / "project"
        (self.project / ".agents").mkdir(parents=True)
        (self.project / ".agents/plugins.json").write_text(
            json.dumps({"schema_version": 1, "plugins": ["selected@publisher"]}), encoding="utf-8")
        self.runtime = self.root / "runtime"
        script = self.runtime / "scripts/setup.py"
        script.parent.mkdir(parents=True)
        script.write_text(SCRIPT, encoding="utf-8")
        harness = self.runtime / "harness.json"
        harness.write_text(json.dumps({"plugin": "publisher/selected", "requires": {
            "setup": {"script": "scripts/setup.py"}}}), encoding="utf-8")
        self.state = self.root / "state"
        self.environment = mock.patch.dict(os.environ, {"PLUGIN_SETUP_STATE": str(self.state)})
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def roots(self):
        return mock.patch.object(required_plugins, "check", return_value={"selected@publisher": self.runtime})

    def test_check_is_read_only_and_apply_converges(self):
        with self.roots():
            self.assertEqual(["selected@publisher: setup --check failed: exit 1"],
                             required_plugins.check_setup(self.project, ("codex",), {"codex": None}))
            self.assertFalse(self.state.exists())
            self.assertEqual([], required_plugins.converge_setup(self.project, ("codex",), {"codex": None}))
            self.assertEqual("setup", self.state.read_text(encoding="utf-8"))

    def test_onboarding_is_passed_only_when_explicitly_requested(self):
        with self.roots():
            self.assertEqual([], required_plugins.converge_setup(self.project, ("codex",), {"codex": None}, onboard=True))
        self.assertEqual("--onboard", self.state.read_text(encoding="utf-8"))

    def test_unselected_runtime_is_not_examined_or_executed(self):
        unrelated = self.root / "unrelated"
        unrelated.mkdir()
        (unrelated / "harness.json").write_text(json.dumps({"plugin": "publisher/other", "requires": {
            "setup": {"script": "missing.py"}}}), encoding="utf-8")
        with mock.patch.object(required_plugins, "check", return_value={"selected@publisher": self.runtime}):
            self.assertEqual([], required_plugins.converge_setup(self.project, ("codex",), {"codex": None}))
        self.assertEqual("setup", self.state.read_text(encoding="utf-8"))

    def test_valid_harness_without_setup_is_not_an_error(self):
        (self.runtime / "harness.json").write_text(json.dumps({"plugin": "publisher/selected", "requires": {}}),
                                                   encoding="utf-8")
        with self.roots():
            self.assertEqual([], required_plugins.converge_setup(self.project, ("codex",), {"codex": None}))
        self.assertFalse(self.state.exists())

    def test_malformed_declaration_fails_without_success(self):
        harness = self.runtime / "harness.json"
        harness.write_text(json.dumps({"plugin": "publisher/selected", "requires": {"setup": {"script": "../bad.py"}}}),
                           encoding="utf-8")
        with self.roots():
            errors = required_plugins.converge_setup(self.project, ("codex",), {"codex": None})
        self.assertTrue(errors)
        self.assertFalse(self.state.exists())

    def test_conflicting_host_runtime_is_rejected(self):
        other = self.root / "other"
        (other / "scripts").mkdir(parents=True)
        (other / "scripts/setup.py").write_text(SCRIPT + "\n# changed\n", encoding="utf-8")
        harness = other / "harness.json"
        harness.write_text((self.runtime / "harness.json").read_text(encoding="utf-8"),
                           encoding="utf-8")
        with mock.patch.object(required_plugins, "check", side_effect=({"selected@publisher": self.runtime}, {"selected@publisher": other})):
            errors = required_plugins.converge_setup(self.project, ("claude", "codex"), {"claude": None, "codex": None})
        self.assertIn("runtimes disagree", errors[0])

    def test_host_adapter_differences_do_not_conflict_when_setup_runtime_matches(self):
        other = self.root / "other"
        (other / "scripts").mkdir(parents=True)
        (other / "scripts/setup.py").write_text(SCRIPT, encoding="utf-8")
        (other / "host-adapter.txt").write_text("different host payload", encoding="utf-8")
        (other / "harness.json").write_text((self.runtime / "harness.json").read_text(encoding="utf-8"),
                                              encoding="utf-8")
        with mock.patch.object(required_plugins, "check", side_effect=({"selected@publisher": self.runtime}, {"selected@publisher": other})):
            self.assertEqual([], required_plugins.converge_setup(self.project, ("claude", "codex"),
                                                       {"claude": None, "codex": None}))

    def test_same_script_with_a_different_runtime_sibling_is_rejected_before_execution(self):
        other = self.root / "other"
        (other / "scripts").mkdir(parents=True)
        (other / "scripts/setup.py").write_text(SCRIPT, encoding="utf-8")
        (other / "scripts/descriptor.json").write_text('{"different": true}', encoding="utf-8")
        (other / "harness.json").write_text((self.runtime / "harness.json").read_text(encoding="utf-8"),
                                              encoding="utf-8")
        with mock.patch.object(required_plugins, "check", side_effect=({"selected@publisher": self.runtime}, {"selected@publisher": other})), \
                mock.patch.object(required_plugins, "invoke") as invoke:
            errors = required_plugins.converge_setup(self.project, ("claude", "codex"), {"claude": None, "codex": None})
        self.assertIn("runtimes disagree", errors[0])
        invoke.assert_not_called()

    def test_setup_declaration_must_exist_on_every_requested_host(self):
        other = self.root / "other"
        other.mkdir()
        (other / "harness.json").write_text(json.dumps({"plugin": "publisher/selected", "requires": {}}),
                                              encoding="utf-8")
        with mock.patch.object(required_plugins, "check", side_effect=({"selected@publisher": self.runtime}, {"selected@publisher": other})), \
                mock.patch.object(required_plugins, "invoke") as invoke:
            errors = required_plugins.converge_setup(self.project, ("claude", "codex"), {"claude": None, "codex": None})
        self.assertIn("setup declarations disagree", errors[0])
        invoke.assert_not_called()

    @unittest.skipUnless(os.name == "nt", "Windows junction")
    def test_windows_junction_in_declared_path_is_rejected(self):
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "setup.py").write_text(SCRIPT, encoding="utf-8")
        junction = self.runtime / "scripts"
        for path in junction.iterdir():
            path.unlink()
        junction.rmdir()
        result = __import__("subprocess").run(["cmd", "/c", "mklink", "/J", str(junction), str(outside)],
                                                capture_output=True, text=True, check=False)
        if result.returncode:
            self.skipTest("cannot create a Windows junction")
        with self.roots():
            errors = required_plugins.converge_setup(self.project, ("codex",), {"codex": None})
        self.assertIn("unsafe", errors[0])


if __name__ == "__main__":
    unittest.main()
