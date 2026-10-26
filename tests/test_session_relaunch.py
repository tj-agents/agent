"""Relaunch fixtures: dummy host processes and a fake recovery launcher; no model session is started."""

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / ".agents/agent/runtime"
PEER = ROOT / ".agents/agent/utility/peer/scripts"
for path in (SCRIPTS, PEER):
    sys.path.insert(0, str(path))


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


RELAUNCH = load("session_relaunch", SCRIPTS / "session_relaunch.py")
WATCHER = load("relaunch_watcher", SCRIPTS / "relaunch_watcher.py")
REFRESH = load("reload_plugins_for_relaunch", SCRIPTS / "reload_plugins.py")
SESSION = "5ea1d656-3e27-42e1-afb3-0af687449f91"


class State(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="session relaunch ")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        environment = {key: value for key, value in os.environ.items()
                       if key not in {"CLAUDE_CODE_SESSION_ID", "CLAUDE_PID", "CLAUDE_CODE_CHILD_SESSION"}}
        environment.update(AGENT_STATE_DIRECTORY=str(self.root / "state"), CLAUDE_CONFIG_DIR=str(self.root / "claude"),
                           CODEX_HOME=str(self.root / "codex"))
        patcher = mock.patch.dict(os.environ, environment, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)


class ArgumentTests(unittest.TestCase):
    def test_only_the_flags_resume_does_not_restore_are_kept(self):
        argv = ["--debug-file", "log", "--add-dir", "C:/a", "C:/b", "--settings", "s.json", "--model", "opus",
                "--mcp-config=m.json", "--strict-mcp-config", "--plugin-dir", "C:/p", "--permission-mode", "plan",
                "--dangerously-skip-permissions", "--resume", "old", "--mcp-config", "x.json", "y.json", "-p"]
        self.assertEqual(["--add-dir", "C:/a", "C:/b", "--settings", "s.json", "--model", "opus", "--mcp-config=m.json",
                          "--strict-mcp-config", "--plugin-dir", "C:/p", "--permission-mode", "plan",
                          "--dangerously-skip-permissions", "--mcp-config", "x.json", "y.json"],
                         RELAUNCH.preserved_arguments(argv))

    def test_a_dummy_process_command_line_is_read_back(self):
        process = subprocess.Popen([sys.executable, "-c", "import time; print('ready', flush=True); time.sleep(30)",
                                    "--add-dir", "C:/extra dir", "--settings", "s.json"], stdout=subprocess.PIPE,
                                   text=True)
        self.addCleanup(process.stdout.close)
        self.addCleanup(process.wait)
        self.addCleanup(process.kill)
        self.assertEqual("ready\n", process.stdout.readline())
        self.assertEqual(["--add-dir", "C:/extra dir", "--settings", "s.json"],
                         RELAUNCH.preserved_arguments(RELAUNCH.host_arguments(process.pid)))


class StalenessTests(State):
    def install(self, plugin, *versions, loaded=None):
        for version in versions:
            (self.root / "cache" / plugin / version).mkdir(parents=True, exist_ok=True)
        if loaded:
            marker = self.root / "cache" / plugin / loaded / ".in_use"
            marker.mkdir(parents=True, exist_ok=True)
            (marker / "4242").write_text('{"pid":4242}', encoding="utf-8")
        return {"scope": "user", "installPath": str(self.root / "cache" / plugin / versions[-1])}

    def test_a_session_holding_an_older_copy_is_stale_and_a_current_one_is_not(self):
        installed = {"machine@tj-agents": [self.install("machine", "v1", "v2", loaded="v1")],
                     "base@tj-agents": [self.install("base", "v1", "v2", loaded="v2")],
                     "work@tj-agents": [self.install("work", "v2")],
                     "react@tj-agents": [self.install("react", "v2")]}
        identities = sorted(installed)
        self.assertEqual(["machine@tj-agents", "work@tj-agents"],
                         RELAUNCH.stale_plugins(4242, self.root, identities, installed, {"work@tj-agents"}))
        self.assertEqual(["work@tj-agents"], RELAUNCH.stale_plugins(0, self.root, identities, installed, {"work@tj-agents"}))

    def test_the_effective_installation_follows_claude_scope_precedence(self):
        project = self.root / "project"
        entries = [{"scope": "user", "installPath": "user"},
                   {"scope": "project", "projectPath": str(project), "installPath": "project"},
                   {"scope": "local", "projectPath": str(self.root / "other"), "installPath": "other"}]
        self.assertEqual("project", RELAUNCH.effective_installation(entries, project / "src")["installPath"])
        self.assertEqual("user", RELAUNCH.effective_installation(entries, self.root / "elsewhere")["installPath"])


class Shared:
    def __init__(self, events):
        self.events = events

    def own_host_and_entry(self):
        return {"host": "claude", "session_id": SESSION, "pid": 4242, "pid_started_at": 1000.5, "title": "Work"}

    def start_detached(self, script, arguments):
        self.events.append(("watcher", Path(script).name, arguments))


