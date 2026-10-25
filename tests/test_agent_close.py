import importlib.util
import json
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

    def test_prepare_requires_explicit_frontend_and_terminal_binding(self):
        record = self.root / "record.json"
        target = self.root / "target.json"
        record.write_text(json.dumps({"frontend": {"pid": 12, "started_at": 4.0}, "terminal": {
            "kind": "tmux", "pane": "%1", "binding": {"pid": 11, "started_at": 3.0}}}), encoding="utf-8")
        self.assertEqual(0, CLOSE.main(["prepare", "--session-id", "one", "--record", str(record), "--target", str(target)]))
        self.assertEqual({"schema_version": 1, "frontend": {"pid": 12, "started_at": 4.0}, "terminal": {
            "kind": "tmux", "pane": "%1", "binding": {"pid": 11, "started_at": 3.0}}}, json.loads(target.read_text(encoding="utf-8")))
        record.write_text(json.dumps({"pid": 12, "pid_started_at": 4.0, "terminal": {"kind": "tmux"}}), encoding="utf-8")
        self.assertEqual(2, CLOSE.main(["prepare", "--session-id", "one", "--record", str(record), "--target", str(target)]))

    def test_arm_passes_only_explicit_target_and_completion_contract_to_cli(self):
        target = self.root / "target.json"
        target.write_text(json.dumps({"schema_version": 1, "frontend": {"pid": 12, "started_at": 4.0}, "terminal": {
            "kind": "tmux", "pane": "%1", "binding": {"pid": 11, "started_at": 3.0}}}), encoding="utf-8")
        with mock.patch.object(CLOSE.subprocess, "run", return_value=mock.Mock(returncode=0, stderr="")) as run:
            self.assertEqual(0, CLOSE.main(["arm", "--target", str(target), "--cli-close", str(self.root / "cli-close.py"),
                                             "--receipt", str(self.root / "receipt.json"), "--completion", str(self.root / "complete.json"),
                                             "--cancel", str(self.root / "cancel.json"), "--completion-token", "token"]))
        command = run.call_args.args[0]
        self.assertIn("--target", command)
        self.assertIn("--completion-token", command)
        self.assertNotIn("one", command)


if __name__ == "__main__":
    unittest.main()
