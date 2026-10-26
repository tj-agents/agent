from contextlib import ExitStack
import io
import json
import os
from pathlib import Path
from types import SimpleNamespace
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
REAPER = ROOT / ".agents/agent/utility/peer/scripts/finish_reaper.py"
CLOSE = ROOT / ".agents/agent/utility/peer/scripts/close.py"
sys.path.insert(0, str(REAPER.parent))
import session_close
import finish_reaper
import close as own_close


class PeerCloseSafetyTests(unittest.TestCase):
    def setUp(self):
        environment = mock.patch.dict(os.environ, {'CODEX_THREAD_ID': '', 'CLAUDE_CODE_SESSION_ID': ''})
        environment.start()
        self.addCleanup(environment.stop)

    def test_unregistered_attachment_refuses_an_unsafe_close(self):
        host = session_close.register_session.ProcessInfo(10, 1, "claude.exe", 20.0)
        with mock.patch.object(session_close.os, "getppid", return_value=10), \
                mock.patch.object(session_close, "process_info", return_value=host), \
                mock.patch.object(session_close.register_session, "read_host_names", return_value={"claude"}), \
                mock.patch.object(session_close, "entries", return_value=[]):
            with self.assertRaisesRegex(session_close.Refusal, "do not force-close the host"):
                session_close.own_host_and_entry(Path.cwd())

    def test_attachment_resolves_under_the_launch_directory_after_the_registry_moved(self):
        host = session_close.register_session.ProcessInfo(10, 1, "claude.exe", 20.0)
        launch = Path.cwd().parent
        entry = {"host": "claude", "session_id": "own", "pid": 10, "pid_started_at": 20.0, "started_at": 30.0,
                 "cwd": str(launch / "elsewhere"), "launch_cwd": str(launch)}
        with mock.patch.object(session_close.os, "getppid", return_value=10), \
                mock.patch.object(session_close, "process_info", return_value=host), \
                mock.patch.object(session_close, "verified_live", return_value=True), \
                mock.patch.object(session_close.register_session, "read_host_names", return_value={"claude"}):
            with mock.patch.object(session_close, "entries", return_value=[("own.json", entry)]):
                self.assertEqual(entry, session_close.own_host_and_entry(Path.cwd()))
            moved_only = {key: value for key, value in entry.items() if key != "launch_cwd"}
            with mock.patch.object(session_close, "entries", return_value=[("own.json", moved_only)]), \
                    self.assertRaisesRegex(session_close.Refusal, "cannot resolve a verified registry attachment"):
                session_close.own_host_and_entry(Path.cwd())

    def test_terminal_refusal_preserves_host_and_wrapper(self):
        entry = {"pid": 30, "pid_started_at": 3.0}
        wrapper = {"pid": 20, "started_at": 2.0}
        for refusal in (
                own_close.close_tab.TerminalUnavailable("unavailable"),
                own_close.close_tab.TerminalRefusal("absent or ambiguous"),
        ):
            for module in (own_close,):
                for mode in ("", "process"):
                    with self.subTest(module=module.__name__, refusal=type(refusal).__name__, mode=mode), \
                            mock.patch.dict(os.environ, {"AGENT_FINISH_CLOSE_MODE": mode}), \
                            mock.patch.object(session_close, "verified_live", return_value=True), \
                            mock.patch.object(module.close_tab, "close_entry", side_effect=refusal), \
                            mock.patch.object(module.os, "kill") as killer:
                        with self.assertRaises(type(refusal)):
                            module.close_host(entry, wrapper)
                        killer.assert_not_called()

    def test_windows_terminal_absent_or_ambiguous_target_preserves_host(self):
        entry = {"pid": 30, "pid_started_at": 3.0, "title": "own",
                 "terminal": {"kind": "windows-terminal", "runtime_id": "tab-1"}}
        target = {"title": "own", "identity": entry["terminal"]}
        for inventory in ([], [target, target]):
            for module in (own_close,):
                with self.subTest(module=module.__name__, inventory=inventory), \
                        mock.patch.object(session_close, "verified_live", return_value=True), \
                        mock.patch.object(session_close, "liveness", return_value=True), \
                        mock.patch.object(module.close_tab, "is_windows", return_value=True), \
                        mock.patch.object(module.close_tab, "windows_inventory", return_value=inventory), \
                        mock.patch.object(module.os, "kill") as killer:
                    with self.assertRaisesRegex(module.close_tab.TerminalRefusal, "absent or ambiguous"):
                        module.close_host(entry)
                    killer.assert_not_called()

    def test_verified_terminal_close_never_signals_a_process(self):
        entry = {"pid": 30, "pid_started_at": 3.0}
        for module in (own_close,):
            with self.subTest(module=module.__name__), \
                    mock.patch.object(session_close, "verified_live", return_value=True), \
                    mock.patch.object(module.close_tab, "close_entry") as closer, \
                    mock.patch.object(module.os, "kill") as killer:
                module.close_host(entry)
                closer.assert_called_once_with(entry)
                killer.assert_not_called()

    def test_unknown_host_refuses_before_terminal_close_or_signal(self):
        entry = {"pid": 30, "pid_started_at": 3.0}
        for module in (own_close,):
            with self.subTest(module=module.__name__), \
                    mock.patch.object(session_close, "verified_live", return_value=False), \
                    mock.patch.object(module.close_tab, "close_entry") as closer, \
                    mock.patch.object(module.os, "kill") as killer:
                with self.assertRaisesRegex(session_close.Refusal, "identity changed before close"):
                    module.close_host(entry)
                closer.assert_not_called()
                killer.assert_not_called()


