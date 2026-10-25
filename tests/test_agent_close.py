import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
CLI_CLOSE = ROOT.parent / "cli/.worktrees/Feature-CliOperations/.agents/cli/utility/close/scripts/close_session.py"
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

    def test_prepare_requires_an_attributable_frontend_record(self):
        record = self.root / "record.json"
        target = self.root / "target.json"
        receipt = self.root / "recovery-launches" / "receipt.json"
        receipt.parent.mkdir()
        frontend = {"pid": 12, "started_at": 4.0}
        record.write_text(json.dumps({"session_id": "one", "profile": "profile", "host": "codex",
            "client_identity": "managed-input", "pid": frontend["pid"], "pid_started_at": frontend["started_at"]}), encoding="utf-8")
        self.assertEqual(0, CLOSE.main(["prepare", "--session-id", "one", "--profile", "profile", "--record", str(record), "--target", str(target)]))
        self.assertEqual({"schema_version": 1, "frontend": {"pid": 12, "started_at": 4.0}},
            json.loads(target.read_text(encoding="utf-8")))
        record.write_text(json.dumps({"pid": 12, "pid_started_at": 4.0}), encoding="utf-8")
        self.assertEqual(2, CLOSE.main(["prepare", "--session-id", "one", "--profile", "profile", "--record", str(record), "--target", str(target)]))

    def test_arm_starts_one_agent_watcher_with_a_frozen_frontend(self):
        target = self.root / "target.json"
        record = self.root / "record.json"
        target.write_text(json.dumps({"schema_version": 1, "frontend": {"pid": 12, "started_at": 4.0}}), encoding="utf-8")
        record.write_text(json.dumps({"session_id": "one", "profile": "profile", "host": "codex",
            "client_identity": "managed-input", "pid": 12, "pid_started_at": 4.0,
            "recording_path": "events", "client_message_id": "input", "backend": None}), encoding="utf-8")
        shared = mock.Mock()
        shared.__file__ = str(ROOT / ".agents/agent/utility/peer/scripts/session_close.py")
        shared.awaiting_input_activity.return_value = 7.0
        shared.start_agent_watcher.return_value = 99
        arguments = ["arm", "--host", "codex", "--session-id", "one", "--turn-id", "turn", "--profile", "profile", "--record", str(record), "--target", str(target),
                     "--cli-close", str(CLI_CLOSE), "--completion", str(self.root / "complete.json"), "--cancel", str(self.root / "cancel.json"), "--completion-token", "token"]
        with mock.patch.object(CLOSE, "_session_close", return_value=shared):
            self.assertEqual(0, CLOSE.main(arguments))
            self.assertEqual(2, CLOSE.main(arguments))
        request = json.loads((self.root / "client-close" / "token.json").read_text(encoding="utf-8"))
        self.assertEqual(7.0, request["entry"]["expected_activity"])
        self.assertEqual(str(CLI_CLOSE.parents[5]), request["cli_root"])
        self.assertEqual(["--request", str(self.root / "client-close" / "token.json"), "--completion", str(self.root / "complete.json"), "--cancel", str(self.root / "cancel.json"), "--result", str(self.root / "client-close" / "token.result.json")],
                         shared.start_agent_watcher.call_args.args[1])
        shared.start_agent_watcher.assert_called_once()

    def test_prepare_rejects_non_finite_process_identity(self):
        record = self.root / "record.json"
        target = self.root / "target.json"
        receipt = self.root / "recovery-launches" / "receipt.json"
        receipt.parent.mkdir()
        frontend = {"pid": 12, "started_at": float("inf")}
        record.write_text(json.dumps({"session_id": "one", "profile": "profile", "client_identity": "managed-input",
            "pid": frontend["pid"], "pid_started_at": frontend["started_at"]}), encoding="utf-8")
        self.assertEqual(2, CLOSE.main(["prepare", "--session-id", "one", "--profile", "profile", "--record", str(record), "--target", str(target)]))


if __name__ == "__main__":
    unittest.main()
