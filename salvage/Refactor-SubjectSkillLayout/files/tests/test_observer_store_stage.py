import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tj_agent.sessions import session_close


class ObserverPackageStageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="observer packages ")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.staging = self.root / "staging"
        self.staging.mkdir()
        self.output = self.root / "context.json"
        self.probe = self.root / "probe.py"
        self.probe.write_text(
            "import json, os, sys\nfrom pathlib import Path\n"
            "import tj_agent_store.sessions\nimport tj_cli.terminal\n"
            "Path(sys.argv[1]).write_text(json.dumps({"
            "'store': tj_agent_store.sessions.__file__, "
            "'terminal': str(tj_cli.terminal.windows_adapter_path()), "
            "'context': {key: os.environ.get(key) for key in "
            "('AGENT_STATE_DIRECTORY', 'CODEX_HOME', 'CLAUDE_CONFIG_DIR')}}))\n",
            encoding="utf-8",
        )
        self.script, self.arguments, self.launcher = session_close.stage_observer(
            self.staging, self.probe, [str(self.output)], ()
        )

    def launch(self) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                sys.executable,
                "-I",
                "-B",
                str(self.launcher),
                "--state-directory",
                str(self.root / "state"),
                "--codex-home",
                str(self.root / "codex"),
                "--claude-config-dir",
                str(self.root / "claude"),
                "--",
                str(self.script),
                *self.arguments,
            ],
            capture_output=True,
            text=True,
            timeout=20,
        )

    def test_staged_closure_imports_store_and_cli_without_producer_search(self) -> None:
        result = self.launch()
        self.assertEqual(0, result.returncode, result.stderr)
        value = json.loads(self.output.read_text(encoding="utf-8"))
        for key in ("store", "terminal"):
            self.assertTrue(Path(value[key]).is_relative_to(self.staging / "python"))
        self.assertEqual(
            {
                "AGENT_STATE_DIRECTORY": str(self.root / "state"),
                "CODEX_HOME": str(self.root / "codex"),
                "CLAUDE_CONFIG_DIR": str(self.root / "claude"),
            },
            value["context"],
        )

    def test_tampered_or_unreceipted_package_refuses_before_callback(self) -> None:
        for target in (
            self.staging / "python/tj_agent_store/sessions.py",
            self.staging / "extra.py",
        ):
            with self.subTest(target=target.name):
                original = target.read_bytes() if target.exists() else None
                target.write_text(
                    "raise AssertionError('tampered code ran')\n", encoding="utf-8"
                )
                result = self.launch()
                self.assertNotEqual(0, result.returncode)
                self.assertIn("detached callback closure changed", result.stderr)
                self.assertFalse(self.output.exists())
                if original is None:
                    target.unlink()
                else:
                    target.write_bytes(original)

    def test_packaged_recovery_path_is_rebound_into_staged_closure(self) -> None:
        from tj_agent import entry

        assert entry.__file__ is not None
        staging = self.root / "relaunch"
        staging.mkdir()
        _, arguments, _ = session_close.stage_observer(
            staging, self.probe, ["--recovery", entry.__file__], ()
        )
        self.assertEqual(str(staging / "python/tj_agent/entry.py"), arguments[1])

    def test_scheduler_command_passes_only_owned_detached_context(self) -> None:
        with mock.patch.dict(
            os.environ,
            {
                "AGENT_STATE_DIRECTORY": str(self.root / "state"),
                "CODEX_HOME": str(self.root / "codex"),
                "CLAUDE_CONFIG_DIR": str(self.root / "claude"),
                "UNRELATED": "secret",
            },
            clear=True,
        ):
            command = session_close.detached_command(
                self.script, ["--request", "exact"], self.launcher
            )
        self.assertIn("--state-directory", command)
        self.assertIn("--codex-home", command)
        self.assertIn("--claude-config-dir", command)
        self.assertNotIn("UNRELATED", " ".join(command))


if __name__ == "__main__":
    unittest.main()
