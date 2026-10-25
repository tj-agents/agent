"""The Agent watcher retains in-process cancellation at the signal edge."""

import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / ".agents/agent/utility/peer/scripts"
sys.path.insert(0, str(SCRIPTS))
spec = importlib.util.spec_from_file_location("agent_client_close_watcher", SCRIPTS / "client_close_watcher.py")
WATCHER = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(WATCHER)


class ClientCloseWatcherTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.entry = {"host": "codex", "session_id": "one", "pid": 12, "pid_started_at": 4.0,
                      "cwd": str(self.root), "expected_activity": 9.0}
        self.completion = self.root / "complete.json"
        self.cancel = self.root / "cancel.json"
        self.result = self.root / "result.json"

    def complete(self):
        self.completion.write_text(json.dumps({"token": "token"}), encoding="utf-8")

    def test_cancelled_request_never_signals(self):
        self.cancel.write_text(json.dumps({"cancelled": True}), encoding="utf-8")
        with mock.patch.object(WATCHER.session_close, "stop_cli_client") as stop:
            self.assertEqual(0, WATCHER.wait(self.entry, self.completion, self.cancel, "token", self.result, 1, str(self.root)))
        stop.assert_not_called()
        self.assertEqual("cancelled", json.loads(self.result.read_text(encoding="utf-8"))["status"])

    def test_new_input_inside_final_callback_sends_no_signal(self):
        self.complete()
        signalled = []

        def stop(_entry, before_signal, _cli_root):
            self.cancel.write_text(json.dumps({"cancelled": True}), encoding="utf-8")
            before_signal()
            signalled.append(True)

        with mock.patch.object(WATCHER.session_close, "stop_cli_client", side_effect=stop), \
                mock.patch.object(WATCHER.session_close, "awaiting_input_activity") as activity:
            self.assertEqual(2, WATCHER.wait(self.entry, self.completion, self.cancel, "token", self.result, 1, str(self.root)))
        activity.assert_not_called()
        self.assertEqual([], signalled)
        self.assertEqual("refused", json.loads(self.result.read_text(encoding="utf-8"))["status"])

    def test_frozen_idle_request_stops_once_then_records_end(self):
        self.complete()
        def stop_entry(_entry, before_signal, _cli_root):
            before_signal()

        with mock.patch.object(WATCHER.session_close, "stop_cli_client", side_effect=stop_entry) as stop, \
                mock.patch.object(WATCHER.session_close, "awaiting_input_activity") as activity, \
                mock.patch.object(WATCHER, "_record_end") as ended:
            self.assertEqual(0, WATCHER.wait(self.entry, self.completion, self.cancel, "token", self.result, 1, str(self.root)))
        stop.assert_called_once()
        activity.assert_called_once_with(self.entry, 9.0)
        ended.assert_called_once_with(self.entry)
        self.assertEqual("closed", json.loads(self.result.read_text(encoding="utf-8"))["status"])


if __name__ == "__main__":
    unittest.main()
