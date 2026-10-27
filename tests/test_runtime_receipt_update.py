import hashlib
import importlib.util
import json
import contextlib
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock
import sys


ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class RuntimeReceiptUpdateTests(unittest.TestCase):
    def setUp(self):
        runtime_directory = str(ROOT / "plugins/agent/.agents/agent/runtime")
        sys.path.insert(0, runtime_directory)
        self.addCleanup(lambda: sys.path.remove(runtime_directory))
        self.runtime = load("runtime_receipt_update", ROOT / "plugins/agent/.agents/agent/runtime/plugin_autoupdate.py")
        self.events = load("runtime_receipt_events", ROOT / "plugins/agent/.agents/agent/runtime/machine_events.py")
        temporary = tempfile.TemporaryDirectory(prefix="agent-receipt-update-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def receipt_root(self, name, runtime_body, resource_body):
        root = self.root / name
        runtime = root / ".agents/agent/runtime/plugin_autoupdate.py"
        resource = root / ".agents/agent/resources/utility-peer.txt"
        runtime.parent.mkdir(parents=True)
        resource.parent.mkdir(parents=True)
        runtime.write_bytes(runtime_body)
        resource.write_bytes(resource_body)
        exports = [".agents/agent/runtime", ".agents/agent/resources"]
        digest = self.runtime._digest(self.runtime._closure(root, exports))
        files = {key: hashlib.sha256(value).hexdigest() for key, value in self.runtime._closure(root, exports).items()}
        (root / "source.json").write_text(json.dumps({
            "repository": "tj-agents/agent", "revision": name,
            "exports": exports, "sha256": digest, "files": files,
        }), encoding="utf-8")
        return root

    def native_root(self, name, runtime_body, resource_body, repository="https://github.com/tj-agents/agent"):
        root = self.root / name
        runtime = root / ".agents/agent/runtime/plugin_autoupdate.py"
        resource = root / ".agents/agent/resources/utility-peer.txt"
        runtime.parent.mkdir(parents=True)
        resource.parent.mkdir(parents=True)
        runtime.write_bytes(runtime_body)
        resource.write_bytes(resource_body)
        manifest = root / ".codex-plugin/plugin.json"
        manifest.parent.mkdir(parents=True)
        manifest.write_text(json.dumps({"name": "agent", "repository": repository}), encoding="utf-8")
        return root

    def test_self_update_replaces_the_receipted_nested_agent_closure(self):
        target = self.receipt_root("old", b"old runtime", b"old resource")
        source = self.native_root("new", b"new runtime", b"new resource")
        current = target / ".agents/agent/runtime/plugin_autoupdate.py"
        with mock.patch.object(self.runtime, "native_agent_roots", return_value=[source]):
            self.assertTrue(self.runtime.self_update(current, {}))
        self.assertEqual(b"new runtime", current.read_bytes())
        self.assertEqual(b"new resource", (target / ".agents/agent/resources/utility-peer.txt").read_bytes())
        receipt = json.loads((target / "source.json").read_text(encoding="utf-8"))
        self.assertEqual("native:" + receipt["sha256"], receipt["revision"])

    def test_self_update_refuses_a_source_with_a_bad_receipt_digest(self):
        target = self.receipt_root("old", b"old runtime", b"old resource")
        source = self.native_root("bad", b"new runtime", b"new resource", repository="https://github.com/other/agent")
        with mock.patch.object(self.runtime, "native_agent_roots", return_value=[source]):
            self.assertFalse(self.runtime.self_update(target / ".agents/agent/runtime/plugin_autoupdate.py", {}))
        self.assertEqual(b"old runtime", (target / ".agents/agent/runtime/plugin_autoupdate.py").read_bytes())

    def test_identical_native_package_keeps_the_core_git_receipt(self):
        target = self.receipt_root("core", b"same runtime", b"same resource")
        source = self.native_root("native", b"same runtime", b"same resource")
        current = target / ".agents/agent/runtime/plugin_autoupdate.py"
        original = (target / "source.json").read_bytes()
        with mock.patch.object(self.runtime, "native_agent_roots", return_value=[source]):
            self.assertFalse(self.runtime.self_update(current, {}))
        self.assertEqual(original, (target / "source.json").read_bytes())

    def test_matching_native_payload_repairs_modified_and_deleted_owned_files(self):
        target = self.receipt_root("core", b"same runtime", b"same resource")
        source = self.native_root("native", b"same runtime", b"same resource")
        current = target / ".agents/agent/runtime/plugin_autoupdate.py"
        resource = target / ".agents/agent/resources/utility-peer.txt"
        with mock.patch.object(self.runtime, "native_agent_roots", return_value=[source]):
            current.write_bytes(b"modified")
            self.assertTrue(self.runtime.self_update(current, {}))
            self.assertEqual(b"same runtime", current.read_bytes())
            resource.unlink()
            self.assertTrue(self.runtime.self_update(current, {}))
        self.assertEqual(b"same resource", resource.read_bytes())

    def test_exact_enabled_identity_not_manifest_claim_selects_native_source(self):
        source = self.native_root("source", b"runtime", b"resource")
        claude = self.root / "claude"
        (claude / "plugins").mkdir(parents=True)
        (claude / "settings.json").write_text(json.dumps({"enabledPlugins": {"agent@other": True}}), encoding="utf-8")
        (claude / "plugins" / "installed_plugins.json").write_text(json.dumps({"plugins": {
            "agent@other": [{"installPath": str(source)}],
            "agent@tj-agents": [{"installPath": str(source)}],
        }}), encoding="utf-8")
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(claude)}):
            self.assertEqual([], self.runtime.native_agent_roots({}))
            (claude / "settings.json").write_text(json.dumps({"enabledPlugins": {"agent@tj-agents": True}}), encoding="utf-8")
            self.assertEqual([source], self.runtime.native_agent_roots({}))

    def test_pending_target_is_a_self_update_failure_not_a_silent_success(self):
        target = self.receipt_root("old", b"old runtime", b"old resource")
        receipt = json.loads((target / "source.json").read_text(encoding="utf-8"))
        receipt["pending"] = {"files": {}, "exports": []}
        (target / "source.json").write_text(json.dumps(receipt), encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "incomplete"):
            self.runtime.self_update(target / ".agents/agent/runtime/plugin_autoupdate.py", {})

    def test_main_returns_nonzero_when_managed_delivery_fails(self):
        output = io.StringIO()
        with mock.patch.object(self.runtime, "state_directory", return_value=self.root / "state"), \
                mock.patch.object(self.runtime, "load_codex_config", return_value={}), \
                mock.patch.object(self.runtime.shutil, "which", return_value=None), \
                mock.patch.object(self.runtime, "self_update", side_effect=RuntimeError("delivery blocked")), \
                contextlib.redirect_stderr(output):
            self.assertEqual(1, self.runtime.main(["--no-sweep"]))
        self.assertIn("delivery blocked", output.getvalue())

    def test_machine_events_only_allows_self_update_from_nested_managed_runtime(self):
        state = self.root / "state"
        managed = state / "bin/.agents/agent/runtime"
        managed.mkdir(parents=True)
        with mock.patch.object(self.events.plugins, "state_directory", return_value=state), \
                mock.patch.object(self.events.plugins, "main", return_value=0) as refresh, \
                mock.patch.object(self.events.plugins, "__file__", str(managed / "plugin_autoupdate.py")):
            self.assertEqual(0, self.events.refresh_plugins())
        refresh.assert_called_once_with(["--no-sweep", "--retry-failures"])


if __name__ == "__main__":
    unittest.main()
