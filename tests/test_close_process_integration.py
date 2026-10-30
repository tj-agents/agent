"""Explicit CLI dependency integration uses only disposable Python children."""

import json
import os
from pathlib import Path
import subprocess
import sys
import unittest

from close_fixtures import CLOSE, CloseFixture, register_session, session_close


@unittest.skipUnless(os.environ.get("AGENT_TEST_CLI_CLOSE"), "set AGENT_TEST_CLI_CLOSE to a declared CLI capability")
class DisposableCloseIntegration(CloseFixture, unittest.TestCase):
    def setUp(self):
        self.setup_close("claude")
        self.capability = Path(os.environ["AGENT_TEST_CLI_CLOSE"])
        cli_root = CLOSE.cli_capability_root(self.capability)
        primitive = session_close.process_identity_runtime(cli_root)
        self.children = [subprocess.Popen([sys.executable, "-B", "-c", "import time; time.sleep(120)"],
                                           cwd=self.root) for _ in range(2)]
        self.addCleanup(self.cleanup_children)
        info = primitive.process_info(self.children[0].pid)
        self.assertIsNotNone(info)
        self.infos[info.pid] = register_session.ProcessInfo(info.pid, info.ppid, "claude.exe", info.started_at)
        entry = json.loads(self.registry.read_text(encoding="utf-8"))
        frontend = {"pid": info.pid, "started_at": info.started_at}
        entry.update({"pid": info.pid, "pid_started_at": info.started_at, "frontend": frontend})
        self.write(self.registry, entry)
        receipt = Path(entry["provenance"]["receipt"])
        value = json.loads(receipt.read_text(encoding="utf-8"))
        value["wrapper"]["child"] = frontend
        self.write(receipt, value)
        self.store.update_session(
            self.handle, lambda session: session.attach_client(info.pid, info.started_at, self.root),
        )

    def cleanup_children(self):
        for process in self.children:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=10)

    def test_actual_cli_guard_refusal_preserves_children_then_closes_only_selected_frontend(self):
        entry = json.loads(self.registry.read_text(encoding="utf-8"))
        def cancelled():
            raise session_close.Refusal("cancelled at signal")
        with self.assertRaisesRegex(session_close.Refusal, "cancelled at signal"):
            session_close.stop_cli_client(entry, cancelled, CLOSE.cli_capability_root(self.capability))
        self.assertTrue(all(child.poll() is None for child in self.children))
        self.assertIsNone(self.row().ended_at)
        self.arm()
        self.stop()
        self.assertEqual(0, self.wait())
        self.children[0].wait(timeout=10)
        self.assertIsNone(self.children[1].poll())
        self.assertTrue(self.root.is_dir())
        self.assertEqual("closed", self.row().end_reason)


if __name__ == "__main__":
    unittest.main()