class CloseRefusalCaptureTests(unittest.TestCase):
    def test_missing_native_attachment_cannot_produce_target_evidence(self):
        with tempfile.TemporaryDirectory() as state:
            for module in (own_close,):
                with self.subTest(command=module.__name__), \
                        mock.patch.dict(os.environ, {"AGENT_STATE_DIRECTORY": state}), \
                        mock.patch.object(session_close, "own_host_and_entry",
                                          side_effect=session_close.Refusal("attachment unavailable")):
                    with self.assertRaisesRegex(session_close.Refusal, "attachment unavailable"):
                        module.main([])
                    self.assertFalse((Path(state) / "merge-cleanup" / "close-attempts").exists())

    def test_failed_evidence_write_preserves_the_original_close_refusal(self):
        entry = {"session_id": "ours", "cwd": str(ROOT), "host": "codex", "pid": 123,
                 "pid_started_at": 1.0, "started_at": 1.0}
        for module in (own_close,):
            with self.subTest(command=module.__name__), \
                    mock.patch.object(session_close, "own_host_and_entry", return_value=entry), \
                    mock.patch.object(module, "close_session", side_effect=session_close.Refusal("original refusal")), \
                    mock.patch.object(session_close, "write_json", side_effect=PermissionError("denied")):
                with self.assertRaisesRegex(session_close.Refusal, "original refusal"):
                    module.main([])


