import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "plugins/agent/.agents/agent/runtime/work_item_sessions.py"
SPEC = importlib.util.spec_from_file_location("test_work_item_sessions_module", SCRIPT)
WORK_ITEM_SESSIONS = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(WORK_ITEM_SESSIONS)


class InterruptedTests(unittest.TestCase):
    def test_last_activity_is_reported_in_utc_like_recovery(self):
        with tempfile.TemporaryDirectory(prefix="interrupted ") as directory, \
                mock.patch.dict(os.environ, {"AGENT_STATE_DIRECTORY": directory}):
            store_module = WORK_ITEM_SESSIONS.tj_agent_store
            with store_module.SessionStore() as store:
                handle = store.start_session("claude", "dead", directory, pid=4000003, pid_started_at=1.0)
                store.update_session(handle, lambda session: setattr(session, "last_activity_at", 1767225600.25))
            with mock.patch.object(WORK_ITEM_SESSIONS, "_pid_is_dead", return_value=True), \
                    mock.patch.object(WORK_ITEM_SESSIONS, "_read_time_status", return_value="closed"):
                result = WORK_ITEM_SESSIONS.cmd_interrupted(cwd=directory)
        [entry] = result["interrupted"]["(unclaimed)"]
        self.assertEqual("2026-01-01T00:00:00.250000Z", entry["last_activity"])


if __name__ == "__main__":
    unittest.main()
