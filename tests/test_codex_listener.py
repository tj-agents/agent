import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]


def load(name, relative):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


LISTENER = load(
    "test_codex_listener",
    "plugins/agent/.agents/agent/utility/listen/scripts/codex_listener.py",
)
WATCHER = load(
    "test_codex_listener_watcher",
    "plugins/agent/.agents/agent/utility/listen/scripts/codex_listener_watcher.py",
)


class CodexListenerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="codex listener ")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.profile = self.root / "profile"
        self.profile.mkdir()
        self.recording = self.root / "recording.jsonl"
        self.recording.write_text("", encoding="utf-8")
        self.state = self.root / "state"
        record = {
            "session_id": "thread-one",
            "host": "codex",
            "cwd": str(self.root),
            "profile": str(self.profile),
            "recording_path": str(self.recording),
            "client_message_id": "input-one",
        }
        path = self.state / "cli-sessions/thread-one.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(record), encoding="utf-8")
        self.environment = mock.patch.dict(
            os.environ,
            {"AGENT_STATE_DIRECTORY": str(self.state), "CODEX_THREAD_ID": "thread-one"},
            clear=False,
        )
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def test_start_persists_exact_listener_and_requires_watcher_acknowledgement(self):
        class Detached:
            def start_agent_watcher(_, script, arguments):
                request = Path(arguments[1])
                value = json.loads(request.read_text(encoding="utf-8"))
                request.write_text(json.dumps({**value, "state": "running"}), encoding="utf-8")

        output = io.StringIO()
        with mock.patch.object(LISTENER, "session_close", return_value=Detached()), contextlib.redirect_stdout(output):
            self.assertEqual(0, LISTENER.main([
                "start", "--purpose", "PR #19 checks", "--", sys.executable, "-c", "print('done')",
            ]))
        result = json.loads(output.getvalue())
        state = json.loads(Path(result["state"]).read_text(encoding="utf-8"))
        self.assertEqual("listening", result["status"])
        self.assertEqual("running", state["state"])
        self.assertEqual([sys.executable, "-c", "print('done')"], state["command"])
        self.assertEqual("thread-one", state["session"]["session_id"])

    def test_watcher_cancels_command_when_the_bound_input_changes(self):
        request = {
            "schema_version": 1,
            "listener_id": "a" * 32,
            "state": "starting",
            "created_at": 1.0,
            "session": {
                "session_id": "thread-one",
                "host": "codex",
                "cwd": str(self.root),
                "profile": str(self.profile),
                "recording_path": str(self.recording),
                "client_message_id": "input-one",
            },
            "purpose": "PR #19 checks",
            "timeout": 5,
            "command": [sys.executable, "-c", "import time; time.sleep(30)"],
        }
        with mock.patch.object(WATCHER, "current_input", return_value=False):
            result = WATCHER.run_command(object(), request)
        self.assertEqual("cancelled", result["status"])
        self.assertEqual("the owning Codex input changed", result["reason"])

    def test_watcher_never_delivers_after_the_bound_input_changes(self):
        request = {
            "listener_id": "a" * 32,
            "purpose": "PR #19 checks",
            "session": {
                "session_id": "thread-one",
                "cwd": str(self.root),
                "profile": str(self.profile),
                "recording_path": str(self.recording),
                "client_message_id": "input-one",
            },
        }
        with mock.patch.object(WATCHER, "current_input", return_value=False):
            result = WATCHER.deliver(object(), request, {"status": "completed", "returncode": 0})
        self.assertEqual("cancelled", result["delivery"])


if __name__ == "__main__":
    unittest.main()