class ArmTests(State):
    def arm(self, arm_close):
        for patcher in (mock.patch.object(RELAUNCH.agent_cli, "resolve_claude_executable", return_value="claude"),
                        mock.patch.object(RELAUNCH, "cli_close_capability", return_value=self.root / "close.py"),
                        mock.patch.object(RELAUNCH, "arm_client_close", side_effect=arm_close)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_the_watcher_starts_before_close_is_armed_and_carries_the_native_arguments(self):
        events = []
        self.arm(lambda *_: events.append(("close",)) or {"status": "armed"})
        result = RELAUNCH.relaunch(self.root, {}, shared=Shared(events),
                                   arguments=lambda pid: ["--add-dir", "C:/extra", "--debug-file", "x"])
        self.assertEqual(["watcher", "close"], [event[0] for event in events])
        self.assertEqual("relaunch_watcher.py", events[0][1])
        options = events[0][2]
        self.assertIn("--native-argument=--add-dir", options)
        self.assertIn("--native-argument=C:/extra", options)
        self.assertNotIn("--native-argument=--debug-file", options)
        self.assertEqual(SESSION, options[options.index("--session-id") + 1])
        self.assertEqual(str(self.root.resolve()), options[options.index("--expected-cwd") + 1])
        self.assertEqual({"relaunch": "armed", "session_id": SESSION, "native_arguments": ["--add-dir", "C:/extra"],
                          "close": "armed"}, result)
        record = json.loads(RELAUNCH.record_path(SESSION).read_text(encoding="utf-8"))
        self.assertEqual("waiting", record["state"])
        self.assertEqual(record["token"], options[options.index("--token") + 1])

    def test_a_different_session_or_pid_never_closes_the_registered_client(self):
        for session, pid in (("different-session", "4242"), (SESSION, "9999")):
            events = []
            with self.subTest(session=session, pid=pid), mock.patch.dict(os.environ, {
                    "CLAUDE_CODE_SESSION_ID": session, "CLAUDE_PID": pid}):
                with self.assertRaisesRegex(RuntimeError, "does not match"):
                    RELAUNCH.relaunch(self.root, {}, shared=Shared(events), arguments=lambda _: [])
            self.assertEqual([], events)
            self.assertFalse(RELAUNCH.record_path(SESSION).exists())

    def test_a_subagent_transcript_cannot_close_the_parent_client(self):
        events = []
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_LOG": str(self.root / "subagents/agent-child.jsonl")}):
            with self.assertRaisesRegex(RuntimeError, "subagent cannot"):
                RELAUNCH.relaunch(self.root, {}, shared=Shared(events), arguments=lambda _: [])
        self.assertEqual([], events)
        self.assertFalse(RELAUNCH.record_path(SESSION).exists())

    def test_a_refused_close_cancels_the_watcher(self):
        self.arm(RuntimeError("tab ownership could not be verified"))
        with self.assertRaisesRegex(RuntimeError, "tab ownership"):
            RELAUNCH.relaunch(self.root, {}, shared=Shared([]), arguments=lambda pid: [])
        self.assertEqual("cancelled", json.loads(RELAUNCH.record_path(SESSION).read_text(encoding="utf-8"))["state"])


class WatcherTests(State):
    def options(self, pid, started_at, recovery, record, token="t"):
        return ["--session-id", SESSION, "--pid", str(pid), "--pid-started-at", repr(started_at), "--record", str(record),
                "--token", token, "--recovery", str(recovery), "--title", "Work", "--prompt", "Continue.",
                "--expected-cwd", str(self.root.resolve()), "--timeout", "60",
                "--native-argument=--add-dir", "--native-argument=C:/extra"]

    def fake_recovery(self):
        recovery = self.root / "agent_recovery.py"
        recovery.write_text(textwrap.dedent("""
            import json, os, sys
            from pathlib import Path
            Path(__file__).with_name("launched.json").write_text(json.dumps(
                {"argv": sys.argv[1:], "session": os.environ.get("CLAUDE_CODE_SESSION_ID")}), encoding="utf-8")
        """), encoding="utf-8")
        return recovery

    def test_the_session_reopens_only_after_the_old_host_process_exits(self):
        host = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(1.5)"])
        self.addCleanup(host.wait)
        started = WATCHER.session_close.process_info(host.pid).started_at
        recovery = self.fake_recovery()
        record = self.root / "record.json"
        request, result = self.root / "close-request.json", self.root / "close-result.json"
        request.write_text("{}", encoding="utf-8")
        result.write_text(json.dumps({"status": "closed", "session_id": SESSION}), encoding="utf-8")
        close = {"request": str(request), "request_sha256": hashlib.sha256(b"{}").hexdigest(), "result": str(result)}
        record.write_text(json.dumps({"state": "waiting", "token": "t", "cwd": str(self.root.resolve()),
                                      "close": close}), encoding="utf-8")
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": SESSION}):
            self.assertEqual(0, WATCHER.main(self.options(host.pid, started, recovery, record)))
        self.assertIsNotNone(host.poll())
        launched = json.loads(recovery.with_name("launched.json").read_text(encoding="utf-8"))
        self.assertEqual(["open", "--host", "claude", "--id", SESSION, "--title", "Work", "--prompt", "Continue.",
                          "--expected-cwd", str(self.root.resolve()),
                          "--native-argument=--add-dir", "--native-argument=C:/extra"], launched["argv"])
        self.assertIsNone(launched["session"])
        self.assertEqual("launched", json.loads(record.read_text(encoding="utf-8"))["state"])

    def test_a_cancelled_record_never_launches(self):
        recovery = self.fake_recovery()
        record = self.root / "record.json"
        record.write_text(json.dumps({"state": "cancelled", "token": "t"}), encoding="utf-8")
        self.assertEqual(1, WATCHER.main(self.options(os.getpid(), 1.0, recovery, record)))
        self.assertFalse(recovery.with_name("launched.json").exists())

    def test_a_host_that_never_exits_expires_without_launching(self):
        record = self.root / "record.json"
        record.write_text(json.dumps({"state": "waiting", "token": "t"}), encoding="utf-8")
        args = WATCHER.arguments(self.options(4242, 1.0, self.root / "missing.py", record))
        clock = iter(range(0, 1000, 30))
        self.assertFalse(WATCHER.wait_for_exit(args, exited=lambda *_: False, clock=lambda: next(clock),
                                               sleep=lambda _: None))
        self.assertEqual("expired", json.loads(record.read_text(encoding="utf-8"))["state"])


class SyncTests(State):
    def run_sync(self, result="updated", stale=("machine@tj-agents",), child=False, relaunch=None, no_relaunch=False):
        environment = {"CLAUDE_CODE_SESSION_ID": SESSION, "CLAUDE_PID": "4242"}
        if child:
            environment["CLAUDE_CODE_CHILD_SESSION"] = "1"
        relaunched = mock.Mock(return_value=relaunch or {"relaunch": "armed", "session_id": SESSION})
        with mock.patch.dict(os.environ, environment), \
                mock.patch.object(REFRESH.launch_check, "check", return_value=result), \
                mock.patch.object(REFRESH, "selection", return_value=({}, ["machine@tj-agents"])), \
                mock.patch.object(RELAUNCH, "stale_plugins", return_value=list(stale)), \
                mock.patch.object(RELAUNCH, "relaunch", relaunched), \
                mock.patch.dict(sys.modules, {"session_relaunch": RELAUNCH}), \
                mock.patch("sys.stdout") as stdout, mock.patch("sys.stderr"):
            code = REFRESH.main(["--project", str(self.root)] + (["--no-relaunch"] if no_relaunch else []))
        output = "".join(call.args[0] for call in stdout.write.call_args_list)
        return code, json.loads(output), relaunched

    def test_a_stale_claude_session_is_relaunched_without_a_user_step(self):
        code, summary, relaunched = self.run_sync()
        self.assertEqual(0, code)
        self.assertEqual("armed", summary["claude"]["relaunch"])
        self.assertEqual(["machine@tj-agents"], summary["claude"]["stale"])
        relaunched.assert_called_once()

    def test_a_current_session_is_left_alone(self):
        code, summary, relaunched = self.run_sync(result="current", stale=())
        self.assertEqual((0, {"check": "current", "plugin setup": []}), (code, summary))
        relaunched.assert_not_called()

    def test_a_tool_subprocess_marker_does_not_reject_the_owning_session(self):
        code, summary, relaunched = self.run_sync(child=True)
        self.assertEqual(0, code)
        self.assertEqual("armed", summary["claude"]["relaunch"])
        relaunched.assert_called_once()

    def test_disabling_relaunch_reports_incomplete_adoption(self):
        code, summary, relaunched = self.run_sync(result="current", no_relaunch=True)
        self.assertEqual(1, code)
        self.assertEqual("failed", summary["claude"]["relaunch"])
        self.assertIn("--no-relaunch", summary["claude"]["error"])
        relaunched.assert_not_called()

    def test_a_failed_relaunch_is_reported_as_a_failure(self):
        relaunched = mock.Mock(side_effect=RuntimeError("relaunch was not armed: refused"))
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": SESSION, "CLAUDE_PID": "4242"}), \
                mock.patch.object(REFRESH.launch_check, "check", return_value="updated"), \
                mock.patch.object(REFRESH, "selection", return_value=({}, ["machine@tj-agents"])), \
                mock.patch.object(RELAUNCH, "stale_plugins", return_value=["machine@tj-agents"]), \
                mock.patch.object(RELAUNCH, "relaunch", relaunched), \
                mock.patch.dict(sys.modules, {"session_relaunch": RELAUNCH}), \
                mock.patch("sys.stdout"), mock.patch("sys.stderr"):
            self.assertEqual(1, REFRESH.main(["--project", str(self.root)]))


if __name__ == "__main__":
    unittest.main()
