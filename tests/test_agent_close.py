import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
PATH = ROOT / ".agents" / "agent" / "utility" / "close" / "scripts" / "agent_close.py"
spec = importlib.util.spec_from_file_location("agent_close", PATH)
CLOSE = importlib.util.module_from_spec(spec)
spec.loader.exec_module(CLOSE)


class AgentCloseTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        environment = mock.patch.dict(os.environ, {"AGENT_STATE_DIRECTORY": str(self.root)})
        environment.start()
        self.addCleanup(environment.stop)

    def test_prepare_requires_explicit_frontend_and_terminal_binding(self):
        record = self.root / "record.json"
        target = self.root / "target.json"
        receipt = self.root / "recovery-launches" / "receipt.json"
        receipt.parent.mkdir()
        frontend = {"pid": 12, "started_at": 4.0}
        terminal = {"kind": "tmux", "pane": "%1", "binding": {"pid": 11, "started_at": 3.0}}
        receipt.write_text(json.dumps({"session": "one", "profile": "profile", "state": "launched", "wrapper": {
            "child": frontend, "terminal": terminal}}), encoding="utf-8")
        record.write_text(json.dumps({"session_id": "one", "profile": "profile", "provenance": {
            "source": "agent-startup-binding", "receipt": str(receipt)}, "frontend": frontend, "terminal": terminal}), encoding="utf-8")
        self.assertEqual(0, CLOSE.main(["prepare", "--session-id", "one", "--profile", "profile", "--record", str(record), "--target", str(target)]))
        self.assertEqual({"schema_version": 1, "frontend": {"pid": 12, "started_at": 4.0},
            "wrapper": {"pid": 11, "started_at": 3.0}, "terminal": {"kind": "tmux", "pane": "%1"}},
            json.loads(target.read_text(encoding="utf-8")))
        record.write_text(json.dumps({"pid": 12, "pid_started_at": 4.0, "terminal": {"kind": "tmux"}}), encoding="utf-8")
        self.assertEqual(2, CLOSE.main(["prepare", "--session-id", "one", "--profile", "profile", "--record", str(record), "--target", str(target)]))

    def test_arm_passes_only_explicit_target_and_completion_contract_to_cli(self):
        target = self.root / "target.json"
        target.write_text(json.dumps({"schema_version": 1, "frontend": {"pid": 12, "started_at": 4.0},
            "wrapper": {"pid": 11, "started_at": 3.0}, "terminal": {"kind": "tmux", "pane": "%1"}}), encoding="utf-8")
        with mock.patch.object(CLOSE.subprocess, "run", return_value=mock.Mock(returncode=0, stderr="")) as run:
            self.assertEqual(0, CLOSE.main(["arm", "--host", "codex", "--session-id", "one", "--turn-id", "turn", "--target", str(target), "--cli-close", str(self.root / "cli-close.py"),
                                             "--receipt", str(self.root / "receipt.json"), "--completion", str(self.root / "complete.json"),
                                             "--cancel", str(self.root / "cancel.json"), "--completion-token", "token"]))
        command = run.call_args.args[0]
        self.assertIn("--target", command)
        self.assertIn("--completion-token", command)
        self.assertNotIn("one", command)

    def test_prepare_rejects_non_finite_process_identity(self):
        record = self.root / "record.json"
        target = self.root / "target.json"
        receipt = self.root / "recovery-launches" / "receipt.json"
        receipt.parent.mkdir()
        frontend = {"pid": 12, "started_at": float("inf")}
        terminal = {"kind": "tmux", "binding": {"pid": 11, "started_at": 3.0}}
        receipt.write_text(json.dumps({"session": "one", "profile": "profile", "state": "launched", "wrapper": {
            "child": frontend, "terminal": terminal}}), encoding="utf-8")
        record.write_text(json.dumps({"session_id": "one", "profile": "profile", "provenance": {
            "source": "agent-startup-binding", "receipt": str(receipt)}, "frontend": frontend, "terminal": terminal}), encoding="utf-8")
        self.assertEqual(2, CLOSE.main(["prepare", "--session-id", "one", "--profile", "profile", "--record", str(record), "--target", str(target)]))


if __name__ == "__main__":
    unittest.main()
