import importlib.util
import io
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from git_isolation import without_git_config


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "plugins/agent/.agents/agent/utility/peer/scripts/register_session.py"


def load_module():
    spec = importlib.util.spec_from_file_location("register_session", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


register_session = load_module()
ProcessInfo = register_session.ProcessInfo


def _git_environment():
    return without_git_config(os.environ)


class ResolveRecordedPidTests(unittest.TestCase):
    def test_nearest_host_ancestor_wins(self):
        table = {
            101: ProcessInfo(pid=101, ppid=102, name="bash", started_at=1000.0),
            102: ProcessInfo(pid=102, ppid=103, name="codex", started_at=2000.0),
            103: ProcessInfo(pid=103, ppid=104, name="codex.exe", started_at=3000.0),
            104: ProcessInfo(pid=104, ppid=None, name="explorer", started_at=4000.0),
        }
        pid, host, started = register_session.resolve_recorded_pid(
            101, lookup=table.get, host_names={"codex"},
        )
        self.assertEqual(pid, 102)
        self.assertEqual(host, "codex")
        self.assertEqual(started, 2000.0)

    def test_no_match_falls_back_to_ppid(self):
        table = {
            201: ProcessInfo(pid=201, ppid=202, name="bash", started_at=10.0),
            202: ProcessInfo(pid=202, ppid=203, name="bash", started_at=20.0),
        }
        pid, host, started = register_session.resolve_recorded_pid(
            201, lookup=table.get, host_names={"claude", "codex"},
        )
        self.assertEqual(pid, 201)
        self.assertIsNone(host)
        self.assertEqual(started, 10.0)

    def test_cyclic_table_falls_back(self):
        table = {
            301: ProcessInfo(pid=301, ppid=302, name="bash", started_at=5.0),
            302: ProcessInfo(pid=302, ppid=301, name="bash", started_at=6.0),
        }
        pid, host, started = register_session.resolve_recorded_pid(
            301, lookup=table.get, host_names={"codex"},
        )
        self.assertEqual(pid, 301)
        self.assertIsNone(host)
        self.assertEqual(started, 5.0)

    def test_garbage_table_falls_back(self):
        def garbage_lookup(_pid):
            raise ValueError("garbage table")

        pid, host, started = register_session.resolve_recorded_pid(
            401, lookup=garbage_lookup, host_names={"codex"},
        )
        self.assertEqual(pid, 401)
        self.assertIsNone(host)
        self.assertIsNone(started)

    def test_depth_cap_without_match_falls_back(self):
        table = {}
        for pid in range(501, 516):
            table[pid] = ProcessInfo(pid=pid, ppid=pid + 1, name="bash", started_at=float(pid))
        pid, host, started = register_session.resolve_recorded_pid(
            501, lookup=table.get, host_names={"codex"}, depth_cap=10,
        )
        self.assertEqual(pid, 501)
        self.assertIsNone(host)
        self.assertEqual(started, 501.0)

    def test_host_names_env_override(self):
        os.environ["AGENT_CLI_HOST_NAMES"] = "my-host"
        try:
            self.assertEqual(register_session.read_host_names(), frozenset({"my-host"}))
        finally:
            del os.environ["AGENT_CLI_HOST_NAMES"]


class RecordTests(unittest.TestCase):
    def test_record_writes_resolved_pid_and_host(self):
        table = {
            10: ProcessInfo(pid=10, ppid=11, name="cmd.exe", started_at=42.5),
            11: ProcessInfo(pid=11, ppid=12, name="codex.exe", started_at=99.75),
        }
        original_getppid = os.getppid
        os.getppid = lambda: 10
        with tempfile.TemporaryDirectory() as tmp:
            original_env = os.environ.get(register_session.STATE_DIRECTORY_ENV)
            os.environ[register_session.STATE_DIRECTORY_ENV] = tmp
            try:
                entry = register_session.record(
                    {"session_id": "test-session", "cwd": "C:/somewhere"},
                    lookup=table.get,
                    host_names={"codex"},
                )
            finally:
                os.getppid = original_getppid
                if original_env is None:
                    del os.environ[register_session.STATE_DIRECTORY_ENV]
                else:
                    os.environ[register_session.STATE_DIRECTORY_ENV] = original_env

            self.assertEqual(entry["pid"], 11)
            self.assertEqual(entry["host"], "codex")
            self.assertEqual(entry["pid_started_at"], 99.75)

            written = json.loads(
                (Path(tmp) / "cli-sessions" / "test-session.json").read_text(encoding="utf-8")
            )
            self.assertEqual(written["pid"], 11)
            self.assertEqual(written["host"], "codex")
            self.assertEqual(written["pid_started_at"], 99.75)

    def test_record_without_session_id_is_noop(self):
        self.assertIsNone(register_session.record({}))


class TerminalIdentityTests(unittest.TestCase):
    def test_tmux_wins_over_outer_kitty_and_captures_original_server_socket(self):
        identity = register_session.terminal_identity({
            "TMUX": "/tmp/tmux-100/default,99,0",
            "TMUX_PANE": "%7",
            "KITTY_WINDOW_ID": "4",
            "KITTY_LISTEN_ON": "unix:/kitty",
        })
        self.assertEqual(identity, {
            "kind": "tmux",
            "pane_id": "%7",
            "socket": "/tmp/tmux-100/default",
            "session_index": "0",
        })

    def test_konsole_preserves_service_and_full_session_path(self):
        identity = register_session.terminal_identity({
            "KONSOLE_DBUS_SERVICE": "org.kde.konsole-123",
            "KONSOLE_DBUS_SESSION": "/Sessions/7",
            "KONSOLE_DBUS_WINDOW": "/Windows/1",
        })
        self.assertEqual(identity["service"], "org.kde.konsole-123")
        self.assertEqual(identity["session"], "/Sessions/7")

    def test_wt_session_does_not_select_windows_terminal_off_windows(self):
        original = register_session.os.name
        try:
            register_session.os.name = "posix"
            self.assertIsNone(register_session.terminal_identity({"WT_SESSION": "from-wsl"}))
        finally:
            register_session.os.name = original


class MainTests(unittest.TestCase):
    def _run_main_with_stdin(self, text):
        original_stdin = sys.stdin
        sys.stdin = io.StringIO(text)
        try:
            return register_session.main()
        finally:
            sys.stdin = original_stdin

    def test_main_returns_zero_on_invalid_json(self):
        self.assertEqual(self._run_main_with_stdin("not json {{{"), 0)

    def test_main_returns_zero_on_empty_stdin(self):
        self.assertEqual(self._run_main_with_stdin(""), 0)

    def test_main_returns_zero_on_non_object_json(self):
        self.assertEqual(self._run_main_with_stdin("[1, 2, 3]"), 0)


class LoadSessionStoreTests(unittest.TestCase):

    def _write_store_stub(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("MARKER = 'session-store-stub'\n", encoding="utf-8")

    def _load_from(self, script_path):
        spec = importlib.util.spec_from_file_location("register_session_under_test", script_path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    @staticmethod
    def _staged_receipt(root):
        path = root / ".agents/dependencies/agent/source.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "repository": "tj-agents/agent", "revision": "fixture",
            "sha256": "0" * 64, "exports": [".agents/agent/utility/peer/scripts"],
        }), encoding="utf-8")

    @staticmethod
    def _package_marker(root):
        marker = root / ".codex-plugin/plugin.json"
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("{}\n", encoding="utf-8")

    def test_finds_the_store_in_the_source_tree_layout(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = root / ".agents/dependencies/agent/.agents/agent/utility/peer/scripts/register_session.py"
            script.parent.mkdir(parents=True, exist_ok=True)
            script.write_text(SCRIPT.read_text(encoding="utf-8"), encoding="utf-8")
            self._write_store_stub(root / ".agents/dependencies/agent/.agents/agent-store/runtime/session_store.py")
            self._staged_receipt(root)
            module = self._load_from(script)
            store = module._load_session_store()
            self.assertIsNotNone(store)
            self.assertEqual("session-store-stub", store.MARKER)

    def test_shallow_staging_path_without_a_store_does_not_index_package_ancestors(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(register_session, "HERE", Path(tmp) / "staging"):
            self.assertIsNone(register_session.session_store_path())

    def test_finds_the_store_in_the_agent_utility_packaged_layout(self):
        with tempfile.TemporaryDirectory() as tmp:
            plugin_root = Path(tmp)
            script = plugin_root / ".agents/agent/utility/peer/scripts/register_session.py"
            script.parent.mkdir(parents=True, exist_ok=True)
            script.write_text(SCRIPT.read_text(encoding="utf-8"), encoding="utf-8")
            self._write_store_stub(plugin_root / ".agents/agent-store/runtime/session_store.py")
            self._package_marker(plugin_root)
            module = self._load_from(script)
            store = module._load_session_store()
            self.assertIsNotNone(store)
            self.assertEqual("session-store-stub", store.MARKER)

    def test_finds_the_store_in_the_skills_packaged_layout(self):
        for layout in ("skills", "codex-skills"):
            with self.subTest(layout=layout):
                with tempfile.TemporaryDirectory() as tmp:
                    plugin_root = Path(tmp)
                    script = plugin_root / layout / "peer/scripts/register_session.py"
                    script.parent.mkdir(parents=True, exist_ok=True)
                    script.write_text(SCRIPT.read_text(encoding="utf-8"), encoding="utf-8")
                    self._write_store_stub(plugin_root / ".agents/agent-store/runtime/session_store.py")
                    self._package_marker(plugin_root)
                    module = self._load_from(script)
                    store = module._load_session_store()
                    self.assertIsNotNone(store)
                    self.assertEqual("session-store-stub", store.MARKER)


@unittest.skipUnless(shutil.which("git"), "git is required for SessionStart persistence regression")
class SessionStartStoreIntegrationTests(unittest.TestCase):
    def _load_from(self, script_path):
        spec = importlib.util.spec_from_file_location("register_session_under_test", script_path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def _run_git(self, cwd, *args):
        result = subprocess.run(
            ["git", "-C", str(cwd), *args], capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=15, check=True, env=_git_environment(),
        )
        return result.stdout.strip()

    def _create_repo(self, root):
        repo = root / "session repo é"
        repo.mkdir()
        self._run_git(repo, "init")
        self._run_git(repo, "config", "user.email", "test@example.com")
        self._run_git(repo, "config", "user.name", "Session Store Test")
        (repo / "README.md").write_text("session store\n", encoding="utf-8")
        self._run_git(repo, "add", "README.md")
        self._run_git(repo, "commit", "-m", "initial session")
        self._run_git(repo, "branch", "-M", "session-registration")
        self._run_git(repo, "remote", "add", "origin", "https://GitHub.com/Tj-Agents/Session-Store-Regression.git")
        return repo, self._run_git(repo, "rev-parse", "HEAD")

    def _copy_layout(self, root, layout):
        if layout == "source":
            script = root / ".agents/dependencies/agent/.agents/agent/utility/peer/scripts/register_session.py"
            store = root / ".agents/dependencies/agent/.agents/agent-store/runtime/session_store.py"
        elif layout == "agents-utility":
            script = root / ".agents/agent/utility/peer/scripts/register_session.py"
            store = root / ".agents/agent-store/runtime/session_store.py"
        else:
            script = root / layout / "peer/scripts/register_session.py"
            store = root / ".agents/agent-store/runtime/session_store.py"
        script.parent.mkdir(parents=True)
        store.parent.mkdir(parents=True)
        shutil.copy2(SCRIPT, script)
        runtime = store.parents[2] / "agent" / "runtime"
        runtime.mkdir(parents=True)
        shutil.copy2(ROOT / "plugins/agent/.agents/agent/runtime/host.py", runtime / "host.py")
        for name in register_session.OBSERVER_STORE_FILES[1:]:
            shutil.copy2(ROOT / "plugins/agent/.agents/agent-store/runtime" / name, store.with_name(name))
        if layout == "source":
            LoadSessionStoreTests._staged_receipt(root)
        else:
            LoadSessionStoreTests._package_marker(root)
        return script

    def test_session_start_records_real_store_from_source_and_packaged_layouts(self):
        cases = (
            ("source", "codex"),
            ("source", "claude"),
            ("agents-utility", "codex"),
            ("skills", "codex"),
            ("codex-skills", "codex"),
        )
        with tempfile.TemporaryDirectory(prefix="session start é ") as tmp:
            root = Path(tmp)
            repo, head = self._create_repo(root)
            for layout, host in cases:
                with self.subTest(layout=layout, host=host):
                    module = self._load_from(self._copy_layout(root / f"{layout} {host}", layout))
                    state = root / f"state {layout} {host} é"
                    profile = root / f"profile {layout} {host} é"
                    session_id = f"{host}-{layout}"
                    hook_pid = os.getppid()
                    pid = hook_pid + 10_000
                    host_pid = pid + 1
                    pid_started_at = 1700000000.25
                    process_table = {
                        hook_pid: module.ProcessInfo(pid=hook_pid, ppid=pid, name="hook-shell", started_at=1.0),
                        pid: module.ProcessInfo(pid=pid, ppid=host_pid, name="python", started_at=2.0),
                        host_pid: module.ProcessInfo(pid=host_pid, ppid=None, name=f"{host}.exe", started_at=pid_started_at),
                    }
                    payload = json.dumps({"session_id": session_id, "cwd": str(repo)})
                    environment = _git_environment()
                    environment.update({
                        module.STATE_DIRECTORY_ENV: str(state),
                        module.TITLE_ENV: f"{host} session é",
                        module.HOST_NAMES_ENV: host,
                        "CODEX_HOME": str(profile / "codex"),
                        "CLAUDE_CONFIG_DIR": str(profile / "claude"),
                    })
                    with (
                        mock.patch.dict(os.environ, environment, clear=True),
                        mock.patch.object(module, "build_process_lookup", return_value=process_table.get),
                        mock.patch.object(sys, "stdin", io.StringIO(payload)),
                    ):
                        self.assertEqual(0, module.main())

                    native = json.loads(
                        (state / "cli-sessions" / f"{session_id}.json").read_text(encoding="utf-8")
                    )
                    self.assertEqual(session_id, native["session_id"])
                    self.assertEqual(host, native["host"])
                    self.assertEqual(f"{host} session é", native["title"])
                    self.assertEqual(str(repo), native["cwd"])
                    self.assertEqual(host_pid, native["pid"])
                    self.assertEqual(pid_started_at, native["pid_started_at"])

                    database = state / "sessions.db"
                    self.assertTrue(database.is_file())
                    connection = sqlite3.connect(database)
                    try:
                        row = connection.execute(
                            "SELECT handle, host, session_id, title, cwd, repo, branch, head, pid, pid_started_at "
                            "FROM sessions WHERE handle = ?",
                            (f"{host}:{session_id}",),
                        ).fetchone()
                    finally:
                        connection.close()
                    self.assertEqual(
                        (
                            f"{host}:{session_id}", host, session_id, f"{host} session é", str(repo),
                            "github.com/tj-agents/session-store-regression", "session-registration", head,
                            host_pid, pid_started_at,
                        ),
                        row,
                    )


class ProcessLookupSafetyTests(unittest.TestCase):
    def test_ps_distinguishes_missing_process_from_inspection_failures(self):
        missing = mock.Mock(returncode=1, stdout="", stderr="")
        denied = mock.Mock(returncode=1, stdout="", stderr="permission denied")
        with mock.patch("subprocess.run", return_value=missing):
            self.assertIsNone(register_session._ps_lookup(101))
        with mock.patch("subprocess.run", return_value=denied):
            with self.assertRaises(OSError):
                register_session._ps_lookup(101)

    def test_ps_rejects_malformed_identity_instead_of_reporting_absence(self):
        malformed = mock.Mock(returncode=0, stdout="not-a-pid codex now", stderr="")
        with mock.patch("subprocess.run", return_value=malformed):
            with self.assertRaises(ValueError):
                register_session._ps_lookup(101)


class LauncherTitleTests(unittest.TestCase):
    def test_a_cleared_session_does_not_inherit_the_tab_title(self):
        module = load_module()
        with mock.patch.dict(os.environ, {module.TITLE_ENV: "Launched work"}, clear=False):
            for source, expected in ((None, "Launched work"), ("startup", "Launched work"),
                                     ("resume", "Launched work"), ("compact", "Launched work"), ("clear", None)):
                with self.subTest(source=source):
                    payload = {"session_id": "id"} if source is None else {"session_id": "id", "source": source}
                    self.assertEqual(expected, module.launcher_title(payload))


if __name__ == "__main__":
    unittest.main()
