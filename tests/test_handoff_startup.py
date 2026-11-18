import contextlib
import importlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
import launcher_test_support


class HandoffStartupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="handoff startup é ")
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.prompt = self.directory / "prompt.txt"
        self.prompt.write_text("Continue the authorized source task.", encoding="utf-8")

    def run_launch(self, host, status, terminal_timeout=False):
        launcher = importlib.import_module(f"tj_agent.hosts.{host}.handoff")
        cli = importlib.import_module("tj_agent.sessions.launch")
        real_wait = cli.await_handoff_startup
        receipt = self.directory / f"{host}.json"

        def terminal(*args, **kwargs):
            arguments = kwargs["arguments"]
            self.assertEqual(
                str(receipt), arguments[arguments.index("--status-file") + 1]
            )
            self.assertIn("--require-session-start", arguments)
            if status is not None:
                receipt.write_text(json.dumps(status), encoding="utf-8")
            if terminal_timeout:
                raise cli.LaunchTimeout("terminal submission timed out")

        launch = mock.Mock(side_effect=terminal)
        output, error = io.StringIO(), io.StringIO()
        with (
            mock.patch.multiple(
                cli,
                resolve_codex_executable=mock.Mock(
                    return_value=launcher_test_support.CODEX_EXECUTABLE
                ),
                resolve_claude_executable=mock.Mock(return_value="/bin/claude"),
                launch_tab=launch,
                handoff_status_path=lambda: receipt,
                await_handoff_startup=lambda path: real_wait(
                    path, timeout=0, wrapper_timeout=0
                ),
            ),
            contextlib.redirect_stdout(output),
            contextlib.redirect_stderr(error),
        ):
            result = launcher.main(
                [
                    "--working-directory",
                    str(self.directory),
                    "--prompt-path",
                    str(self.prompt),
                    "--model",
                    "explicit-model",
                ]
            )
        launch.assert_called_once()
        return result, output.getvalue(), error.getvalue(), receipt

    def test_failed_refresh_never_emits_success_for_either_host(self):
        for host in ("codex", "claude"):
            with self.subTest(host=host):
                result, output, error, receipt = self.run_launch(
                    host, {"state": "failed", "error": "marketplace unavailable"}
                )
                self.assertEqual(1, result)
                self.assertIn("marketplace unavailable", error)
                self.assertIn(str(receipt), error)
                self.assertNotIn("Launched", output)
                self.assertNotIn('"startup": "verified"', output)

    def test_dispatcher_requires_host_and_forwards_only_the_host_arguments(self):
        dispatcher = importlib.import_module("tj_agent.sessions.handoff")
        handler = mock.Mock()
        handler.main.return_value = 7
        with mock.patch.dict(dispatcher.HANDLERS, {dispatcher.Host.CLAUDE: handler}):
            self.assertEqual(
                7,
                dispatcher.main(
                    ["--host", "claude", "--", "--lane", "L4", "--title", "exact"]
                ),
            )
        handler.main.assert_called_once_with(["--lane", "L4", "--title", "exact"])

    def test_dispatcher_refuses_unseparated_or_unsupported_host_before_loading(self):
        dispatcher = importlib.import_module("tj_agent.sessions.handoff")
        handlers = {host: mock.Mock() for host in dispatcher.Host}
        with mock.patch.dict(dispatcher.HANDLERS, handlers):
            with self.assertRaises(SystemExit):
                dispatcher.main(["--host", "claude", "--lane", "L4"])
            with self.assertRaises(SystemExit):
                dispatcher.main(["--host", "unknown", "--", "--lane", "L4"])
        for handler in handlers.values():
            handler.main.assert_not_called()

    def test_native_exit_before_confirmation_is_failure_even_with_zero_exit(self):
        for host in ("codex", "claude"):
            for returncode in (0, 2):
                with self.subTest(host=host, returncode=returncode):
                    result, output, error, _ = self.run_launch(
                        host, {"state": "exited", "returncode": returncode}
                    )
                    self.assertEqual(1, result)
                    self.assertIn("before handoff confirmation", error)
                    self.assertNotIn('"startup": "verified"', output)

    def test_uncertain_preparation_retains_receipt_and_never_retries(self):
        for host in ("codex", "claude"):
            with self.subTest(host=host):
                result, output, error, receipt = self.run_launch(
                    host, {"state": "preparing"}
                )
                self.assertEqual(3, result)
                self.assertEqual(
                    {"state": "preparing"},
                    json.loads(receipt.read_text(encoding="utf-8")),
                )
                self.assertIn(str(receipt), error)
                self.assertNotIn('"startup": "verified"', output)

    def test_live_session_without_acknowledgement_reports_uncertainty_without_retry(
        self,
    ):
        for host in ("codex", "claude"):
            with self.subTest(host=host):
                status = {"state": "unconfirmed", "child_pid": 4242}
                result, output, error, receipt = self.run_launch(host, status)
                self.assertEqual(3, result)
                self.assertEqual(
                    status, json.loads(receipt.read_text(encoding="utf-8"))
                )
                self.assertIn("still running", error)
                self.assertIn("do not relaunch", error)
                self.assertIn(str(receipt), error)
                self.assertNotIn('"startup": "verified"', output)

    def test_missing_wrapper_is_abandoned_atomically_for_either_host(self):
        for host in ("codex", "claude"):
            with self.subTest(host=host):
                result, output, error, receipt = self.run_launch(host, None)
                self.assertEqual(1, result)
                self.assertEqual(
                    "abandoned",
                    json.loads(receipt.read_text(encoding="utf-8"))["state"],
                )
                self.assertIn("late launch is blocked", error)
                self.assertNotIn('"startup": "verified"', output)

    def test_verified_native_startup_is_bound_to_the_exact_attempt(self):
        for host in ("codex", "claude"):
            with self.subTest(host=host):
                result, output, error, receipt = self.run_launch(
                    host, {"state": "running"}
                )
                self.assertEqual((0, ""), (result, error))
                event = json.loads(output.splitlines()[-1])
                self.assertEqual("verified", event["startup"])
                self.assertEqual(str(receipt), event["status_path"])
                self.assertEqual(str(self.prompt.resolve()), event["prompt_path"])

    def test_wrapper_claim_wins_abandonment_race_without_late_success(self):
        cli = importlib.import_module("tj_agent.sessions.launch")
        receipt = self.directory / "race.json"

        class Status:
            @staticmethod
            def read(path):
                return None

            @staticmethod
            def create(path, value):
                return False

        with mock.patch.object(cli, "status_file", Status):
            with self.assertRaises(cli.LaunchTimeout):
                cli.await_handoff_startup(receipt, timeout=0, wrapper_timeout=0)

    def test_clean_exit_retains_verified_session_start_without_polling_race(self):
        for host in ("codex", "claude"):
            with self.subTest(host=host):
                result, output, error, _ = self.run_launch(
                    host, {"state": "exited", "returncode": 0, "running_at": 100}
                )
                self.assertEqual((0, ""), (result, error))
                self.assertIn('"startup": "verified"', output)

    def test_terminal_submission_timeout_keeps_exact_attempt_receipt(self):
        for host in ("codex", "claude"):
            with self.subTest(host=host):
                result, output, error, receipt = self.run_launch(
                    host, {"state": "preparing"}, terminal_timeout=True
                )
                self.assertEqual(3, result)
                self.assertIn(str(receipt), error)
                self.assertTrue(receipt.is_file())
                self.assertNotIn('"startup": "verified"', output)
