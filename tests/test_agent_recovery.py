import importlib.util
import contextlib
import io
import json
import os
import subprocess
import sys
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("agent_recovery", ROOT / "plugins/agent/.agents/agent/runtime/agent_recovery.py")
RECOVERY = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(RECOVERY)
from codex import sessions as CODEX_SESSIONS  # noqa: E402
from host import Host  # noqa: E402
AWAIT_PICKUP = RECOVERY.await_pickup


class RecoveryTests(unittest.TestCase):
    def test_store_loader_requires_the_canonical_runtime(self):
        with mock.patch.object(RECOVERY, "_package_root", return_value=Path("staging")):
            with self.assertRaisesRegex(RuntimeError, "session_store.py"):
                RECOVERY._script_path("session_store")

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="synthetic recovery ")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.cwd = self.root / "checkout with spaces"
        self.cwd.mkdir()
        self.state = self.root / "state"
        self.env = mock.patch.dict("os.environ", {"AGENT_STATE_DIRECTORY": str(self.state)}, clear=False)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.pickup = mock.patch.object(RECOVERY, "await_pickup", return_value="registered in sessions.db")
        self.pickup.start()
        self.addCleanup(self.pickup.stop)

    def write_codex(self, session="exact_id", source="cli"):
        path = self.root / "sessions/archive" / f"{session}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        events = [
            {"type": "session_meta", "payload": {"id": session, "cwd": str(self.cwd), "source": source}},
            {"type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "recover launch topic"}]}},
        ]
        path.write_text("\n".join(json.dumps(item) for item in events), encoding="utf-8")

    def write_claude(self, root, session="claude_id"):
        path = root / "project" / f"{session}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"type": "user", "sessionId": session, "cwd": str(self.cwd),
                                    "message": {"content": [{"type": "text", "text": "Claude recovery title"}]}}), encoding="utf-8")

    def test_search_reads_archive_and_filters_non_cli_helper(self):
        self.write_codex()
        results = RECOVERY.search((Host.CODEX,), topic="topic", roots={Host.CODEX: self.root / "sessions"})
        self.assertEqual(results[0]["id"], "exact_id")
        self.assertEqual(results[0]["status"], "unknown")
        self.write_codex("helper", "vscode")
        self.assertEqual([item["id"] for item in RECOVERY.search((Host.CODEX,), roots={Host.CODEX: self.root / "sessions"})], ["exact_id"])

    def test_search_uses_native_archives_titles_and_ids_before_counting(self):
        sessions = self.root / "sessions"
        sessions.mkdir()
        archive = self.root / "archived_sessions"
        original_root = self.root
        self.root = archive.parent
        self.write_codex("archived_id")
        source = archive / "archive" / "archived_id.jsonl"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text((original_root / "sessions/archive/archived_id.jsonl").read_text(encoding="utf-8"), encoding="utf-8")
        (original_root / "sessions/archive/archived_id.jsonl").unlink()
        (self.root / "session_index.jsonl").write_text(json.dumps({"id": "archived_id", "thread_name": "Lost title"}), encoding="utf-8")
        self.assertEqual(RECOVERY.search((Host.CODEX,), topic="title", roots={Host.CODEX: sessions})[0]["id"], "archived_id")
        self.assertEqual(RECOVERY.search((Host.CODEX,), topic="archived_id", roots={Host.CODEX: sessions})[0]["title"], "Lost title")
        registry = RECOVERY._registry_root()
        registry.mkdir(parents=True)
        (registry / "archived_id.json").write_text(json.dumps({"session_id": "archived_id", "title": "Saved tab title"}), encoding="utf-8")
        self.assertEqual(RECOVERY.search((Host.CODEX,), topic="Lost title", roots={Host.CODEX: sessions})[0]["id"], "archived_id")
        self.assertEqual(RECOVERY.search((Host.CODEX,), topic="Saved tab title", roots={Host.CODEX: sessions})[0]["id"], "archived_id")
        with self.assertRaises(ValueError):
            RECOVERY.search((Host.CODEX,), roots={Host.CODEX: self.root / "missing"})
        with self.assertRaises(ValueError):
            RECOVERY.search((Host.CODEX,), count=0, roots={Host.CODEX: sessions})

    def test_live_and_pid_reuse_are_distinct(self):
        self.write_codex()
        item = RECOVERY.find(Host.CODEX, "exact_id", {Host.CODEX: self.root / "sessions"})
        record = RECOVERY._registry_root() / "exact_id.json"
        record.parent.mkdir(parents=True)
        record.write_text(json.dumps({"pid": 7, "pid_started_at": 10, "host": "codex"}), encoding="utf-8")
        process = type("P", (), {"name": "codex", "started_at": 10})()
        self.assertEqual(RECOVERY.liveness(item, lambda _: process), "live")
        process.started_at = 20
        self.assertEqual(RECOVERY.liveness(item, lambda _: process), "closed")
        record.write_text(json.dumps({"pid": 7, "pid_started_at": "bad", "host": "codex"}), encoding="utf-8")
        self.assertEqual(RECOVERY.liveness(item, lambda _: process), "unknown")

    def test_rejects_flag_like_native_id_and_keeps_ambiguous_receipt(self):
        self.assertFalse(RECOVERY.SESSION_ID.fullmatch("-resume"))
        path = RECOVERY._claim(Host.CODEX, "exact_id", "profile")
        RECOVERY._receipt_state(path, Host.CODEX, "exact_id", "profile", "ambiguous")
        with self.assertRaises(ValueError):
            RECOVERY._claim(Host.CODEX, "exact_id", "profile")

    def test_receipt_waits_for_newer_closed_registration_before_reuse(self):
        registry = RECOVERY._registry_root() / "exact_id.json"
        registry.parent.mkdir(parents=True)
        registry.write_text(json.dumps({"pid": 7, "pid_started_at": 1, "host": "codex", "started_at": 1}), encoding="utf-8")
        receipt = RECOVERY._claim(Host.CODEX, "exact_id", "profile")
        registry.write_text(json.dumps({"pid": 7, "pid_started_at": 1, "host": "codex", "started_at": 1}), encoding="utf-8")
        with self.assertRaises(ValueError):
            RECOVERY._claim(Host.CODEX, "exact_id", "profile")
        created = json.loads(receipt.read_text(encoding="utf-8"))["created_at"]
        registry.write_text(json.dumps({"pid": 8, "pid_started_at": 2, "host": "codex", "started_at": created + 1}), encoding="utf-8")
        with mock.patch.object(RECOVERY, "liveness", return_value="closed"):
            self.assertTrue(RECOVERY._claim(Host.CODEX, "exact_id", "profile").is_file())

    def test_search_skips_a_session_file_removed_during_the_scan(self):
        self.write_codex()
        self.write_codex("vanished_id")
        history = RECOVERY._load("history")
        real_session = history.session

        def session(path, host):
            if "vanished_id" in path.name:
                raise FileNotFoundError(path)
            return real_session(path, host)

        with mock.patch.object(history, "session", side_effect=session):
            results = RECOVERY.search((Host.CODEX,), topic="topic", roots={Host.CODEX: self.root / "sessions"})
        self.assertEqual(["exact_id"], [item["id"] for item in results])

    def test_launched_receipt_is_released_once_its_wrapper_process_ends(self):
        receipt = RECOVERY._claim(Host.CODEX, "exact_id", "profile")
        RECOVERY._receipt_state(receipt, Host.CODEX, "exact_id", "profile", "launched", {"pid": 41, "started_at": 5.0})
        wrapper = SimpleNamespace(name="python", started_at=5.0)
        lookup = mock.Mock(return_value=mock.Mock(side_effect=lambda pid: wrapper if pid == 41 else None))
        register = SimpleNamespace(build_process_lookup=lookup)
        real_load = RECOVERY._load
        with mock.patch.object(RECOVERY, "_load", side_effect=lambda name: register if name == "register_session" else real_load(name)):
            with self.assertRaisesRegex(ValueError, "confirm-closed"):
                RECOVERY._claim(Host.CODEX, "exact_id", "profile", confirm_closed=True)
            wrapper.started_at = 9.0
            self.assertEqual("pending", json.loads(RECOVERY._claim(Host.CODEX, "exact_id", "profile").read_text(encoding="utf-8"))["state"])

    def test_launch_ends_only_when_wrapper_and_native_child_are_both_gone(self):
        processes = {41: SimpleNamespace(started_at=5.0), 42: SimpleNamespace(started_at=6.0)}
        receipt = {"wrapper": {"pid": 41, "started_at": 5.0, "child": {"pid": 42, "started_at": 6.0}}}
        lookup = processes.get
        self.assertFalse(RECOVERY._launch_ended(receipt, lookup))
        del processes[41]
        self.assertFalse(RECOVERY._launch_ended(receipt, lookup))
        processes[42] = SimpleNamespace(started_at=None)
        self.assertIsNone(RECOVERY._launch_ended(receipt, lookup))
        processes[42] = SimpleNamespace(started_at=99.0)
        self.assertTrue(RECOVERY._launch_ended(receipt, lookup))
        del processes[42]
        self.assertTrue(RECOVERY._launch_ended(receipt, lookup))

    def test_ambiguous_receipt_rereads_its_status_for_a_child_started_later(self):
        status = self.root / "attempt.status.json"
        status.write_text(json.dumps({"pid": 41, "state": "preparing", "at": 5.5}), encoding="utf-8")
        processes = {41: SimpleNamespace(started_at=5.0)}
        receipt = {"wrapper": None, "status_file": str(status)}
        self.assertFalse(RECOVERY._launch_ended(receipt, processes.get))
        status.write_text(json.dumps({"pid": 41, "child_pid": 42, "state": "running", "at": 7.0}), encoding="utf-8")
        processes[42] = SimpleNamespace(started_at=6.0)
        receipt["wrapper"] = {"pid": 41, "started_at": 5.0}
        del processes[41]
        self.assertFalse(RECOVERY._launch_ended(receipt, processes.get))
        processes[42] = SimpleNamespace(started_at=500.0)
        processes[41] = SimpleNamespace(started_at=600.0)
        self.assertTrue(RECOVERY._launch_ended(receipt, processes.get))
        processes[42] = SimpleNamespace(started_at=None)
        self.assertIsNone(RECOVERY._launch_ended(receipt, processes.get))
        status.write_text(json.dumps({"pid": 41, "child_pid": 42, "state": "exited", "at": 8.0}), encoding="utf-8")
        processes[42] = SimpleNamespace(started_at=6.0)
        self.assertTrue(RECOVERY._launch_ended(receipt, processes.get))
        status.unlink()
        self.assertTrue(RECOVERY._launch_ended(receipt, processes.get))
        self.assertIsNone(RECOVERY._launch_ended({"wrapper": None, "status_file": str(status)}, processes.get))

    def test_wrapper_identity_records_the_native_child_and_refuses_unreadable_starts(self):
        status = self.root / "attempt.status.json"
        status.write_text(json.dumps({"pid": 41, "child_pid": 42, "state": "running"}), encoding="utf-8")
        processes = {41: SimpleNamespace(started_at=5.0), 42: SimpleNamespace(started_at=6.0)}
        self.assertEqual({"pid": 41, "started_at": 5.0, "child": {"pid": 42, "started_at": 6.0}},
                         RECOVERY._wrapper_identity(status, processes.get))
        del processes[41]
        self.assertEqual({"pid": 41, "started_at": None, "child": {"pid": 42, "started_at": 6.0}},
                         RECOVERY._wrapper_identity(status, processes.get))
        processes[42] = SimpleNamespace(started_at=None)
        self.assertIsNone(RECOVERY._wrapper_identity(status, processes.get))

    def test_wrapper_identity_preserves_the_frontends_terminal_observation(self):
        status = self.root / "frontend.status.json"
        terminal = {"kind": "tmux", "pane_id": "%12", "socket": "/tmp/exact-socket"}
        status.write_text(json.dumps({"pid": 41, "child_pid": 42, "state": "running",
                                      "terminal": terminal, "title": "exact frontend"}), encoding="utf-8")
        processes = {41: SimpleNamespace(started_at=5.0), 42: SimpleNamespace(started_at=6.0)}
        identity = RECOVERY._wrapper_identity(status, processes.get)
        self.assertEqual({**terminal, 'binding': {'pid': 41, 'started_at': 5.0}}, identity['terminal'])
        self.assertEqual('exact frontend', identity['title'])

    def test_untracked_receipt_is_released_only_by_confirmation_after_pending_settles(self):
        receipt = RECOVERY._claim(Host.CODEX, "exact_id", "profile")
        with self.assertRaises(ValueError):
            RECOVERY._claim(Host.CODEX, "exact_id", "profile", confirm_closed=True)
        RECOVERY._receipt_state(receipt, Host.CODEX, "exact_id", "profile", "launched")
        with self.assertRaises(ValueError):
            RECOVERY._claim(Host.CODEX, "exact_id", "profile")
        self.assertTrue(RECOVERY._claim(Host.CODEX, "exact_id", "profile", confirm_closed=True).is_file())

    def test_verified_launch_records_its_wrapper_identity(self):
        self.write_codex()
        cli = RECOVERY._load("agent_cli")
        roots = {Host.CODEX: self.root / "sessions"}
        wrapper = {"pid": 41, "started_at": 5.0}
        with mock.patch.object(RECOVERY, "liveness", return_value="closed"), \
                mock.patch.object(RECOVERY, "_wrapper_identity", return_value=wrapper), \
                mock.patch.object(CODEX_SESSIONS, "verify_resume"), \
                mock.patch.object(cli, "resolve_codex_executable", return_value=("native-codex", {})), \
                mock.patch.object(cli, "launch_tab"):
            RECOVERY.open_session(Host.CODEX, "exact_id", roots=roots, cli=cli)
        receipt = RECOVERY._receipt(Host.CODEX, "exact_id", RECOVERY._profile(roots[Host.CODEX]))
        recorded = json.loads(receipt.read_text(encoding="utf-8"))
        self.assertEqual(("launched", wrapper), (recorded["state"], recorded["wrapper"]))

    def test_two_processes_retire_one_completed_receipt_once(self):
        registry = RECOVERY._registry_root() / "race_id.json"
        registry.parent.mkdir(parents=True)
        registry.write_text(json.dumps({"pid": 7, "pid_started_at": 1, "host": "codex", "started_at": 1}), encoding="utf-8")
        receipt = RECOVERY._claim(Host.CODEX, "race_id", "profile")
        created = json.loads(receipt.read_text(encoding="utf-8"))["created_at"]
        registry.write_text(json.dumps({"pid": 999999, "pid_started_at": 2, "host": "codex", "started_at": created + 1}), encoding="utf-8")
        code = "import importlib.util,sys; p=sys.argv[1]; s=importlib.util.spec_from_file_location('r',p); m=importlib.util.module_from_spec(s); s.loader.exec_module(m);\ntry: m._claim(m.Host.CODEX,'race_id','profile'); print('ok')\nexcept ValueError: print('blocked')"
        env = dict(os.environ)
        commands = [[sys.executable, "-c", code, str(ROOT / "plugins/agent/.agents/agent/runtime/agent_recovery.py")] for _ in range(2)]
        results = [subprocess.Popen(command, stdout=subprocess.PIPE, text=True, env=env) for command in commands]
        output = [process.communicate(timeout=20)[0].strip() for process in results]
        self.assertEqual(sorted(output), ["blocked", "ok"])

    def _legacy_open_uses_exact_resume_cwd_and_refuses_unknown_without_confirmation(self):
        self.write_codex()
        cli = mock.Mock()
        cli.resolve_tab_directory.side_effect = lambda value: Path(value)
        cli.resolve_codex_executable.return_value = ("native-codex", {})
        real_load = RECOVERY._load
        with mock.patch.object(RECOVERY, "_load", side_effect=lambda name: cli if name == "agent_cli" else real_load(name)), \
             mock.patch.object(CODEX_SESSIONS, "verify_resume"):
            with self.assertRaises(ValueError):
                RECOVERY.open_session(Host.CODEX, "exact_id", roots={Host.CODEX: self.root / "sessions"})
            RECOVERY.open_session(Host.CODEX, "exact_id", roots={Host.CODEX: self.root / "sessions"}, confirm_closed=True)
        cli.launch_tab.assert_called_once_with(self.cwd, "native-codex", mock.ANY,
                                                ["resume", "exact_id", "Continue the requested work."], clear=("TERM",),
                                                force={"CODEX_HOME": RECOVERY._profile(self.root / "sessions")})

    def _legacy_open_main_preserves_codex_task_recovery_authority_and_limits_file(self):
        self.write_codex()
        prompt = self.root / "recovery prompt é ' $().txt"
        content = "Observed follow-up blocker: proxy said 'unavailable'; $(retry) & <none>."
        content = "\n".join((
            content,
            "Canonical Goal: C:/private/GOAL.md",
            "Actual authorization: User authorized exact-owner recovery.",
            "Provenance: recorded user request.",
            "Authorized scope: inspect and reopen the exact owner.",
            "Limits: no settings changes.",
            "Holds: approval review remains a gate.",
            "Remaining gates: verify owner identity.",
        ))
        prompt.write_text(content, encoding="utf-8")
        original = prompt.read_bytes()
        cli = RECOVERY._load("agent_cli")
        with mock.patch.object(cli, "resolve_codex_executable", return_value=("native-codex", {})), \
             mock.patch.object(cli, "launch_tab") as launch, \
             mock.patch.object(CODEX_SESSIONS, "verify_resume"), \
             contextlib.redirect_stdout(io.StringIO()):
            code = RECOVERY.main([
                "open", "--host", "codex", "--id", "exact_id", "--confirm-closed",
                "--codex-history-root", str(self.root / "sessions"), "--prompt-path", str(prompt),
            ])
        self.assertEqual(0, code)
        arguments = launch.call_args.args[3]
        self.assertEqual("resume", arguments[0])
        self.assertEqual("exact_id", arguments[1])
        self.assertEqual(
            f"Read the file at {prompt.resolve()} and follow its instructions, working from the current directory.",
            arguments[2],
        )
        self.assertNotIn(content, arguments)
        self.assertEqual(original, prompt.read_bytes())

    def test_open_session_preserves_claude_task_recovery_authority_and_limits_file(self):
        profile = self.root / "alternate profile"
        projects = profile / "projects"
        self.write_claude(projects)
        prompt = self.root / "claude recovery authority.txt"
        content = """Host: claude
Native session ID: claude_id
Canonical Goal: C:/private/GOAL.md
Actual authorization: User authorized exact-owner recovery.
Provenance: recorded user request.
Authorized scope: inspect and reopen the exact owner.
Limits: no settings changes.
Holds: approval review remains a gate.
Remaining gates: verify owner identity.
Observed blocker: no send was attempted."""
        prompt.write_text(content, encoding="utf-8")
        original = prompt.read_bytes()
        cli = RECOVERY._load("agent_cli")
        with mock.patch.object(cli, "open_claude_tab") as launch:
            RECOVERY.open_session(
                Host.CLAUDE,
                "claude_id",
                prompt=None,
                roots={Host.CLAUDE: projects},
                confirm_closed=True,
                prompt_path=prompt,
                cli=cli,
            )
        arguments = launch.call_args.args[2]
        self.assertEqual("--resume", arguments[0])
        self.assertEqual("claude_id", arguments[1])
        self.assertEqual(
            f"Read the file at {prompt.resolve()} and follow its instructions, working from the current directory.",
            arguments[2],
        )
        self.assertNotIn(content, arguments)
        self.assertEqual(original, prompt.read_bytes())

    def test_explicit_claude_profile_overrides_conflicting_environment_for_resume(self):
        profile = self.root / "alternate profile"
        projects = profile / "projects"
        self.write_claude(projects)
        cli = mock.Mock()
        cli.resolve_tab_directory.side_effect = lambda value: Path(value)
        real_load = RECOVERY._load
        with mock.patch.dict("os.environ", {"CLAUDE_CONFIG_DIR": "wrong profile"}, clear=False), \
             mock.patch.object(RECOVERY, "_load", side_effect=lambda name: cli if name == "agent_cli" else real_load(name)):
            RECOVERY.open_session(Host.CLAUDE, "claude_id", roots={Host.CLAUDE: projects}, confirm_closed=True)
        environment = cli.open_claude_tab.call_args.kwargs
        self.assertEqual(environment["force"], {"CLAUDE_CONFIG_DIR": RECOVERY._profile(projects)})
        self.assertNotIn("clear", environment)

    def test_claude_resume_re_passes_native_arguments_before_the_session(self):
        projects = self.root / "native profile" / "projects"
        self.write_claude(projects)
        cli = mock.Mock()
        cli.resolve_tab_directory.side_effect = lambda value: Path(value)
        RECOVERY.open_session(Host.CLAUDE, "claude_id", prompt="Continue.", roots={Host.CLAUDE: projects}, confirm_closed=True,
                              cli=cli, native_arguments=["--add-dir", "C:/extra", "--settings", "s.json"])
        self.assertEqual(["--add-dir", "C:/extra", "--settings", "s.json", "--resume", "claude_id", "Continue."],
                         cli.open_claude_tab.call_args.args[2])

    def test_default_claude_profile_resumes_without_config_dir(self):
        home = self.root / "home"
        projects = home / ".claude" / "projects"
        self.write_claude(projects)
        cli = mock.Mock()
        cli.resolve_tab_directory.side_effect = lambda value: Path(value)
        real_load = RECOVERY._load
        with mock.patch.dict("os.environ", {"CLAUDE_CONFIG_DIR": str(home / ".claude")}, clear=False), \
             mock.patch.object(RECOVERY.Path, "home", return_value=home), \
             mock.patch.object(RECOVERY, "_load", side_effect=lambda name: cli if name == "agent_cli" else real_load(name)):
            RECOVERY.open_session(Host.CLAUDE, "claude_id", roots={Host.CLAUDE: projects}, confirm_closed=True)
        environment = cli.open_claude_tab.call_args.kwargs
        self.assertEqual(environment["clear"], ("CLAUDE_CONFIG_DIR",))
        self.assertNotIn("force", environment)

    def store_session(self, session="01a1415f-6448", title="Personal workflow policy research", work_title=None,
                      started_at=1000.0, native=True):
        if native:
            rollout = self.root / "codex profile" / "sessions" / "2026" / f"rollout-x-{session}.jsonl"
            rollout.parent.mkdir(parents=True, exist_ok=True)
            events = [
                {"type": "session_meta", "payload": {"id": session, "cwd": str(self.cwd), "source": "cli"}},
                {"type": "response_item", "payload": {"type": "message", "role": "user",
                                                      "content": [{"type": "input_text", "text": "policy"}]}},
            ]
            rollout.write_text("\n".join(json.dumps(event) for event in events), encoding="utf-8")
        store = RECOVERY._load("session_store")
        connection = store.connect()
        try:
            if work_title:
                store.upsert_work_item(connection, "github.com/tj-agents/docs:problem-management/personal-policy",
                                       "github.com/tj-agents/docs", "problem-management", "personal-policy",
                                       status="active", title=work_title, plan_path="plans/problem-management/PLAN.md")
            connection.execute(
                "INSERT OR REPLACE INTO sessions (handle, host, session_id, profile, title, cwd, repo, branch, work_item, "
                "started_at, last_activity_at) VALUES (?, 'codex', ?, ?, ?, ?, 'github.com/tj-agents/docs', 'main', ?, ?, ?)",
                (f"codex:{session}", session, str(self.root / "codex profile"), title, str(self.cwd),
                 "github.com/tj-agents/docs:problem-management/personal-policy" if work_title else None,
                 started_at, started_at),
            )
            connection.commit()
        finally:
            connection.close()

    def test_topic_search_reads_sqlite_work_items_without_scanning_history(self):
        self.store_session(work_title="Optional GitHub work-items package and personal adoption")
        self.store_session("other_id", title="Unrelated session")
        history = RECOVERY._load("history")
        with mock.patch.dict(os.environ, {"CODEX_HOME": str(self.root / "unscanned profile")}):
            results = RECOVERY.search(topic="work items GitHub")
        self.assertEqual(["01a1415f-6448"], [item["id"] for item in results])
        self.assertEqual("sessions.db", results[0]["source"])
        self.assertIn("work_item.title", results[0]["matched_by"])
        self.assertEqual("plans/problem-management/PLAN.md", results[0]["work_item"]["plan_path"])

    def test_sqlite_ignores_path_words_and_sessions_without_native_history(self):
        self.store_session(work_title="Optional GitHub work-items package and personal adoption")
        self.store_session("unresumable", title="Personal workflow policy research", native=False)
        history = RECOVERY._load("history")
        with mock.patch.dict(os.environ, {"CODEX_HOME": str(self.root / "missing profile")}):
            self.assertEqual(["01a1415f-6448"], [item["id"] for item in RECOVERY.search(topic="personal workflow")])
            with self.assertRaises(ValueError):
                RECOVERY.search((Host.CODEX,), topic="synthetic recovery")

    def test_sqlite_checkout_filter_and_history_fallback(self):
        self.store_session(title="Personal workflow policy research")
        other = self.root / "other checkout"
        other.mkdir()
        self.assertEqual(["01a1415f-6448"], [item["id"] for item in RECOVERY.search(checkout=str(self.cwd))])
        history = RECOVERY._load("history")
        with mock.patch.dict(os.environ, {"CODEX_HOME": str(self.root / "missing profile")}):
            with self.assertRaises(ValueError):
                RECOVERY.search((Host.CODEX,), checkout=str(other))
        self.write_codex()
        results = RECOVERY.search((Host.CODEX,), topic="launch topic", roots={Host.CODEX: self.root / "sessions"})
        self.assertEqual(("native-history", ["message"]), (results[0]["source"], results[0]["matched_by"]))

    def test_find_uses_the_recorded_session_before_native_history(self):
        self.store_session()
        history = RECOVERY._load("history")
        with mock.patch.dict(os.environ, {"CODEX_HOME": str(self.root / "unscanned profile")}):
            item = RECOVERY.find(Host.CODEX, "01a1415f-6448")
        self.assertEqual(str(self.cwd), item["cwd"])
        self.assertEqual(Path(item["profile"]), (self.root / "codex profile").resolve())

    def test_find_does_not_resume_a_recorded_session_without_cli_history(self):
        self.store_session("helper_id", native=False)
        rollout = self.root / "codex profile" / "sessions" / "rollout-x-helper_id.jsonl"
        rollout.parent.mkdir(parents=True, exist_ok=True)
        rollout.write_text(json.dumps({"type": "session_meta", "payload": {
            "id": "helper_id", "cwd": str(self.cwd), "source": "exec"}}), encoding="utf-8")
        history = RECOVERY._load("history")
        with mock.patch.dict(os.environ, {"CODEX_HOME": str(self.root / "missing profile")}):
            with self.assertRaises(ValueError):
                RECOVERY.find(Host.CODEX, "helper_id")

    def run_pickup(self, statuses=(), registered_after=None, history_after=None, timeout=10, wrapper_timeout=3):
        self.launches = getattr(self, "launches", 0) + 1
        session = f"pickup_{self.launches}"
        status = self.root / f"{session}.status.json"
        history = self.root / f"{session}.jsonl"
        history.write_text("{}", encoding="utf-8")
        before = history.stat().st_mtime
        now = [0.0]
        states = list(statuses)

        def wait(seconds):
            now[0] += seconds
            if states and now[0] >= states[0][0]:
                status.write_text(json.dumps(states.pop(0)[1]), encoding="utf-8")
            if registered_after is not None and now[0] >= registered_after:
                self.store_session(session, started_at=before)
            if history_after is not None and now[0] >= history_after:
                os.utime(history, (before + 5, before + 5))

        return AWAIT_PICKUP(Host.CODEX, session, status, before - 1, history, timeout=timeout,
                            wrapper_timeout=wrapper_timeout, interval=1, clock=lambda: now[0], wait=wait)

    def test_pickup_is_verified_by_registration_or_history_growth_once_running(self):
        running = [(1, {"state": "preparing"}), (6, {"state": "running"})]
        self.assertEqual("registered in sessions.db", self.run_pickup(running, registered_after=4))
        self.assertEqual("native history written", self.run_pickup(running, history_after=4))
        with self.assertRaises(RECOVERY.LaunchUnverified):
            self.run_pickup([(1, {"state": "preparing"})], registered_after=4)
        with self.assertRaisesRegex(RECOVERY.LaunchFailed, "acknowledge"):
            self.run_pickup([(1, {"state": "preparing"}), (6, {"state": "failed", "error": "did not acknowledge"})],
                            registered_after=4)

    def test_a_session_that_resumed_and_exited_counts_as_picked_up(self):
        exited = {"state": "exited", "returncode": 0, "running_at": 2}
        self.assertEqual("registered in sessions.db",
                         self.run_pickup([(1, {"state": "preparing"}), (2, exited)], registered_after=2))
        with self.assertRaisesRegex(RECOVERY.LaunchFailed, "exited with 0"):
            self.run_pickup([(1, {"state": "preparing"}), (2, exited)])

    def test_status_files_are_created_once_with_content(self):
        status = RECOVERY._load("launch_status")
        path = self.root / "attempt.status.json"
        self.assertTrue(status.create(path, {"state": "preparing"}))
        self.assertFalse(status.create(path, {"state": "abandoned"}))
        self.assertEqual({"state": "preparing"}, status.read(path))
        self.assertTrue(status.replace(path, {"state": "running", "running_at": 1}))
        self.assertEqual("running", status.read(path)["state"])
        receipt = RECOVERY._receipt(Host.CODEX, "exact_id", "profile")
        self.assertNotEqual(RECOVERY._status_path(receipt), RECOVERY._status_path(receipt))

    def test_registration_without_any_wrapper_status_is_not_a_crash(self):
        with self.assertRaisesRegex(RECOVERY.LaunchFailed, "never started"):
            self.run_pickup(registered_after=1)

    def test_an_unresumable_store_row_does_not_hide_native_matches(self):
        self.store_session("ghost_id", title="recover launch topic", native=False)
        self.write_codex()
        history = RECOVERY._load("history")
        with mock.patch.dict(os.environ, {"CODEX_HOME": str(self.root)}):
            results = RECOVERY.search((Host.CODEX,), topic="launch topic")
        self.assertEqual(["exact_id"], [item["id"] for item in results])
        self.assertEqual("native-history", results[0]["source"])

    def test_live_rollout_wins_over_its_archived_copy(self):
        profile = self.root / "profile"
        live = profile / "sessions" / "2026" / "rollout-a-same_id.jsonl"
        archived = profile / "archived_sessions" / "rollout-a-same_id.jsonl"
        for path in (archived, live):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("{}", encoding="utf-8")
        self.assertEqual(live, RECOVERY._history_file(Host.CODEX, "same_id", profile))

    def test_pickup_failures_carry_the_cause(self):
        with self.assertRaisesRegex(RECOVERY.LaunchFailed, "unsupported value"):
            self.run_pickup([(1, {"state": "preparing"}), (3, {"state": "failed", "error": "unsupported value"})])
        with self.assertRaisesRegex(RECOVERY.LaunchFailed, "never started"):
            self.run_pickup()
        self.assertEqual("abandoned", json.loads(
            (self.root / f"pickup_{self.launches}.status.json").read_text(encoding="utf-8"))["state"])
        self.assertEqual("registered in sessions.db", self.run_pickup(
            [(3, {"state": "preparing"}), (4, {"state": "running"})], registered_after=4, wrapper_timeout=3))
        with self.assertRaisesRegex(RECOVERY.LaunchFailed, "exited with 1"):
            self.run_pickup([(1, {"state": "exited", "returncode": 1})])
        with self.assertRaises(RECOVERY.LaunchUnverified):
            self.run_pickup([(1, {"state": "preparing"})])

    def test_failed_launch_releases_its_receipt_and_unverified_keeps_it_ambiguous(self):
        self.write_codex()
        cli = RECOVERY._load("agent_cli")
        roots = {Host.CODEX: self.root / "sessions"}
        for outcome, remaining in ((RECOVERY.LaunchFailed("tab never ran"), None),
                                   (RECOVERY.LaunchUnverified("not resumed"), "ambiguous")):
            with self.subTest(outcome=type(outcome).__name__), \
                    mock.patch.object(RECOVERY, "await_pickup", side_effect=outcome), \
                    mock.patch.object(RECOVERY, "liveness", return_value="closed"), \
                    mock.patch.object(CODEX_SESSIONS, "verify_resume"), \
                    mock.patch.object(cli, "resolve_codex_executable", return_value=("native-codex", {})), \
                    mock.patch.object(cli, "launch_tab"):
                with self.assertRaises(type(outcome)):
                    RECOVERY.open_session(Host.CODEX, "exact_id", roots=roots, cli=cli)
                receipt = RECOVERY._receipt(Host.CODEX, "exact_id", RECOVERY._profile(roots[Host.CODEX]))
                recorded = json.loads(receipt.read_text(encoding="utf-8")) if receipt.exists() else {}
                self.assertEqual(remaining, recorded.get("state"))
                self.assertEqual(remaining is not None, bool(recorded.get("status_file")))
                receipt.unlink(missing_ok=True)

    def test_main_exits_one_for_failed_and_three_for_unverified_launches(self):
        for error, code in ((RECOVERY.LaunchFailed("native codex exited"), 1), (RECOVERY.LaunchUnverified("x"), 3)):
            output = io.StringIO()
            with self.subTest(code=code), mock.patch.object(RECOVERY, "open_session", side_effect=error), \
                    contextlib.redirect_stderr(output):
                self.assertEqual(code, RECOVERY.main(["open", "--host", "codex", "--id", "exact_id"]))
            self.assertIn(str(error), output.getvalue())

    def test_loader_caches_native_exception_identity(self):
        self.assertIs(RECOVERY._load("agent_cli").LaunchTimeout, RECOVERY._load("agent_cli").LaunchTimeout)

    def test_main_reports_native_launch_failure(self):
        cli = RECOVERY._load("agent_cli")
        with mock.patch.object(RECOVERY, "open_session", side_effect=cli.LaunchError("terminal unavailable")), \
             contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(RECOVERY.main(["open", "--host", "codex", "--id", "exact_id"]), 1)

    def test_main_reports_native_launch_timeout_as_three(self):
        cli = RECOVERY._load("agent_cli")
        with mock.patch.object(RECOVERY, "open_session", side_effect=cli.LaunchTimeout("unknown launch")), \
             contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(RECOVERY.main(["open", "--host", "codex", "--id", "exact_id"]), 3)

    def test_recover_reports_partial_launch_without_losing_timeout_exit(self):
        cli = RECOVERY._load("agent_cli")
        items = [{"host": "codex", "id": "first_id"}, {"host": "codex", "id": "second_id"}]
        output = io.StringIO()
        with mock.patch.object(RECOVERY, "search", return_value=items), \
             mock.patch.object(RECOVERY, "open_session", side_effect=[items[0], cli.LaunchTimeout("unknown second launch")]), \
             contextlib.redirect_stderr(output):
            self.assertEqual(RECOVERY.main(["recover", "--host", "codex", "--select", "codex:first_id", "--select", "codex:second_id"]), 3)
        self.assertIn("Already opened: codex:first_id", output.getvalue())

    def test_recover_rejects_duplicate_selection_before_launch(self):
        item = {"host": "codex", "id": "exact_id"}
        with mock.patch.object(RECOVERY, "search", return_value=[item]), \
             mock.patch.object(RECOVERY, "open_session") as opened, \
             contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(RECOVERY.main(["recover", "--host", "codex", "--select", "codex:exact_id", "--select", "codex:exact_id"]), 1)
        opened.assert_not_called()

    def roots(self):
        claude = self.root / "claude projects"
        return claude, {Host.CODEX: self.root / "sessions", Host.CLAUDE: claude}

    def test_reopen_resolves_an_exact_id_in_either_host_without_a_host_argument(self):
        self.write_codex()
        claude, roots = self.roots()
        self.write_claude(claude)
        self.assertEqual("codex", RECOVERY.resolve_target("exact_id", roots=roots)["host"])
        self.assertEqual("claude", RECOVERY.resolve_target("claude_id", roots=roots)["host"])

    def test_reopen_resolves_a_topic_and_a_directory_across_hosts_and_refuses_ambiguity(self):
        self.write_codex()
        claude, roots = self.roots()
        self.write_claude(claude)
        self.assertEqual("claude_id", RECOVERY.resolve_target("Claude recovery title", roots=roots)["id"])
        with self.assertRaisesRegex(ValueError, "More than one session matched"):
            RECOVERY.resolve_target(str(self.cwd), roots=roots)
        self.assertIn(RECOVERY.resolve_target(str(self.cwd), roots=roots, pick_latest=True)["id"], {"exact_id", "claude_id"})
        with self.assertRaisesRegex(ValueError, "No Claude or Codex session matched"):
            RECOVERY.resolve_target("no such topic", roots=roots)

    def test_latest_prefers_a_live_match_over_a_newer_closed_one(self):
        found = [{"host": "claude", "id": "new", "status": "closed", "activity": "2", "cwd": str(self.cwd), "title": None, "preview": ""},
                 {"host": "codex", "id": "old", "status": "live", "activity": "1", "cwd": str(self.cwd), "title": None, "preview": ""}]
        with mock.patch.object(RECOVERY, "search", side_effect=lambda hosts, *_: [item for item in found if Host(item["host"]) in hosts]):
            self.assertEqual("old", RECOVERY.resolve_target("topic", pick_latest=True)["id"])

    def test_reopen_bare_word_is_a_topic_even_when_a_directory_has_that_name(self):
        self.write_codex()
        _, roots = self.roots()
        (self.root / "topic_dir_probe").mkdir()
        previous = Path.cwd()
        os.chdir(self.root)
        self.addCleanup(os.chdir, previous)
        with mock.patch.object(RECOVERY, "search", return_value=[]) as search:
            with self.assertRaises(ValueError):
                RECOVERY.resolve_target("topic_dir_probe", roots=roots)
        self.assertEqual(("topic_dir_probe", None), search.call_args.args[1:3])

    def test_reopen_closed_session_opens_in_its_own_host(self):
        self.write_codex()
        claude, roots = self.roots()
        self.write_claude(claude)
        focus = mock.Mock()
        with mock.patch.object(RECOVERY, "liveness", return_value="closed"), \
             mock.patch.object(RECOVERY, "open_session", return_value={"host": "claude", "id": "claude_id"}) as opened:
            result = RECOVERY.reopen("claude_id", roots=roots, focus=focus)
        self.assertEqual("opened", result["action"])
        self.assertEqual((Host.CLAUDE, "claude_id"), opened.call_args.args[:2])
        focus.assert_not_called()

    def test_reopen_closed_codex_enters_verified_startup_before_native_resume(self):
        self.write_codex()
        cli = RECOVERY._load("agent_cli")
        focus = mock.Mock()
        with mock.patch.object(RECOVERY, "liveness", return_value="closed"), \
             mock.patch.object(CODEX_SESSIONS, "verify_resume"), \
             mock.patch.object(cli, "resolve_codex_executable", return_value=("native-codex", {})), \
             mock.patch.object(cli, "launch_tab") as launch:
            result = RECOVERY.reopen("exact_id", hosts=(Host.CODEX,), roots={Host.CODEX: self.root / "sessions"},
                                     focus=focus, cli=cli)
        self.assertEqual("opened", result["action"])
        launch.assert_called_once()
        directory, executable, _title, arguments = launch.call_args.args
        self.assertEqual(self.cwd, directory)
        self.assertEqual(sys.executable, executable)
        self.assertEqual("startup_binding.py", Path(arguments[2]).name)
        self.assertEqual(["--host", "codex", "--project", str(self.cwd), "--target", "native-codex"], arguments[3:9])
        self.assertEqual("--status-file", arguments[9])
        self.assertEqual("codex-", Path(arguments[10]).name[:6])
        self.assertEqual(["--", "resume", "exact_id", "Continue the requested work."], arguments[11:])
        focus.assert_not_called()

    def test_reopen_live_session_focuses_its_tab_and_never_launches_a_second_writer(self):
        self.write_codex()
        _, roots = self.roots()
        focus = mock.Mock()
        with mock.patch.object(RECOVERY, "liveness", return_value="live"), \
             mock.patch.object(RECOVERY, "open_session") as opened:
            result = RECOVERY.reopen("exact_id", roots=roots, focus=focus)
        self.assertEqual("focused", result["action"])
        focus.assert_called_once_with(Host.CODEX, "exact_id")
        opened.assert_not_called()

    def test_reopen_live_session_with_unfocusable_tab_fails_without_launching(self):
        self.write_codex()
        _, roots = self.roots()
        focus = mock.Mock(side_effect=ValueError("could not be focused"))
        with mock.patch.object(RECOVERY, "liveness", return_value="live"), \
             mock.patch.object(RECOVERY, "open_session") as opened:
            with self.assertRaisesRegex(ValueError, "could not be focused"):
                RECOVERY.reopen("exact_id", roots=roots, focus=focus)
        opened.assert_not_called()

    def test_reopen_unknown_session_is_still_refused_without_confirmation(self):
        self.write_codex()
        _, roots = self.roots()
        focus = mock.Mock()
        with self.assertRaisesRegex(ValueError, "conflicting or incomplete live identity"):
            RECOVERY.reopen("exact_id", roots=roots, focus=focus)
        focus.assert_not_called()

    def test_focus_session_runs_the_peer_cli_focus_and_reports_failure(self):
        done = subprocess.CompletedProcess([], 0, "", "")
        with mock.patch.object(RECOVERY.subprocess, "run", return_value=done) as run:
            RECOVERY.focus_session(Host.CODEX, "exact_id")
        self.assertEqual(["--session", "exact_id", "--focus"], run.call_args.args[0][-3:])
        failed = subprocess.CompletedProcess([], 2, "", "close-tab: absent")
        with mock.patch.object(RECOVERY.subprocess, "run", return_value=failed):
            with self.assertRaisesRegex(ValueError, "absent"):
                RECOVERY.focus_session(Host.CODEX, "exact_id")

    def test_reopen_main_requires_a_target(self):
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(1, RECOVERY.main(["reopen"]))
        self.assertIn("--target", err.getvalue())


if __name__ == "__main__":
    unittest.main()
