import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "plugins/agent/.agents/agent/runtime"
sys.path.insert(0, str(RUNTIME))
SPEC = importlib.util.spec_from_file_location("agent_launch_check_tests", RUNTIME / "launch_check.py")
CHECK = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CHECK)
from host import Host  # noqa: E402


class LaunchCheckExpiryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="agent-launch-check-")
        self.addCleanup(temporary.cleanup)
        self.state = Path(temporary.name)
        (self.state / "machine-events.json").write_text(json.dumps({"topic": "t" * 40}), encoding="utf-8")
        self.cursor = self.state / "machine-events-cursor.json"
        self.cursor.write_text(json.dumps({"since": "old", "since_at": 0}), encoding="utf-8")
        environment = mock.patch.dict(os.environ, {
            "AGENT_STATE_DIRECTORY": str(self.state),
            "TJ_AGENTS_INSTALL_RECOVERY": "1",
        })
        environment.start()
        self.addCleanup(environment.stop)

    def check(self, outcomes):
        calls = []

        def update(host, repositories, _project):
            calls.append((host, repositories))
            return outcomes.get(host, True)

        with mock.patch.object(sys, "stderr"):
            result = CHECK.check(self.state, fetch=lambda _path: b"", update=update,
                                 now=CHECK.EXPIRY_SECONDS + 1)
        return result, calls

    def test_expired_empty_poll_forces_full_comparison_and_advances_from_existing_cursor(self):
        result, calls = self.check({})
        self.assertEqual("updated", result)
        self.assertEqual({(Host.CLAUDE, None), (Host.CODEX, None)}, set(calls))
        self.assertEqual({"since": "old", "since_at": CHECK.EXPIRY_SECONDS + 1},
                         json.loads(self.cursor.read_text(encoding="utf-8")))

    def test_failed_expired_empty_poll_preserves_the_old_cursor_for_retry(self):
        result, calls = self.check({Host.CODEX: False})
        self.assertEqual("failed", result)
        self.assertEqual({(Host.CLAUDE, None), (Host.CODEX, None)}, set(calls))
        saved = json.loads(self.cursor.read_text(encoding="utf-8"))
        self.assertEqual("old", saved["since"])
        self.assertEqual({"applied": "old"}, saved["hosts"]["claude"])
        self.assertEqual({"applied": None}, saved["hosts"]["codex"])


if __name__ == "__main__":
    unittest.main()