@unittest.skipUnless(sys.platform == "linux" or os.name == "nt", "Supported process backends")
class FinishReaperProcessTests(unittest.TestCase):
    def test_staged_observer_can_load_the_session_store(self):
        import importlib.util

        def scheduler(arguments, **kwargs):
            stdout = '"EXAMPLE\\user","S-1-5-21-123-456-789-1001"\r\n' if arguments[0] == "whoami.exe" else ""
            return SimpleNamespace(returncode=0, stdout=stdout, stderr="")

        with tempfile.TemporaryDirectory() as temporary, \
                mock.patch.dict(os.environ, {"AGENT_STATE_DIRECTORY": temporary}), \
                mock.patch.object(session_close.subprocess, "run", side_effect=scheduler), \
                mock.patch.object(session_close.subprocess, "Popen", return_value=SimpleNamespace(pid=1)):
            session_close.start_observer(["--result", "unused"])
            staging = next((Path(temporary) / "merge-cleanup" / "observers").iterdir())
            spec = importlib.util.spec_from_file_location("staged_register_session", staging / "register_session.py")
            staged = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(staged)
            store = staged._load_session_store()
        self.assertIsNotNone(store)
        self.assertTrue(hasattr(store, "delete_session"))

    def test_close_entrypoint_records_verified_session_exit_without_removing_checkout(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            worktree = root / "worktree"
            state = root / "state"
            host_executable, host_name = self.fixture_host(root)
            environment = {
                key: value for key, value in os.environ.items()
                if not key.startswith("GIT_CONFIG_")
            }
            subprocess.run(["git", "init", "-q", "-b", "main", str(worktree)], check=True, env=environment)
            subprocess.run(["git", "-C", str(worktree), "config", "user.email", "t@example.com"], check=True, env=environment)
            subprocess.run(["git", "-C", str(worktree), "config", "user.name", "t"], check=True, env=environment)
            (worktree / "base").write_text("base\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(worktree), "add", "."], check=True, env=environment)
            subprocess.run(["git", "-C", str(worktree), "commit", "-q", "-m", "base"], check=True, env=environment)
            env = dict(environment)
            env.update(
                AGENT_STATE_DIRECTORY=str(state),
                AGENT_CLI_HOST_NAMES=host_name,
                AGENT_FINISH_REAPER_TIMEOUT_SECONDS="10",
            )
            host = subprocess.Popen([
                str(host_executable), "-c",
                "import os, subprocess, sys, time; "
                "\nwhile not os.path.exists(sys.argv[2]): time.sleep(.01)"
                "\nraise SystemExit(subprocess.call([sys.executable, sys.argv[3], sys.argv[1]]))",
                str(CLOSE), str(root / "start"), str(self.terminal_fixture(root)),
            ], cwd=worktree, env=env)
            try:
                started = self.process_started_at(host.pid)
                sessions = state / "cli-sessions"
                sessions.mkdir(parents=True)
                (sessions / "ours.json").write_text(json.dumps({
                    "session_id": "ours", "cwd": str(worktree), "pid": host.pid,
                    "pid_started_at": started, "started_at": time.time(),
                    "host": host_name,
                }), encoding="utf-8")
                self.record_idle_session(state, host_name, "ours", worktree, host.pid, started)
                (root / "start").touch()
                result_directory = state / "merge-cleanup/results"
                for _ in range(300):
                    results = list(result_directory.glob("*.json")) if result_directory.exists() else []
                    if results:
                        break
                    time.sleep(.05)
                self.assertTrue(results)
                result_path = results[0]
                host.wait(timeout=session_close.DEFAULT_TIMEOUTS["AGENT_FINISH_ACCEPT_TIMEOUT_SECONDS"])
                for _ in range(300):
                    result = session_close.read_json(result_path) or {}
                    if result.get("status") == "session-closed":
                        break
                    time.sleep(.05)
                self.assertEqual("session-closed", result.get("status"), result)
                self.assertTrue(result["session_exited"])
                self.assertEqual("ours", result["session_id"])
                self.assertTrue(worktree.is_dir())
                self.wait_for_observer_exit(result)
            finally:
                if host.poll() is None:
                    host.kill()
                host.wait(timeout=5)

    def wait_for_observer_exit(self, result):
        # A final result precedes observer exit; Windows holds its cwd until that exact process exits.
        for _ in range(200):
            status = session_close.identity_status(result['observer_pid'], result['observer_started_at'])
            if status in ('exited', 'reused'):
                break
            time.sleep(.025)
        self.assertIn(status, ('exited', 'reused'))

    @staticmethod
    def terminal_fixture(root):
        runner = root / "terminal_fixture.py"
        runner.write_text(
            "import os, runpy, signal, sys\n"
            "from pathlib import Path\n"
            "target = sys.argv.pop(1)\n"
            "sys.path.insert(0, str(Path(target).parent))\n"
            "import close_tab\n"
            "close_tab.close_entry = lambda entry: os.kill(entry['pid'], signal.SIGTERM)\n"
            "sys.argv[0] = target\n"
            "runpy.run_path(target, run_name='__main__')\n",
            encoding="utf-8",
        )
        return runner

    @staticmethod
    def fixture_host(root):
        if os.name == "nt":
            executable = Path(sys.executable)
            return executable, executable.stem.casefold()
        executable = root / "fixture-host"
        os.symlink(sys.executable, executable)
        return executable, "fixture-host"

    @staticmethod
    def process_started_at(pid):
        process = session_close.register_session.build_process_lookup()(pid)
        if process is None or not session_close.valid_identity(pid, process.started_at):
            raise AssertionError("disposable host has no queryable process identity")
        return process.started_at

    @staticmethod
    def record_idle_session(state, host, session_id, cwd, pid, pid_started_at):
        store = session_close.register_session._load_session_store()
        if store is None:
            raise AssertionError("session store is unavailable")
        connection = store.connect(state / "sessions.db")
        try:
            handle = store.start_session(
                connection, host, session_id, cwd, pid=pid, pid_started_at=pid_started_at,
            )
            store.record_stop(connection, handle, cwd)
        finally:
            connection.close()


class ProcessIdentityTests(unittest.TestCase):
    def test_absent_zombie_reused_and_unqueryable_are_distinct(self):
        original = session_close.register_session.build_process_lookup
        self.addCleanup(setattr, session_close.register_session, "build_process_lookup", original)

        session_close.register_session.build_process_lookup = lambda: lambda _pid: None
        self.assertEqual(session_close.identity_status(101, 10.0), "exited")
        self.assertTrue(session_close.verified_exited(101, 10.0))

        reused = session_close.register_session.ProcessInfo(101, 1, "codex", 20.0)
        session_close.register_session.build_process_lookup = lambda: lambda _pid: reused
        self.assertEqual(session_close.identity_status(101, 10.0), "reused")
        self.assertFalse(session_close.verified_exited(101, 10.0))

        def unreadable(_pid):
            raise PermissionError("denied")

        session_close.register_session.build_process_lookup = lambda: unreadable
        self.assertEqual(session_close.identity_status(101, 10.0), "unknown")
        self.assertFalse(session_close.verified_exited(101, 10.0))

    def test_invalid_pid_and_timestamp_are_unknown(self):
        for pid, started in ((0, 1.0), (1, 0.0), (1, float("nan")), (True, 1.0)):
            with self.subTest(pid=pid, started=started):
                self.assertEqual(session_close.identity_status(pid, started), "unknown")
                self.assertFalse(session_close.verified_exited(pid, started))


class ClaimAndObligationSafetyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.worktree = Path(self.temp.name) / "worktree"
        self.worktree.mkdir()
        self.worktree = self.worktree.resolve()
        self.original_state = os.environ.get("AGENT_STATE_DIRECTORY")
        os.environ["AGENT_STATE_DIRECTORY"] = self.temp.name
        self.addCleanup(self.restore_state)

    def restore_state(self):
        if self.original_state is None:
            os.environ.pop("AGENT_STATE_DIRECTORY", None)
        else:
            os.environ["AGENT_STATE_DIRECTORY"] = self.original_state

    def write_entry(self, name, entry):
        directory = Path(self.temp.name) / "cli-sessions"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / name).write_text(json.dumps(entry), encoding="utf-8")

    def test_obligation_removal_requires_both_session_and_worktree(self):
        directory = Path(self.temp.name) / "merge-cleanup/obligations"
        directory.mkdir(parents=True)
        worktree = Path(self.temp.name) / "one"
        other = Path(self.temp.name) / "other"
        records = {
            "matching.json": {"session_id": "ours", "worktree": str(worktree)},
            "same-session.json": {"session_id": "ours", "worktree": str(other)},
            "same-worktree.json": {"session_id": "other", "worktree": str(worktree)},
        }
        for name, value in records.items():
            (directory / name).write_text(json.dumps(value), encoding="utf-8")
        session_close.remove_matching_obligations(worktree, "ours")
        self.assertFalse((directory / "matching.json").exists())
        self.assertTrue((directory / "same-session.json").exists())
        self.assertTrue((directory / "same-worktree.json").exists())


class WindowsWrapperSafetyTests(unittest.TestCase):
    def setUp(self):
        platform = mock.patch.object(session_close, "os", SimpleNamespace(name="nt"))
        platform.start()
        self.addCleanup(platform.stop)

    def test_wrapper_requires_a_shell_command_that_launches_the_verified_host(self):
        host = session_close.register_session.ProcessInfo(10, 20, "codex.exe", 100.0)
        wrapper = session_close.register_session.ProcessInfo(20, 1, "pwsh.exe", 95.0)
        original_table = session_close.register_session._windows_process_table
        session_close.register_session._windows_process_table = lambda: {10: host, 20: wrapper}
        self.addCleanup(
            setattr, session_close.register_session, "_windows_process_table", original_table,
        )
        entry = {
            "session_id": "ours", "cwd": "C:/worktree", "pid": 10,
            "pid_started_at": 100.0, "started_at": 101.0, "host": "codex",
        }
        command = mock.Mock(returncode=0, stdout="pwsh -Command codex", stderr="")
        with mock.patch.object(session_close, "process_info", return_value=host), mock.patch(
                "subprocess.run", return_value=command):
            self.assertEqual(
                {"pid": 20, "started_at": 95.0},
                session_close.qualified_windows_wrapper(entry),
            )
        foreign = mock.Mock(returncode=0, stdout="pwsh -NoProfile", stderr="")
        with mock.patch.object(session_close, "process_info", return_value=host), mock.patch(
                "subprocess.run", return_value=foreign):
            self.assertIsNone(session_close.qualified_windows_wrapper(entry))


    def test_reused_host_identity_cannot_qualify_a_wrapper(self):
        entry = {
            "session_id": "ours", "cwd": "C:/worktree", "pid": 10,
            "pid_started_at": 100.0, "started_at": 101.0, "host": "codex",
        }
        reused = session_close.register_session.ProcessInfo(10, 20, "codex.exe", 200.0)
        with mock.patch.object(session_close, "process_info", return_value=reused), \
                mock.patch.object(session_close.register_session, "_windows_process_table") as table:
            self.assertIsNone(session_close.qualified_windows_wrapper(entry))
            table.assert_not_called()


class WindowsObserverTaskTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="peer cli & xml ")
        self.addCleanup(self.temporary.cleanup)
        self.state = Path(self.temporary.name) / "state & xml"
        self.original_state = os.environ.get("AGENT_STATE_DIRECTORY")
        os.environ["AGENT_STATE_DIRECTORY"] = str(self.state)
        self.addCleanup(self.restore_state)
        self.platform = mock.patch.object(
            session_close, "os", SimpleNamespace(
                name="nt", environ=os.environ, fdopen=os.fdopen, close=os.close, replace=os.replace,
            ),
        )
        self.platform.start()
        self.addCleanup(self.platform.stop)

    def restore_state(self):
        if self.original_state is None:
            os.environ.pop("AGENT_STATE_DIRECTORY", None)
        else:
            os.environ["AGENT_STATE_DIRECTORY"] = self.original_state

    @staticmethod
    def completed(returncode=0, stdout="", stderr=""):
        return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)

    @staticmethod
    def windows_arguments(command_line):
        arguments = []
        index = 0
        while index < len(command_line):
            while index < len(command_line) and command_line[index] in " \t":
                index += 1
            if index == len(command_line):
                break
            argument = []
            quoted = False
            while index < len(command_line):
                if command_line[index] in " \t" and not quoted:
                    break
                slashes = 0
                while index < len(command_line) and command_line[index] == "\\":
                    slashes += 1
                    index += 1
                if index < len(command_line) and command_line[index] == '"':
                    argument.append("\\" * (slashes // 2))
                    if slashes % 2:
                        argument.append('"')
                        index += 1
                    elif quoted and index + 1 < len(command_line) and command_line[index + 1] == '"':
                        argument.append('"')
                        index += 2
                    else:
                        quoted = not quoted
                        index += 1
                else:
                    argument.append("\\" * slashes)
                    if index < len(command_line):
                        argument.append(command_line[index])
                        index += 1
            arguments.append("".join(argument))
        return arguments

    def test_long_special_arguments_register_as_a_structured_exec_action(self):
        arguments = [
            "--worktree", "C:\\a space\\<project>&\\quoted\"value",
            "--proof", "x" * 600,
        ]
        calls = []
        task_xml = {}

        def scheduler(arguments, **kwargs):
            calls.append((arguments, kwargs))
            if arguments[0] == "whoami.exe":
                return self.completed(stdout='"EXAMPLE\\A�da","S-1-5-21-123-456-789-1001"\r\n')
            if arguments[1] == "/create":
                task_xml["path"] = Path(arguments[arguments.index("/xml") + 1])
                task_xml["contents"] = task_xml["path"].read_bytes()
            return self.completed()

        with mock.patch.object(session_close.subprocess, "run", side_effect=scheduler):
            self.assertIsNone(session_close.start_observer(arguments))

        namespace = {"task": session_close.TASK_SCHEDULER_NAMESPACE}
        self.assertTrue(task_xml["contents"].startswith(b"\xff\xfe"))
        self.assertIn("encoding='utf-16'", task_xml["contents"].decode("utf-16").splitlines()[0])
        task = session_close.ET.fromstring(task_xml["contents"])
        execute = task.find("task:Actions/task:Exec", namespace)
        self.assertIsNotNone(execute)
        executable = execute.findtext("task:Command", namespaces=namespace)
        encoded_arguments = execute.findtext("task:Arguments", namespaces=namespace)
        working_directory = execute.findtext("task:WorkingDirectory", namespaces=namespace)
        self.assertEqual(sys.executable, executable)
        self.assertGreater(len(encoded_arguments), 262)
        decoded_arguments = self.windows_arguments(encoded_arguments)
        script = decoded_arguments.index("--") + 1
        self.assertTrue(decoded_arguments[script].endswith("finish_reaper.py"))
        self.assertEqual(arguments, decoded_arguments[script + 1:])
        self.assertEqual(str(self.state), working_directory)
        self.assertEqual("S-1-5-21-123-456-789-1001", task.findtext("task:Principals/task:Principal/task:UserId", namespaces=namespace))
        self.assertEqual("PT0S", task.findtext("task:Settings/task:ExecutionTimeLimit", namespaces=namespace))
        self.assertEqual(["whoami.exe", "/user", "/fo", "csv", "/nh"], calls[0][0])
        self.assertEqual("InteractiveToken", task.findtext("task:Principals/task:Principal/task:LogonType", namespaces=namespace))
        self.assertFalse(task_xml["path"].exists())
        create = calls[1][0]
        self.assertEqual(["schtasks.exe", "/create", "/tn"], create[:3])
        self.assertIn("/xml", create)
        self.assertNotIn("/tr", [part.casefold() for part in create])
        self.assertEqual(["schtasks.exe", "/run"], calls[2][0][:2])
        self.assertEqual(["schtasks.exe", "/delete"], calls[3][0][:2])

    def test_registration_refusal_reports_scheduler_output_and_removes_task_and_xml(self):
        calls = []
        task_xml = {}

        def scheduler(arguments, **kwargs):
            calls.append(arguments)
            if arguments[0] == "whoami.exe":
                return self.completed(stdout='"EXAMPLE\\A�da","S-1-5-21-123-456-789-1001"\r\n')
            if arguments[1] == "/create":
                task_xml["path"] = Path(arguments[arguments.index("/xml") + 1])
                return self.completed(returncode=5, stderr="ERROR: Access is denied.")
            return self.completed()

        with mock.patch.object(session_close.subprocess, "run", side_effect=scheduler), \
                self.assertRaisesRegex(session_close.Refusal, "create.*exit 5.*Access is denied"):
            session_close.start_observer(["--result", "C:\\result.json"])

        self.assertFalse(task_xml["path"].exists())
        self.assertEqual(["whoami.exe", "schtasks.exe", "schtasks.exe"], [call[0] for call in calls])
        self.assertEqual("/delete", calls[-1][1])


class ObserverHandshakeTests(unittest.TestCase):
    def test_startup_record_requires_spawned_reaper_and_exact_binding(self):
        worktree = str(Path("/tmp/worktree").resolve())
        record = {
            "host_pid": 10,
            "host_start": 5.0,
            "worktree": worktree,
            "session_id": "session",
            "invocation_id": "invocation",
            "started": time.time(),
            "reaper_pid": 20,
            "reaper_started_at": 30.0,
        }
        with mock.patch.object(session_close, "verified_live", return_value=True):
            self.assertTrue(session_close.valid_observer_start(
                record, 20, 10, 5.0, worktree, "session", "invocation",
            ))
            self.assertFalse(session_close.valid_observer_start(
                record, 21, 10, 5.0, worktree, "session", "invocation",
            ))
            self.assertFalse(session_close.valid_observer_start(
                record, 20, 11, 5.0, worktree, "session", "invocation",
            ))

    def test_startup_rejects_final_missing_and_future_bindings(self):
        baseline = {
            "host_pid": 10, "host_start": 5.0, "worktree": "/tmp/worktree",
            "session_id": "session", "invocation_id": "invocation", "started": 100.0,
            "reaper_pid": 20, "reaper_started_at": 30.0,
        }
        invalid = (
            {"status": "failed"},
            {"invocation_id": None},
            {"started": float("nan")},
            {"started": 200.0},
        )
        original_now = session_close.now
        session_close.now = lambda: 100.0
        self.addCleanup(setattr, session_close, "now", original_now)
        with mock.patch.object(session_close, "verified_live", return_value=True):
            for change in invalid:
                with self.subTest(change=change):
                    record = dict(baseline)
                    record.update(change)
                    self.assertFalse(session_close.valid_observer_start(
                        record, 20, 10, 5.0, "/tmp/worktree", "session", "invocation",
                    ))
            self.assertFalse(session_close.valid_observer_start(
                baseline, 20, 10, 5.0, "/tmp/worktree", "session", "invocation",
                minimum_started=103.0,
            ))

    def test_armed_requires_full_acknowledged_identity(self):
        expected = session_close.binding(10, 5.0, "/tmp/worktree", "session", "invocation")
        observer = {
            **expected, "started": 10.0, "reaper_pid": 20,
            "reaper_started_at": 30.0, "observer_pid": 20, "observer_started_at": 30.0,
        }
        accepted = {
            **expected, "accepted": 40.0, "reaper_pid": 20,
            "reaper_started_at": 30.0, "observer_pid": 20, "observer_started_at": 30.0,
        }
        armed = {**accepted, "armed": 50.0}
        original_now = session_close.now
        session_close.now = lambda: 50.0
        self.addCleanup(setattr, session_close, "now", original_now)
        with mock.patch.object(session_close, "verified_live", return_value=True):
            self.assertTrue(session_close.valid_armed(armed, expected, observer, accepted))
            armed["session_id"] = "different"
            self.assertFalse(session_close.valid_armed(armed, expected, observer, accepted))


class EndReasonRecordingTests(unittest.TestCase):
    def setUp(self):
        self.entry = {"host": "claude", "session_id": "a2bcd5c4-bf6d-4087-95e3-d7ba7f711875", "pid": 1, "pid_started_at": 1.0}

    def test_close_records_closed_reason_through_the_shared_store(self):
        store = mock.Mock()
        store.session_handle.side_effect = lambda host, session_id: f"{host}:{session_id}"
        connection = mock.Mock()
        store.connect.return_value = connection
        with mock.patch.object(own_close.register_session, "_load_session_store", return_value=store):
            own_close._record_own_end(self.entry, "closed")
        store.end_session.assert_called_once_with(connection, "claude:a2bcd5c4-bf6d-4087-95e3-d7ba7f711875", "closed")
        connection.close.assert_called_once()

    def test_reopen_reverses_a_recorded_end_through_the_shared_store(self):
        store = mock.Mock()
        store.session_handle.side_effect = lambda host, session_id: f"{host}:{session_id}"
        connection = mock.Mock()
        store.connect.return_value = connection
        with mock.patch.object(own_close.register_session, "_load_session_store", return_value=store):
            own_close._reopen_own_end(self.entry, "closed")
        store.reopen_session.assert_called_once_with(connection, "claude:a2bcd5c4-bf6d-4087-95e3-d7ba7f711875", "closed")
        connection.close.assert_called_once()

    def test_recording_never_raises_when_the_store_is_unavailable(self):
        with mock.patch.object(own_close.register_session, "_load_session_store", return_value=None):
            own_close._record_own_end(self.entry, "closed")
        with mock.patch.object(own_close.register_session, "_load_session_store", side_effect=RuntimeError("boom")):
            own_close._record_own_end(self.entry, "closed")

    def test_recording_is_skipped_without_a_known_host_or_session(self):
        store = mock.Mock()
        with mock.patch.object(own_close.register_session, "_load_session_store", return_value=store):
            own_close._record_own_end({"pid": 1, "pid_started_at": 1.0}, "closed")
        store.connect.assert_not_called()


class SelfCloseActivityGuardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state = Path(self.temp.name) / "state"
        state_environment = mock.patch.dict(os.environ, {"AGENT_STATE_DIRECTORY": str(self.state)})
        state_environment.start()
        self.addCleanup(state_environment.stop)
        self.cwd = Path(self.temp.name) / "checkout"
        self.cwd.mkdir()
        self.entry = {
            "host": "codex", "session_id": "session", "pid": 10,
            "pid_started_at": 20.0, "started_at": 30.0, "cwd": str(self.cwd),
        }

    def store_row(self, turn_state="awaiting_input", ended_at=None, activity=40.0, **changes):
        return {
            "host": "codex", "session_id": "session", "pid": 10,
            "pid_started_at": 20.0, "cwd": str(self.cwd), "turn_state": turn_state,
            "ended_at": ended_at, "last_activity_at": activity, **changes,
        }

    def test_guard_requires_real_idle_store_evidence_and_refuses_active_missing_or_mismatched_rows(self):
        store = mock.Mock()
        store.session_handle.return_value = "codex:session"
        connection = mock.Mock()
        store.connect.return_value = connection
        cases = (
            (None, "missing"),
            (self.store_row(turn_state="working"), "not awaiting input"),
            (self.store_row(ended_at=41.0), "already ended"),
            (self.store_row(pid_started_at=21.0), "does not match"),
        )
        with mock.patch.object(session_close, "verified_live", return_value=True), \
                mock.patch.object(session_close.register_session, "_load_session_store", return_value=store):
            for row, message in cases:
                with self.subTest(message=message):
                    store.get_session.return_value = row
                    with self.assertRaisesRegex(session_close.Refusal, message):
                        session_close.awaiting_input_activity(self.entry)
            store.get_session.return_value = self.store_row()
            self.assertEqual(40.0, session_close.awaiting_input_activity(self.entry))
            with self.assertRaisesRegex(session_close.Refusal, "activity changed"):
                session_close.awaiting_input_activity(self.entry, 39.0)

    def test_first_guard_refuses_before_cleanup_observer_end_record_or_signal(self):
        for module in (own_close,):
            with self.subTest(module=module.__name__), \
                    mock.patch.object(module.session_close, "own_host_and_entry", return_value=self.entry), \
                    mock.patch.object(module.session_close, "worktree_from_cwd", return_value=self.cwd), \
                    mock.patch.object(module.session_close, "under_or_equal", return_value=True), \
                    mock.patch.object(module.session_close, "awaiting_input_activity", side_effect=session_close.Refusal("session is not awaiting input")), \
                    mock.patch.object(module.session_close, "start_observer") as observer, \
                    mock.patch.object(module, "_record_own_end") as end_record, \
                    mock.patch.object(module, "close_host") as closer:
                with self.assertRaisesRegex(session_close.Refusal, "not awaiting input"):
                    module.main([])
                observer.assert_not_called()
                end_record.assert_not_called()
                closer.assert_not_called()

    def test_prompt_then_stop_with_a_new_idle_token_refuses_before_end_or_signal(self):
        for module in (own_close,):
            with self.subTest(module=module.__name__), ExitStack() as stack:
                stack.enter_context(mock.patch.object(module.session_close, "own_host_and_entry", return_value=self.entry))
                stack.enter_context(mock.patch.object(module.session_close, "worktree_from_cwd", return_value=self.cwd))
                stack.enter_context(mock.patch.object(module.session_close, "under_or_equal", return_value=True))
                stack.enter_context(mock.patch.object(
                    module.session_close, "awaiting_input_activity",
                    side_effect=[40.0, session_close.Refusal("session activity changed before close")],
                ))
                stack.enter_context(mock.patch.object(module.session_close, "git", return_value=(0, "", "")))
                stack.enter_context(mock.patch.object(module.session_close, "results_path", return_value=self.state / "result.json"))
                stack.enter_context(mock.patch.object(module.session_close, "qualified_windows_wrapper", return_value=None))
                stack.enter_context(mock.patch.object(module.session_close, "start_observer", return_value=20))
                stack.enter_context(mock.patch.object(module.session_close, "wait_for_record", side_effect=[{"reaper_pid": 20, "reaper_started_at": 30.0}, {"armed": 50.0}]))
                stack.enter_context(mock.patch.object(module.session_close, "valid_observer_start", return_value=True))
                stack.enter_context(mock.patch.object(module.session_close, "valid_armed", return_value=True))
                stack.enter_context(mock.patch.object(module.session_close, "write_json"))
                cancel = stack.enter_context(mock.patch.object(module.session_close, "cancel_observer"))
                stack.enter_context(mock.patch.object(module, "refresh_attachment", return_value=None, create=True))
                end_record = stack.enter_context(mock.patch.object(module, "_record_own_end"))
                closer = stack.enter_context(mock.patch.object(module, "close_host"))
                with self.assertRaisesRegex(session_close.Refusal, "activity changed"):
                    module.main([])
                end_record.assert_not_called()
                closer.assert_not_called()
                cancel.assert_called_once()

    def test_a_session_that_moved_checkout_still_matches_its_attachment_after_stop(self):
        moved = Path(self.temp.name) / "moved worktree"
        moved.mkdir()
        with mock.patch.dict(os.environ, {"AGENT_STATE_DIRECTORY": str(self.state)}), \
                mock.patch.object(session_close, "verified_live", return_value=True):
            store = session_close.register_session._load_session_store()
            for host in ("codex", "claude"):
                with self.subTest(host=host):
                    entry = {
                        "host": host, "session_id": host + "-moved", "pid": 10,
                        "pid_started_at": 20.0, "started_at": 30.0, "cwd": str(self.cwd),
                    }
                    registry = self.state / "cli-sessions" / f"{entry['session_id']}.json"
                    registry.parent.mkdir(parents=True, exist_ok=True)
                    registry.write_text(json.dumps(entry), encoding="utf-8")
                    connection = store.connect()
                    try:
                        store.start_session(connection, host, entry["session_id"], self.cwd,
                                            pid=entry["pid"], pid_started_at=entry["pid_started_at"])
                    finally:
                        connection.close()
                    payload = json.dumps({"session_id": entry["session_id"], "cwd": str(moved)})
                    with mock.patch.object(sys, "stdin", io.StringIO(payload)):
                        store.main(["hook", "stop", "--host", host])
                    refreshed = json.loads(registry.read_text(encoding="utf-8"))
                    self.assertEqual(str(moved), refreshed["cwd"])
                    with self.assertRaisesRegex(session_close.Refusal, "does not match"):
                        session_close.awaiting_input_activity(entry)
                    self.assertIsInstance(session_close.awaiting_input_activity(refreshed), float)

    def test_real_store_turn_transitions_require_the_current_idle_token_for_both_hosts(self):
        with mock.patch.dict(os.environ, {"AGENT_STATE_DIRECTORY": str(self.state)}), \
                mock.patch.object(session_close, "verified_live", return_value=True):
            store = session_close.register_session._load_session_store()
            self.assertIsNotNone(store)
            for host in ("codex", "claude"):
                with self.subTest(host=host):
                    entry = {
                        "host": host, "session_id": host + "-session", "pid": 10,
                        "pid_started_at": 20.0, "started_at": 30.0, "cwd": str(self.cwd),
                    }
                    connection = store.connect()
                    try:
                        with mock.patch.object(store.time, "time", side_effect=(10.0, 20.0, 30.0, 40.0)):
                            handle = store.start_session(
                                connection, host, entry["session_id"], self.cwd,
                                pid=entry["pid"], pid_started_at=entry["pid_started_at"],
                            )
                            store.record_stop(connection, handle, self.cwd)
                            idle_token = session_close.awaiting_input_activity(entry)
                            store.record_prompt(connection, handle)
                            with self.assertRaisesRegex(session_close.Refusal, "not awaiting input"):
                                session_close.awaiting_input_activity(entry)
                            store.record_stop(connection, handle, self.cwd)
                    finally:
                        connection.close()
                    with self.assertRaisesRegex(session_close.Refusal, "activity changed"):
                        session_close.awaiting_input_activity(entry, idle_token)
                    self.assertEqual(40.0, session_close.awaiting_input_activity(entry))
