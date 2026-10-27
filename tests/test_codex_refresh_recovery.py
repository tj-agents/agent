import argparse
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest import mock
import shutil


ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "plugins/agent/.agents/agent/runtime"
sys.path.insert(0, str(RUNTIME))
sys.path.insert(0, str(ROOT / "plugins/agent/.agents/agent/utility/peer/scripts"))
import agent_cli  # noqa: E402
from claude import sessions as CLAUDE_SESSIONS  # noqa: E402
from codex import plugins as CODEX  # noqa: E402
from codex import sessions as CODEX_SESSIONS  # noqa: E402
from host import Host  # noqa: E402


def load(name):
    spec = importlib.util.spec_from_file_location(name, RUNTIME / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class CodexRefreshRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.relaunch = load("session_relaunch")
        self.watcher = load("relaunch_watcher")
        self.recovery = load("agent_recovery")
        self.refresh = load("reload_plugins")
        caller = mock.patch.object(self.refresh, "calling_host", return_value=Host.CODEX)
        caller.start()
        self.addCleanup(caller.stop)
        temporary = tempfile.TemporaryDirectory(prefix="codex-refresh-recovery-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        environment = mock.patch.dict(os.environ, {"AGENT_STATE_DIRECTORY": str(self.root / "state")})
        environment.start()
        self.addCleanup(environment.stop)

    def args(self, record, token="token"):
        return argparse.Namespace(host="codex", session_id="thread", pid=17, pid_started_at=3.0,
                                  record=record, token=token, timeout=1.0, history_root=self.root,
                                  recovery=self.root / "agent_recovery.py", title="Codex", prompt="continue",
                                  native_argument=["--profile", "work"], expected_cwd=self.root)

    def test_codex_options_preserve_safe_native_configuration(self):
        options = CODEX_SESSIONS.native_arguments([
            "initial prompt", "--profile", "work", "--model=gpt-6", "--no-daemon", "--sandbox", "read-only", "--config", "model=fast",
            "resume", "old-thread", "ignored prompt",
        ])
        self.assertEqual(["--profile", "work", "--model=gpt-6", "--no-daemon", "--sandbox", "read-only", "--config", "model=fast"], options)

    def test_codex_options_refuse_hook_trust_bypass(self):
        with self.assertRaisesRegex(RuntimeError, "hook-trust bypass"):
            CODEX_SESSIONS.native_arguments(["--dangerously-bypass-hook-trust"])

    def test_compact_and_resume_last_options_preserve_safe_settings(self):
        self.assertEqual(["-sread-only", "-aon-request", "-mchosen", "-cmodel_reasoning_effort=high"],
                         CODEX_SESSIONS.native_arguments(["resume", "thread", "-sread-only", "-aon-request",
                                                        "-mchosen", "-cmodel_reasoning_effort=high", "old prompt"]))
        self.assertEqual(["--no-daemon", "-sread-only"],
                         CODEX_SESSIONS.native_arguments(["resume", "--last", "--no-daemon", "-sread-only", "old prompt"]))

    def test_cancelled_or_changed_close_never_relaunches(self):
        request = self.root / "request.json"
        request.write_text('{"exact": true}', encoding="utf-8")
        result = self.root / "result.json"
        result.write_text(json.dumps({"status": "cancelled", "session_id": "thread"}), encoding="utf-8")
        record = self.root / "record.json"
        record.write_text(json.dumps({"state": "waiting", "token": "token", "close": {
            "request": str(request), "request_sha256": "0" * 64, "result": str(result),
        }}), encoding="utf-8")
        args = self.args(record)
        self.assertFalse(self.watcher.wait_for_exit(args, exited=lambda *_: True, clock=lambda: 0, sleep=lambda _: None))
        state = json.loads(record.read_text(encoding="utf-8"))
        self.assertEqual("cancelled", state["state"])

    def test_closed_exact_request_requires_the_original_process_to_exit(self):
        request = self.root / "request.json"
        raw = b'{"exact": true}'
        request.write_bytes(raw)
        result = self.root / "result.json"
        result.write_text(json.dumps({"status": "closed", "session_id": "thread"}), encoding="utf-8")
        import hashlib
        record = self.root / "record.json"
        record.write_text(json.dumps({"state": "waiting", "token": "token", "close": {
            "request": str(request), "request_sha256": hashlib.sha256(raw).hexdigest(), "result": str(result),
        }}), encoding="utf-8")
        args = self.args(record)
        calls = []
        self.assertTrue(self.watcher.wait_for_exit(args, exited=lambda *identity: calls.append(identity) or True,
                                                   clock=lambda: 0, sleep=lambda _: None))
        self.assertEqual([(17, 3.0)], calls)

    def test_watcher_sleeps_while_the_exact_close_is_still_pending(self):
        request = self.root / "request.json"
        raw = b'{"exact": true}'
        request.write_bytes(raw)
        record = self.root / "record.json"
        import hashlib
        record.write_text(json.dumps({"state": "waiting", "token": "token", "close": {
            "request": str(request), "request_sha256": hashlib.sha256(raw).hexdigest(),
            "result": str(self.root / "not-yet.json"),
        }}), encoding="utf-8")
        args = self.args(record)
        args.timeout = 0.1
        ticks = iter((0.0, 0.0, 1.0))
        sleeps = []
        self.assertFalse(self.watcher.wait_for_exit(args, clock=lambda: next(ticks), sleep=sleeps.append))
        self.assertEqual([self.watcher.POLL_SECONDS], sleeps)

    def test_existing_waiting_generation_is_refused_without_overwrite(self):
        record = self.root / "record.json"
        original = {"state": "waiting", "token": "first"}
        record.write_text(json.dumps(original), encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "already armed"):
            self.relaunch.claim_record(record, {"state": "waiting", "token": "second"})
        self.assertEqual(original, json.loads(record.read_text(encoding="utf-8")))

    def test_simultaneous_claims_publish_exactly_one_complete_generation(self):
        record = self.root / "record.json"
        barrier = threading.Barrier(2)
        outcomes = []

        def claim(token):
            barrier.wait()
            try:
                self.relaunch.claim_record(record, {"state": "waiting", "token": token})
                outcomes.append((token, "claimed"))
            except RuntimeError:
                outcomes.append((token, "refused"))

        workers = [threading.Thread(target=claim, args=(token,)) for token in ("first", "second")]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join()
        self.assertEqual(["claimed", "refused"], sorted(outcome for _, outcome in outcomes))
        self.assertIn(json.loads(record.read_text(encoding="utf-8"))["token"], {"first", "second"})

    def test_partial_terminal_record_is_replaced_under_the_claim_lock(self):
        record = self.root / "record.json"
        record.write_text('{"state":', encoding="utf-8")
        self.relaunch.claim_record(record, {"state": "waiting", "token": "fresh"})
        self.assertEqual({"state": "waiting", "token": "fresh"}, json.loads(record.read_text(encoding="utf-8")))

    def test_watcher_passes_expected_checkout_to_the_actual_open(self):
        record = self.root / "record.json"
        record.write_text(json.dumps({"state": "waiting", "token": "token", "cwd": str(self.root)}), encoding="utf-8")
        command = self.watcher.launch_command(self.args(record))
        self.assertEqual(str(self.root), command[command.index("--expected-cwd") + 1])

    def test_claude_watcher_requires_the_same_exact_close_result(self):
        request = self.root / "request.json"
        raw = b'{"exact": true}'
        request.write_bytes(raw)
        result = self.root / "result.json"
        result.write_text(json.dumps({"status": "closed", "session_id": "thread"}), encoding="utf-8")
        import hashlib
        record = self.root / "claude.json"
        record.write_text(json.dumps({"state": "waiting", "token": "token", "cwd": str(self.root), "close": {
            "request": str(request), "request_sha256": hashlib.sha256(raw).hexdigest(), "result": str(result),
        }}), encoding="utf-8")
        args = self.args(record)
        args.host = "claude"
        self.assertTrue(self.watcher.wait_for_exit(args, exited=lambda *_: True, clock=lambda: 0, sleep=lambda _: None))

    def test_claude_relaunch_arms_the_shared_exact_close_protocol(self):
        entry = {"host": "claude", "session_id": "thread", "pid": 17, "pid_started_at": 3.0,
                 "profile": str(self.root / "profile"), "title": "Claude"}
        shared = mock.Mock()
        shared.own_host_and_entry.return_value = entry
        close = {"request": str(self.root / "request"), "request_sha256": "a" * 64,
                 "result": str(self.root / "result"), "cancel": str(self.root / "cancel")}
        with mock.patch.object(agent_cli, "resolve_claude_executable", return_value="claude"), \
                mock.patch.object(self.relaunch, "cli_close_capability", return_value=self.root / "process_identity.py"), \
                mock.patch.object(self.relaunch, "arm_client_close", return_value=close) as arm:
            result = self.relaunch.relaunch(self.root, {}, shared=shared, host=Host.CLAUDE, arguments=lambda _: ["--model", "chosen"])
        self.assertEqual("armed", result["relaunch"])
        self.assertEqual(Host.CLAUDE, arm.call_args.args[-1])
        self.assertEqual(close, self.relaunch.read_json(self.relaunch.record_path("thread"))["close"])

    def test_codex_relaunch_uses_native_checkout_not_the_launch_directory(self):
        entry = {"host": "codex", "session_id": "thread", "pid": 17, "pid_started_at": 3.0,
                 "launch_cwd": str(self.root / "original"), "native_cwd": str(self.root),
                 "profile": str(self.root / "profile"), "title": "Codex"}
        shared = mock.Mock()
        shared.own_host_and_entry.return_value = entry
        close = {"request": str(self.root / "request"), "request_sha256": "a" * 64,
                 "result": str(self.root / "result"), "cancel": str(self.root / "cancel")}
        with mock.patch.object(agent_cli, "resolve_codex_executable", return_value=("codex", [])), \
                mock.patch.object(self.relaunch.required_plugins, "check_launch"), \
                mock.patch.object(self.relaunch, "cli_close_capability", return_value=self.root / "close.py"), \
                mock.patch.object(self.relaunch, "arm_client_close", return_value=close):
            result = self.relaunch.relaunch(self.root, {}, shared=shared, host=Host.CODEX, arguments=lambda _: [])
        self.assertEqual("armed", result["relaunch"])

    def test_codex_relaunch_refuses_missing_or_different_native_checkout(self):
        for native_cwd in (None, "", "relative", str(self.root / "other")):
            with self.subTest(native_cwd=native_cwd):
                entry = {"host": "codex", "session_id": "thread", "pid": 17, "pid_started_at": 3.0,
                         "launch_cwd": str(self.root), "native_cwd": native_cwd}
                shared = mock.Mock()
                shared.own_host_and_entry.return_value = entry
                with self.assertRaisesRegex(RuntimeError, "checkout"):
                    self.relaunch.relaunch(self.root, {}, shared=shared, host=Host.CODEX, arguments=lambda _: [])
                shared.start_detached.assert_not_called()

    def test_cli_close_uses_enabled_native_inventory_not_repository_selection(self):
        cli = self.root / "cli"
        process_identity = cli / ".agents/cli/utility/close/scripts/process_identity.py"
        process_identity.parent.mkdir(parents=True)
        process_identity.write_text("# capability\n", encoding="utf-8")
        with mock.patch.object(self.relaunch.required_plugins, "check", side_effect=AssertionError("selection filter used")), \
                mock.patch.object(self.relaunch.required_plugins, "inventory_roots", return_value={"cli@tj-agents": cli}):
            self.assertEqual(process_identity, self.relaunch.cli_close_capability(self.root, Host.CODEX, "codex"))

    def test_claude_tool_restrictions_and_aliases_survive_recovery(self):
        self.assertEqual([
            "--tools", "Read", "--disallowedTools", "Bash,Write", "--allowed-tools=Read,Edit",
            "--restricted", "--dangerously-skip-permissions",
        ], CLAUDE_SESSIONS.native_arguments([
            "initial prompt", "--tools", "Read", "--disallowedTools", "Bash,Write",
            "--allowed-tools=Read,Edit", "--restricted", "--dangerously-skip-permissions",
        ]))
        entry = {"host": "claude", "session_id": "thread", "pid": 17, "pid_started_at": 3.0,
                 "profile": str(self.root / "profile"), "title": "Claude"}
        shared = mock.Mock()
        shared.own_host_and_entry.return_value = entry
        with self.assertRaisesRegex(RuntimeError, "tool restrictions"):
            self.relaunch.relaunch(self.root, {}, shared=shared, host=Host.CLAUDE, arguments=lambda _: ["--tools"])
        shared.start_detached.assert_not_called()
        self.assertFalse(self.relaunch.record_path("thread").exists())

    def test_claude_argument_delimiter_never_promotes_literal_prompt_tokens(self):
        self.assertEqual([], CLAUDE_SESSIONS.native_arguments([]))
        self.assertEqual([], CLAUDE_SESSIONS.native_arguments(["--", "--dangerously-skip-permissions"]))
        self.assertEqual([], CLAUDE_SESSIONS.native_arguments(["--", "--tools", "Read"]))
        self.assertEqual(["--tools", "Read"], CLAUDE_SESSIONS.native_arguments([
            "--tools", "Read", "--", "--dangerously-skip-permissions",
        ]))

    def run_refresh(self, failure=None, extra=(), setup_errors=()):
        output = io.StringIO()
        with mock.patch.object(self.refresh.launch_check, "check", return_value="current"), \
                mock.patch.object(CODEX, "ensure_hooks", return_value="unloaded daemon", side_effect=failure), \
                mock.patch.object(self.refresh.required_plugins, "converge_setup", return_value=list(setup_errors)), \
                mock.patch.object(CODEX.session_relaunch, "relaunch",
                                  return_value={"relaunch": "armed", "session_id": "thread"}) as restart, \
                contextlib.redirect_stdout(output), contextlib.redirect_stderr(io.StringIO()):
            code = self.refresh.main(["--host", "codex", "--project", str(self.root), *extra])
        return code, json.loads(output.getvalue()), restart

    def test_unloaded_daemon_automatically_arms_same_session_recovery(self):
        code, report, restart = self.run_refresh()
        self.assertEqual(0, code)
        self.assertEqual("armed", report["codex"]["relaunch"])
        restart.assert_called_once()

    def test_untrusted_hook_failure_never_becomes_a_restart(self):
        code, report, restart = self.run_refresh(RuntimeError("required hooks modified"))
        self.assertEqual(1, code)
        self.assertIn("modified", report["hook_readiness_error"])
        restart.assert_not_called()

    def test_setup_failure_and_no_relaunch_prevent_client_closure(self):
        code, _, restart = self.run_refresh(setup_errors=["setup failed"])
        self.assertEqual(1, code)
        restart.assert_not_called()
        code, report, restart = self.run_refresh(extra=["--no-relaunch"])
        self.assertEqual(1, code)
        self.assertIn("--no-relaunch", report["hook_readiness_error"])
        restart.assert_not_called()

    def test_verify_only_never_arms_recovery(self):
        with mock.patch.object(CODEX, "resolve_executable", return_value="native"), \
                mock.patch.object(self.refresh, "selection", return_value=({}, [])), \
                mock.patch.object(self.refresh, "verify", return_value={"host": "codex", "problems": []}), \
                mock.patch.object(self.refresh.required_plugins, "check_setup", return_value=[]), \
                mock.patch.object(CODEX.session_relaunch, "relaunch") as restart, \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(0, self.refresh.main(["--host", "codex", "--verify-only", "--project", str(self.root)]))
        restart.assert_not_called()

    def test_full_refresh_uses_the_same_recovery_path(self):
        with mock.patch.object(CODEX, "resolve_executable", return_value="native"), \
                mock.patch.object(self.refresh, "selection", return_value=({}, [])), \
                mock.patch.object(self.refresh, "refresh", return_value=[]), \
                mock.patch.object(self.refresh, "verify", return_value={"host": "codex", "problems": []}), \
                mock.patch.object(self.refresh.required_plugins, "converge_setup", return_value=[]), \
                mock.patch.object(CODEX, "ensure_hooks", return_value="restart"), \
                mock.patch.object(CODEX.session_relaunch, "relaunch", return_value={"relaunch": "armed"}) as restart, \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(0, self.refresh.main(["--host", "codex", "--verify", "--project", str(self.root)]))
        restart.assert_called_once_with(self.root, {}, host=Host.CODEX)

    def test_full_refresh_does_not_restart_a_foreign_codex_client(self):
        with mock.patch.object(CODEX, "resolve_executable", return_value="native"), \
                mock.patch.object(self.refresh, "selection", return_value=({}, [])), \
                mock.patch.object(self.refresh, "refresh", return_value=[]), \
                mock.patch.object(self.refresh, "verify", return_value={"host": "codex", "problems": []}), \
                mock.patch.object(self.refresh.required_plugins, "converge_setup", return_value=[]), \
                mock.patch.object(self.refresh, "calling_host", return_value=Host.CLAUDE), \
                mock.patch.object(CODEX, "ensure_hooks", return_value="restart") as readiness, \
                mock.patch.object(CODEX.session_relaunch, "relaunch") as restart, \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(0, self.refresh.main(["--host", "codex", "--verify", "--project", str(self.root)]))
        readiness.assert_called_once_with(self.root, "native", running_session=False)
        restart.assert_not_called()

    def test_actual_open_item_must_match_expected_checkout_before_launch(self):
        item = {"status": "closed", "cwd": str(self.root / "other"), "profile": str(self.root)}
        with mock.patch.object(self.recovery, "find", return_value=item), \
                mock.patch.object(self.recovery, "_claim") as claim:
            with self.assertRaisesRegex(ValueError, "expected refresh checkout"):
                self.recovery.open_session("codex", "thread", expected_cwd=self.root)
        claim.assert_not_called()

    def test_peer_runtime_accepts_a_nearest_receipted_agent_root(self):
        import session_close

        staged = self.root / "managed"
        runtime = staged / ".agents/agent/runtime"
        peer = staged / ".agents/agent/utility/peer/scripts/session_close.py"
        runtime.mkdir(parents=True)
        peer.parent.mkdir(parents=True)
        shutil.copyfile(RUNTIME / "package_root.py", runtime / "package_root.py")
        peer.touch()
        (staged / "source.json").write_text(json.dumps({
            "repository": "tj-agents/agent", "revision": "native:fixture",
            "exports": [".agents/agent/runtime", ".agents/agent/utility/peer/scripts"], "sha256": "0" * 64,
            "files": {".agents/agent/runtime/package_root.py": "0" * 64},
        }), encoding="utf-8")
        with mock.patch.object(session_close, "__file__", str(peer)):
            self.assertEqual(staged, session_close._package_root())


if __name__ == "__main__":
    unittest.main()
