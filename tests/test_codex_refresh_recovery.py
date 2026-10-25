import argparse
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / ".agents/agent/runtime"
sys.path.insert(0, str(RUNTIME))
sys.path.insert(0, str(ROOT / ".agents/agent/utility/peer/scripts"))


def load(name):
    spec = importlib.util.spec_from_file_location(name, RUNTIME / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class CodexRefreshRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.relaunch = load("session_relaunch")
        self.watcher = load("relaunch_watcher")
        temporary = tempfile.TemporaryDirectory(prefix="codex-refresh-recovery-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def args(self, record, token="token"):
        return argparse.Namespace(host="codex", session_id="thread", pid=17, pid_started_at=3.0,
                                  record=record, token=token, timeout=1.0, profile=self.root,
                                  recovery=self.root / "agent_recovery.py", title="Codex", prompt="continue",
                                  native_argument=["--profile", "work"], expected_cwd=self.root)

    def test_codex_options_preserve_safe_native_configuration(self):
        options = self.relaunch.codex_arguments([
            "initial prompt", "--profile", "work", "--model=gpt-6", "--no-daemon", "--sandbox", "read-only", "--config", "model=fast",
            "resume", "old-thread", "ignored prompt",
        ])
        self.assertEqual(["--profile", "work", "--model=gpt-6", "--no-daemon", "--sandbox", "read-only", "--config", "model=fast"], options)

    def test_codex_options_refuse_hook_trust_bypass(self):
        with self.assertRaisesRegex(RuntimeError, "hook-trust bypass"):
            self.relaunch.codex_arguments(["--dangerously-bypass-hook-trust"])

    def test_cancelled_or_changed_close_never_relaunches(self):
        request = self.root / "request.json"
        request.write_text('{"exact": true}', encoding="utf-8")
        result = self.root / "result.json"
        result.write_text(json.dumps({"status": "cancelled", "session_id": "thread"}), encoding="utf-8")
        record = self.root / "record.json"
        record.write_text(json.dumps({"state": "waiting", "token": "token", "close": {
            "request": str(request), "request_sha256": "0" * 64, "result": str(result),
        }}), encoding="utf-8")
        args = self.args(record)
        self.assertFalse(self.watcher.wait_for_exit(args, exited=lambda *_: True, clock=lambda: 0, sleep=lambda _: None))
        state = json.loads(record.read_text(encoding="utf-8"))
        self.assertEqual("cancelled", state["state"])

    def test_closed_exact_request_requires_the_original_process_to_exit(self):
        request = self.root / "request.json"
        raw = b'{"exact": true}'
        request.write_bytes(raw)
        result = self.root / "result.json"
        result.write_text(json.dumps({"status": "closed", "session_id": "thread"}), encoding="utf-8")
        import hashlib
        record = self.root / "record.json"
        record.write_text(json.dumps({"state": "waiting", "token": "token", "close": {
            "request": str(request), "request_sha256": hashlib.sha256(raw).hexdigest(), "result": str(result),
        }}), encoding="utf-8")
        args = self.args(record)
        calls = []
        self.assertTrue(self.watcher.wait_for_exit(args, exited=lambda *identity: calls.append(identity) or True,
                                                   clock=lambda: 0, sleep=lambda _: None))
        self.assertEqual([(17, 3.0)], calls)

    def test_existing_waiting_generation_is_refused_without_overwrite(self):
        record = self.root / "record.json"
        original = {"state": "waiting", "token": "first"}
        record.write_text(json.dumps(original), encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "already armed"):
            self.relaunch.claim_record(record, {"state": "waiting", "token": "second"})
        self.assertEqual(original, json.loads(record.read_text(encoding="utf-8")))

    def test_watcher_passes_expected_checkout_to_the_actual_open(self):
        record = self.root / "record.json"
        record.write_text(json.dumps({"state": "waiting", "token": "token", "cwd": str(self.root)}), encoding="utf-8")
        command = self.watcher.launch_command(self.args(record))
        self.assertEqual(str(self.root), command[command.index("--expected-cwd") + 1])


if __name__ == "__main__":
    unittest.main()
