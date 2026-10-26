import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "work_item_sessions", ROOT / ".agents/agent/runtime/work_item_sessions.py"
)
WIS = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(WIS)

STORE = WIS._session_store()
REAL_RECOVERY = WIS._agent_recovery()
GIT = shutil.which("git")


def _git_environment():
    environment = os.environ.copy()
    for name in tuple(environment):
        if name == "GIT_CONFIG_COUNT" or name.startswith(("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_")):
            environment.pop(name)
    return environment


def fake_recovery(liveness_result="closed", open_session=None):
    return types.SimpleNamespace(
        SESSION_ID=REAL_RECOVERY.SESSION_ID,
        liveness=mock.Mock(return_value=liveness_result),
        open_session=open_session if open_session is not None else mock.Mock(return_value={"ok": True}),
    )


@unittest.skipUnless(GIT, "git is required for work-item session tests")
class WorkItemSessionsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="work item sessions é ")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.state = self.root / "state"
        self.environ = mock.patch.dict(os.environ, {"AGENT_STATE_DIRECTORY": str(self.state)}, clear=False)
        self.environ.start()
        self.addCleanup(self.environ.stop)
        self.repo = self.root / "repo with spaces"
        self.repo.mkdir()
        self._run_git(self.repo, "init")
        self._run_git(self.repo, "config", "user.email", "test@example.com")
        self._run_git(self.repo, "config", "user.name", "Test User")
        (self.repo / "README.md").write_text("hello\n", encoding="utf-8")
        self._run_git(self.repo, "add", "README.md")
        self._run_git(self.repo, "commit", "-m", "initial")
        self._run_git(self.repo, "remote", "add", "origin", "https://github.com/tj-agents/core.git")

    def _run_git(self, cwd, *args):
        result = subprocess.run(
            ["git", "-C", str(cwd), *args], capture_output=True, text=True, timeout=15,
            env=_git_environment(),
        )
        if result.returncode != 0:
            raise RuntimeError(result.stderr)
        return result.stdout.strip()

    def _register_current_session(self, host, session_id, title):
        connection = self._connection()
        try:
            STORE.start_session(
                connection, host, session_id, self.repo, title=title,
                pid=os.getpid(), pid_started_at=time.time(),
            )
        finally:
            connection.close()

    def _write_plan(self, name="GOAL.md"):
        path = self.repo / "plans" / "work-item-sessions" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# Plan\n", encoding="utf-8")
        return path.relative_to(self.repo).as_posix()


    def _claim_claude(self, session_id="a2bcd5c4-bf6d-4087-95e3-d7ba7f711875", **kwargs):
        with mock.patch.dict(os.environ, {
            "CLAUDE_CODE_SESSION_ID": session_id,
            "CLAUDE_CONFIG_DIR": str(self.root / "claude-profile"),
        }, clear=False):
            self._register_current_session("claude", session_id, "Claude tab")
            return WIS.cmd_claim(
                kwargs.pop("work_item", "example-feature/example-item"), "claude",
                role=kwargs.pop("role", "owner"), predecessor=kwargs.pop("predecessor", None),
                status=kwargs.pop("status", "active"), title=kwargs.pop("title", "Example task"),
                plan_path=kwargs.pop("plan_path", None), cwd=self.repo,
            )

    def _claim_codex(self, session_id="01a12aa7-5b58-7312-9400-ef710762147f", **kwargs):
        with mock.patch.dict(os.environ, {
            "CODEX_THREAD_ID": session_id,
            "CODEX_HOME": str(self.root / "codex-profile"),
        }, clear=False):
            self._register_current_session("codex", session_id, "Codex tab")
            return WIS.cmd_claim(
                kwargs.pop("work_item", "example-feature/example-item"), "codex",
                role=kwargs.pop("role", "bounded: docs"), predecessor=kwargs.pop("predecessor", None),
                status=kwargs.pop("status", "active"), title=kwargs.pop("title", "Example task"),
                plan_path=kwargs.pop("plan_path", None), cwd=self.repo,
            )

    def _connection(self):
        return STORE.connect()

    def test_peer_loader_finds_a_packaged_session_close(self):
        package_root = self.root / "agent-package"
        candidate = package_root / ".agents" / "agent" / "utility" / "peer" / "scripts" / "session_close.py"
        candidate.parent.mkdir(parents=True)
        candidate.touch()
        with mock.patch.object(WIS, "_package_root", return_value=package_root):
            self.assertEqual(candidate, WIS._peer_cli_script("session_close"))

    def test_recover_agents_facade_exposes_the_native_session_cli(self):
        facade = ROOT / ".agents/agent/utility/recover/scripts/work_item_sessions.py"
        result = subprocess.run(
            [sys.executable, str(facade), "--help"], capture_output=True, text=True, timeout=15,
            env=_git_environment(),
        )
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("register", result.stdout)
        self.assertIn("bind", result.stdout)

    def test_claim_creates_work_item_and_owner(self):
        self._claim_claude()
        connection = self._connection()
        try:
            item = STORE.get_work_item(connection, "github.com/tj-agents/core:example-feature/example-item")
            self.assertEqual("active", item["status"])
            row = STORE.get_session(connection, "claude:a2bcd5c4-bf6d-4087-95e3-d7ba7f711875")
            self.assertEqual("owner", row["role"])
        finally:
            connection.close()

    def test_native_reference_selectors_register_claim_and_status_without_local_aliases(self):
        registered = WIS.cmd_register("GitHub", "GitHub.com/Owner/Repository", "42", cwd=self.repo)
        execution = registered["execution_key"]
        self.assertTrue(registered["created"])
        self.assertEqual(execution, registered["work_item"])
        self.assertEqual(
            {"provider": "github", "scope": "github.com/owner/repository", "native_id": "42"},
            registered["reference"],
        )

        session_id = "01a12aa7-5b58-7312-9400-ef710762147f"
        with mock.patch.dict(os.environ, {"CODEX_THREAD_ID": session_id}, clear=False):
            self._register_current_session("codex", session_id, "Codex tab")
            claimed = WIS.cmd_claim(
                host="codex", provider="github", scope="github.com/owner/repository", native_id="42",
                cwd=self.repo,
            )
        self.assertEqual(execution, claimed["execution_key"])
        self.assertEqual(execution, claimed["work_item"])

        output = io.StringIO()
        with mock.patch.object(sys, "stdout", output):
            self.assertEqual(0, WIS.main(["status", "--execution-key", execution]))
        self.assertEqual(execution, json.loads(output.getvalue())["execution_key"])

        output = io.StringIO()
        with mock.patch.object(sys, "stdout", output):
            self.assertEqual(0, WIS.main([
                "status", "--provider", "github", "--scope", "github.com/owner/repository", "--native-id", "42",
            ]))
        self.assertEqual(execution, json.loads(output.getvalue())["execution_key"])

        output = io.StringIO()
        with mock.patch.object(sys, "stdout", output):
            self.assertEqual(1, WIS.main(["status", "example-feature/example-item", "--execution-key", execution]))
        self.assertIn("select exactly one", json.loads(output.getvalue())["error"])

    def test_native_claim_requires_an_explicit_registration(self):
        session_id = "01a12aa7-5b58-7312-9400-ef710762147f"
        with mock.patch.dict(os.environ, {"CODEX_THREAD_ID": session_id}, clear=False):
            self._register_current_session("codex", session_id, "Codex tab")
            with self.assertRaisesRegex(ValueError, "register the remote reference"):
                WIS.cmd_claim(
                    host="codex", provider="github", scope="github.com/owner/repository", native_id="42",
                    cwd=self.repo,
                )
        connection = self._connection()
        try:
            self.assertEqual(0, connection.execute("SELECT COUNT(*) FROM work_items").fetchone()[0])
            self.assertEqual(0, connection.execute("SELECT COUNT(*) FROM work_item_references").fetchone()[0])
        finally:
            connection.close()

    def test_cross_repository_native_claim_keeps_the_execution_home_metadata(self):
        self._run_git(self.repo, "checkout", "-b", "home-branch")
        registered = WIS.cmd_register("github", "github.com/owner/repository", "42", cwd=self.repo)
        execution = registered["execution_key"]
        connection = self._connection()
        try:
            home = STORE.get_work_item(connection, execution)
        finally:
            connection.close()

        other = self.root / "other repository"
        other.mkdir()
        self._run_git(other, "init")
        self._run_git(other, "config", "user.email", "test@example.com")
        self._run_git(other, "config", "user.name", "Test User")
        (other / "README.md").write_text("other\n", encoding="utf-8")
        self._run_git(other, "add", "README.md")
        self._run_git(other, "commit", "-m", "initial")
        self._run_git(other, "remote", "add", "origin", "https://github.com/tj-agents/other.git")
        self._run_git(other, "checkout", "-b", "join-branch")

        session_id = "01a12aa7-5b58-7312-9400-ef710762147f"
        connection = self._connection()
        try:
            self.assertIsNone(STORE.get_session(connection, f"codex:{session_id}"))
        finally:
            connection.close()
        entry = {
            "host": "codex",
            "session_id": session_id,
            "pid": os.getpid(),
            "pid_started_at": time.time(),
            "title": "Joining Codex tab",
        }
        with mock.patch.dict(os.environ, {"CODEX_THREAD_ID": session_id}, clear=False), \
                mock.patch.object(WIS, "_session_close", return_value=types.SimpleNamespace(
                    own_host_and_entry=mock.Mock(return_value=entry),
                )):
            WIS.cmd_claim(
                host="codex", provider="github", scope="github.com/owner/repository", native_id="42", cwd=other,
            )

        connection = self._connection()
        try:
            item = STORE.get_work_item(connection, execution)
            session = STORE.get_session(connection, f"codex:{session_id}")
        finally:
            connection.close()
        self.assertEqual(home["repo"], item["repo"])
        self.assertEqual(home["branch"], item["branch"])
        self.assertEqual("github.com/tj-agents/other", session["repo"])
        self.assertEqual(str(other), session["cwd"])
        self.assertEqual("join-branch", session["branch"])
        self.assertEqual(self._run_git(other, "rev-parse", "HEAD"), session["head"])
        self.assertEqual("Joining Codex tab", session["title"])
        self.assertEqual(execution, session["work_item"])
        match = WIS.cmd_find(str(other))["matches"]
        self.assertEqual(execution, match[0]["work_item"])
        self.assertEqual(execution, match[0]["execution_key"])
        self.assertEqual(
            [{"provider": "github", "scope": "github.com/owner/repository", "native_id": "42"}],
            match[0]["references"],
        )

    def test_native_home_claim_and_item_fill_null_branches_from_feature_branches(self):
        self._run_git(self.repo, "branch", "-M", "main")
        claimed = WIS.cmd_register("github", "github.com/owner/repository", "42", cwd=self.repo)
        self._run_git(self.repo, "checkout", "-b", "native-claim-branch")
        session_id = "01a12aa7-5b58-7312-9400-ef710762147f"
        with mock.patch.dict(os.environ, {"CODEX_THREAD_ID": session_id}, clear=False):
            self._register_current_session("codex", session_id, "Codex tab")
            WIS.cmd_claim(
                host="codex", provider="github", scope="github.com/owner/repository", native_id="42",
                cwd=self.repo,
            )

        self._run_git(self.repo, "checkout", "main")
        updated = WIS.cmd_register("github", "github.com/owner/repository", "43", cwd=self.repo)
        self._run_git(self.repo, "checkout", "-b", "native-item-branch")
        WIS.cmd_item(execution_key=updated["execution_key"], status="active", cwd=self.repo)

        connection = self._connection()
        try:
            claimed_item = STORE.get_work_item(connection, claimed["execution_key"])
            updated_item = STORE.get_work_item(connection, updated["execution_key"])
        finally:
            connection.close()
        self.assertEqual("native-claim-branch", claimed_item["branch"])
        self.assertEqual("native-item-branch", updated_item["branch"])

    def test_foreign_native_plan_paths_reject_claim_and_item_without_mutation(self):
        home_plan = self._write_plan("HOME.md")
        registered = WIS.cmd_register("github", "github.com/owner/repository", "42", cwd=self.repo)
        execution = registered["execution_key"]
        WIS.cmd_item(execution_key=execution, status="active", plan_path=home_plan, cwd=self.repo)
        connection = self._connection()
        try:
            before = STORE.get_work_item(connection, execution)
        finally:
            connection.close()

        other = self.root / "foreign repository"
        other.mkdir()
        self._run_git(other, "init")
        self._run_git(other, "config", "user.email", "test@example.com")
        self._run_git(other, "config", "user.name", "Test User")
        plan = other / "plans" / "FOREIGN.md"
        plan.parent.mkdir()
        plan.write_text("# Foreign\n", encoding="utf-8")
        self._run_git(other, "add", "plans/FOREIGN.md")
        self._run_git(other, "commit", "-m", "initial")
        self._run_git(other, "remote", "add", "origin", "https://github.com/tj-agents/other.git")
        foreign_plan = plan.relative_to(other).as_posix()

        session_id = "01a12aa7-5b58-7312-9400-ef710762147f"
        entry = {
            "host": "codex",
            "session_id": session_id,
            "pid": os.getpid(),
            "pid_started_at": time.time(),
            "title": "Foreign Codex tab",
        }
        with mock.patch.dict(os.environ, {"CODEX_THREAD_ID": session_id}, clear=False), \
                mock.patch.object(WIS, "_session_close", return_value=types.SimpleNamespace(
                    own_host_and_entry=mock.Mock(return_value=entry),
                )):
            with self.assertRaisesRegex(ValueError, "different repository"):
                WIS.cmd_claim(
                    host="codex", role="owner", provider="github", scope="github.com/owner/repository",
                    native_id="42", plan_path=foreign_plan, cwd=other,
                )
        with self.assertRaisesRegex(ValueError, "different repository"):
            WIS.cmd_item(
                execution_key=execution, status="paused", plan_path=foreign_plan, cwd=other,
            )

        connection = self._connection()
        try:
            after = STORE.get_work_item(connection, execution)
            session = STORE.get_session(connection, f"codex:{session_id}")
        finally:
            connection.close()
        self.assertEqual(before, after)
        self.assertEqual(home_plan, after["plan_path"])
        self.assertIsNone(session)

    def test_claim_keeps_durable_metadata_separate_from_host_tab_titles(self):
        plan_path = self._write_plan()
        self._claim_claude(title="Persist durable metadata", plan_path=plan_path)
        self._claim_codex(
            work_item="second-feature/second-item", role="owner", title="Codex durable task",
        )
        connection = self._connection()
        try:
            item = STORE.get_work_item(connection, "github.com/tj-agents/core:example-feature/example-item")
            claude = STORE.get_session(connection, "claude:a2bcd5c4-bf6d-4087-95e3-d7ba7f711875")
            codex = STORE.get_session(connection, "codex:01a12aa7-5b58-7312-9400-ef710762147f")
            STORE.start_session(
                connection, "claude", "a2bcd5c4-bf6d-4087-95e3-d7ba7f711875", self.repo,
                title="Renamed Claude tab",
            )
            reopened = STORE.get_work_item(connection, "github.com/tj-agents/core:example-feature/example-item")
        finally:
            connection.close()
        self.assertEqual("Persist durable metadata", item["title"])
        self.assertEqual(plan_path, item["plan_path"])
        self.assertEqual("Claude tab", claude["title"])
        self.assertEqual("owner", claude["role"])
        self.assertEqual("Codex tab", codex["title"])
        self.assertEqual("owner", codex["role"])
        self.assertEqual("Persist durable metadata", reopened["title"])
        self.assertEqual(plan_path, reopened["plan_path"])
        status = WIS.cmd_status("example-feature/example-item", cwd=self.repo)
        self.assertEqual("Persist durable metadata", status["title"])
        self.assertEqual(plan_path, status["plan_path"])

    def test_item_supports_quick_chores_and_planning_without_an_owner(self):
        plan_path = self._write_plan("LEGACY-GOAL.md")
        WIS.cmd_item("quick-chore/rename", "proposed", title="Rename a small file", cwd=self.repo)
        WIS.cmd_item(
            "planned-work/prepare", "proposed", title="Prepare planned work", plan_path=plan_path, cwd=self.repo,
        )
        quick = WIS.cmd_status("quick-chore/rename", cwd=self.repo)
        planned = WIS.cmd_status("planned-work/prepare", cwd=self.repo)
        self.assertEqual("Rename a small file", quick["title"])
        self.assertIsNone(quick["plan_path"])
        self.assertEqual([], quick["sessions"])
        self.assertEqual(plan_path, planned["plan_path"])
        self.assertEqual([], planned["sessions"])

    def test_invalid_plan_paths_are_rejected(self):
        cases = ("../README.md", "/README.md", "C:/README.md", "README.md\\child", "missing/GOAL.md")
        for value in cases:
            with self.subTest(value=value), self.assertRaises(ValueError):
                WIS.cmd_item("invalid-plan/path", "proposed", title="Invalid plan", plan_path=value, cwd=self.repo)
        outside = self.root / "outside-plan.md"
        outside.write_text("# Outside\n", encoding="utf-8")
        escaped = self.repo / "escaped-plan.md"
        try:
            escaped.symlink_to(outside)
        except OSError:
            return
        with self.assertRaises(ValueError):
            WIS.cmd_item("invalid-plan/symlink", "proposed", title="Invalid plan", plan_path=escaped.name, cwd=self.repo)

    def test_omitted_claim_status_preserves_an_existing_item_status(self):
        self._claim_claude(status="paused", title="Paused task")
        self._claim_claude(status=None, title=None)
        result = WIS.cmd_status("example-feature/example-item", cwd=self.repo)
        self.assertEqual("paused", result["status"])
        self.assertEqual("Paused task", result["title"])

    def test_claim_refuses_an_ended_stored_identity(self):
        self._claim_claude()
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "a2bcd5c4-bf6d-4087-95e3-d7ba7f711875"}, clear=False):
            WIS.cmd_end("claude", "closed")
            with self.assertRaisesRegex(ValueError, "not a current native session"):
                WIS.cmd_claim("example-feature/example-item", "claude", title="Changed task", cwd=self.repo)
        result = WIS.cmd_status("example-feature/example-item", cwd=self.repo)
        self.assertEqual("active", result["status"])
        self.assertEqual("Example task", result["title"])

    def test_fallback_registration_claims_atomically_with_current_git_context(self):
        self._run_git(self.repo, "branch", "-M", "main")
        plan_path = self._write_plan()
        entry = {
            "host": "claude",
            "session_id": "fallback-claude",
            "pid": os.getpid(),
            "pid_started_at": time.time(),
            "title": "Native Claude tab",
        }
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": ""}, clear=False), \
                mock.patch.object(WIS, "_session_close", return_value=types.SimpleNamespace(
                    own_host_and_entry=mock.Mock(return_value=entry),
                )):
            WIS.cmd_claim(
                "fallback-feature/fallback-item", "claude", status="proposed", title="Fallback task",
                plan_path=plan_path, cwd=self.repo,
            )
        connection = self._connection()
        try:
            item = STORE.get_work_item(connection, "github.com/tj-agents/core:fallback-feature/fallback-item")
            session = STORE.get_session(connection, "claude:fallback-claude")
        finally:
            connection.close()
        self.assertEqual("proposed", item["status"])
        self.assertEqual("Fallback task", item["title"])
        self.assertEqual(plan_path, item["plan_path"])
        self.assertEqual("owner", session["role"])
        self.assertEqual("github.com/tj-agents/core", session["repo"])
        self.assertEqual(self._run_git(self.repo, "rev-parse", "HEAD"), session["head"])
        self.assertEqual("main", session["branch"])
        self.assertEqual("Native Claude tab", session["title"])
        self.assertIsNone(item["branch"])

    def test_refused_fallback_claim_rolls_back_registration_and_metadata(self):
        plan_path = self._write_plan()
        self._claim_claude(title="Original task", plan_path=plan_path)
        entry = {
            "host": "codex",
            "session_id": "fallback-codex",
            "pid": os.getpid(),
            "pid_started_at": time.time(),
            "title": "Native Codex tab",
        }
        with mock.patch.dict(os.environ, {"CODEX_THREAD_ID": ""}, clear=False), \
                mock.patch.object(WIS, "_session_close", return_value=types.SimpleNamespace(
                    own_host_and_entry=mock.Mock(return_value=entry),
                )):
            with self.assertRaises(ValueError):
                WIS.cmd_claim(
                    "example-feature/example-item", "codex", status="paused", title="Replacement task",
                    cwd=self.repo,
                )
        connection = self._connection()
        try:
            item = STORE.get_work_item(connection, "github.com/tj-agents/core:example-feature/example-item")
            rejected = STORE.get_session(connection, "codex:fallback-codex")
        finally:
            connection.close()
        self.assertEqual("active", item["status"])
        self.assertEqual("Original task", item["title"])
        self.assertEqual(plan_path, item["plan_path"])
        self.assertIsNone(rejected)

    def test_refused_claim_does_not_create_a_placeholder_or_update_metadata(self):
        plan_path = self._write_plan()
        self._claim_claude(title="Original task", plan_path=plan_path)
        codex_id = "01a12aa7-5b58-7312-9400-ef710762147f"
        self._register_current_session("codex", codex_id, "Codex tab")
        with mock.patch.dict(os.environ, {"CODEX_THREAD_ID": codex_id}, clear=False):
            with self.assertRaises(ValueError):
                WIS.cmd_claim(
                    "example-feature/example-item", "codex", role="owner", status="paused",
                    title="Replacement task", cwd=self.repo,
                )
        connection = self._connection()
        try:
            item = STORE.get_work_item(connection, "github.com/tj-agents/core:example-feature/example-item")
            owner = STORE.get_session(connection, "claude:a2bcd5c4-bf6d-4087-95e3-d7ba7f711875")
            rejected = STORE.get_session(connection, f"codex:{codex_id}")
        finally:
            connection.close()
        self.assertEqual("active", item["status"])
        self.assertEqual("Original task", item["title"])
        self.assertEqual(plan_path, item["plan_path"])
        self.assertEqual("owner", owner["role"])
        self.assertIsNone(rejected["work_item"])
        self.assertIsNone(rejected["role"])

    def test_claim_refuses_unknown_current_native_identity(self):
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "unknown"}, clear=False), \
                mock.patch.object(WIS, "_session_close", side_effect=RuntimeError("no attachment")):
            with self.assertRaisesRegex(ValueError, "could not verify"):
                WIS.cmd_claim("unknown-session/item", "claude", title="Unknown session", cwd=self.repo)

    def test_claim_does_not_treat_a_metadata_poor_session_as_registered_identity(self):
        session_id = "a2bcd5c4-bf6d-4087-95e3-d7ba7f711875"
        connection = self._connection()
        try:
            STORE.start_session(connection, "claude", session_id, self.repo)
        finally:
            connection.close()
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": session_id}, clear=False), \
                mock.patch.object(WIS, "_session_close", side_effect=RuntimeError("no attachment")):
            with self.assertRaisesRegex(ValueError, "could not verify"):
                WIS.cmd_claim("metadata-poor/item", "claude", title="Should not claim", cwd=self.repo)
        connection = self._connection()
        try:
            session = STORE.get_session(connection, f"claude:{session_id}")
            self.assertIsNone(STORE.get_work_item(connection, "github.com/tj-agents/core:metadata-poor/item"))
        finally:
            connection.close()
        self.assertIsNone(session["work_item"])
        self.assertIsNone(session["role"])


    def test_second_owner_without_handoff_is_refused(self):
        self._claim_claude()
        with self.assertRaises(ValueError):
            self._claim_codex(role="owner")

    def test_predecessor_handoff_marks_handed_off_and_successor(self):
        self._claim_claude()
        result = self._claim_codex(role="owner", predecessor="claude:a2bcd5c4-bf6d-4087-95e3-d7ba7f711875")
        self.assertEqual("claude:a2bcd5c4-bf6d-4087-95e3-d7ba7f711875", result["removed"])
        connection = self._connection()
        try:
            predecessor = STORE.get_session(connection, "claude:a2bcd5c4-bf6d-4087-95e3-d7ba7f711875")
            successor = STORE.get_session(connection, "codex:01a12aa7-5b58-7312-9400-ef710762147f")
        finally:
            connection.close()
        self.assertEqual("handed_off", predecessor["end_reason"])
        self.assertEqual("codex:01a12aa7-5b58-7312-9400-ef710762147f", predecessor["successor"])
        self.assertEqual("owner", successor["role"])
        self.assertEqual("claude:a2bcd5c4-bf6d-4087-95e3-d7ba7f711875", successor["predecessor"])

    def test_bounded_role_does_not_conflict_with_owner(self):
        self._claim_claude()
        self._claim_codex(role="bounded: docs")
        connection = self._connection()
        try:
            rows = STORE.sessions_for_work_item(connection, "github.com/tj-agents/core:example-feature/example-item")
        finally:
            connection.close()
        self.assertEqual(2, len(rows))

    def test_status_reports_live_closed_unknown_foreign_and_stale(self):
        self._claim_claude()
        recovery = fake_recovery(liveness_result="live")
        with mock.patch.object(WIS, "_agent_recovery", return_value=recovery):
            result = WIS.cmd_status("example-feature/example-item", cwd=self.repo)
        self.assertEqual(1, len(result["sessions"]))
        self.assertEqual("live", result["sessions"][0]["status"])

        for liveness_value in ("closed", "unknown"):
            recovery = fake_recovery(liveness_result=liveness_value)
            with mock.patch.object(WIS, "_agent_recovery", return_value=recovery):
                result = WIS.cmd_status("example-feature/example-item", cwd=self.repo)
            self.assertEqual(liveness_value, result["sessions"][0]["status"])

        connection = self._connection()
        try:
            STORE.start_session(connection, "claude", "a2bcd5c4-bf6d-4087-95e3-d7ba7f711875", self.repo, pid=1, pid_started_at=1.0)
            connection.execute(
                "UPDATE sessions SET machine = 'some-other-host' WHERE handle = ?",
                ("claude:a2bcd5c4-bf6d-4087-95e3-d7ba7f711875",),
            )
            connection.commit()
        finally:
            connection.close()
        result = WIS.cmd_status("example-feature/example-item", cwd=self.repo)
        self.assertEqual("foreign", result["sessions"][0]["status"])

    def test_status_reports_live_for_a_live_session_even_on_branch_mismatch(self):
        self._claim_claude()
        self._run_git(self.repo, "checkout", "-b", "other-branch")
        recovery = fake_recovery(liveness_result="live")
        with mock.patch.object(WIS, "_agent_recovery", return_value=recovery):
            result = WIS.cmd_status("example-feature/example-item", cwd=self.repo)
        self.assertEqual("live", result["sessions"][0]["status"])

    def test_resume_selects_single_owner_and_calls_open_session(self):
        self._claim_claude()
        recovery = fake_recovery(liveness_result="closed")
        with mock.patch.object(WIS, "_agent_recovery", return_value=recovery):
            result = WIS.cmd_resume("example-feature/example-item", cwd=self.repo)
        self.assertEqual({"ok": True}, result)
        recovery.open_session.assert_called_once()
        args, kwargs = recovery.open_session.call_args
        self.assertEqual(("claude", "a2bcd5c4-bf6d-4087-95e3-d7ba7f711875"), args)
        self.assertIn("roots", kwargs)
        self.assertEqual({"claude"}, set(kwargs["roots"]))
        self.assertTrue(str(kwargs["roots"]["claude"]).endswith("projects"))

    def test_resume_refuses_multiple_owners_without_a_handle(self):
        self._claim_claude(role="bounded: a")
        self._claim_codex(role="bounded: b")
        recovery = fake_recovery(liveness_result="closed")
        with mock.patch.object(WIS, "_agent_recovery", return_value=recovery):
            with self.assertRaises(ValueError):
                WIS.cmd_resume("example-feature/example-item", cwd=self.repo)
        recovery.open_session.assert_not_called()

    def test_resume_refuses_foreign(self):
        self._claim_claude()
        connection = self._connection()
        try:
            connection.execute(
                "UPDATE sessions SET machine = 'some-other-host' WHERE handle = ?",
                ("claude:a2bcd5c4-bf6d-4087-95e3-d7ba7f711875",),
            )
            connection.commit()
        finally:
            connection.close()
        recovery = fake_recovery(liveness_result="closed")
        with mock.patch.object(WIS, "_agent_recovery", return_value=recovery):
            with self.assertRaises(ValueError):
                WIS.cmd_resume("example-feature/example-item", cwd=self.repo)
        recovery.open_session.assert_not_called()

    def test_resume_refuses_live_even_with_a_branch_mismatch(self):
        self._claim_claude()
        self._run_git(self.repo, "checkout", "-b", "other-branch")
        recovery = fake_recovery(liveness_result="live")
        with mock.patch.object(WIS, "_agent_recovery", return_value=recovery):
            with self.assertRaises(ValueError):
                WIS.cmd_resume("example-feature/example-item", cwd=self.repo)
        recovery.open_session.assert_not_called()

    def test_resume_refuses_live(self):
        self._claim_claude()
        recovery = fake_recovery(liveness_result="live")
        with mock.patch.object(WIS, "_agent_recovery", return_value=recovery):
            with self.assertRaises(ValueError):
                WIS.cmd_resume("example-feature/example-item", cwd=self.repo)
        recovery.open_session.assert_not_called()

    def test_find_matches_worktree_to_its_claimed_work_item(self):
        self._claim_claude()
        result = WIS.cmd_find(str(self.repo))
        self.assertEqual(1, len(result["matches"]))
        self.assertEqual("example-feature/example-item", result["matches"][0]["work_item"])
        self.assertEqual("claude:a2bcd5c4-bf6d-4087-95e3-d7ba7f711875", result["matches"][0]["handle"])

    def test_find_reports_no_matches_for_an_unclaimed_worktree(self):
        other = self.root / "unclaimed"
        other.mkdir()
        result = WIS.cmd_find(str(other))
        self.assertEqual([], result["matches"])

    def test_interrupted_lists_only_dead_pid_not_ended_rows_grouped_by_work_item(self):
        self._claim_claude()
        connection = self._connection()
        try:
            STORE.start_session(
                connection, "claude", "a2bcd5c4-bf6d-4087-95e3-d7ba7f711875", self.repo,
                pid=999999999, pid_started_at=1234.0,
            )
            STORE.set_session_ownership(
                connection, "claude:a2bcd5c4-bf6d-4087-95e3-d7ba7f711875", "owner",
                "github.com/tj-agents/core:example-feature/example-item",
            )
        finally:
            connection.close()
        register_session = WIS._register_session()
        with mock.patch.object(register_session, "build_process_lookup", return_value=lambda pid: None):
            result = WIS.cmd_interrupted(cwd=self.repo)
        self.assertIn("example-feature/example-item", result["interrupted"])
        handles = {row["handle"] for row in result["interrupted"]["example-feature/example-item"]}
        self.assertEqual({"claude:a2bcd5c4-bf6d-4087-95e3-d7ba7f711875"}, handles)

    def test_interrupted_excludes_live_pids(self):
        self._claim_claude()
        connection = self._connection()
        try:
            STORE.start_session(
                connection, "claude", "a2bcd5c4-bf6d-4087-95e3-d7ba7f711875", self.repo,
                pid=os.getpid(), pid_started_at=1234.0,
            )
        finally:
            connection.close()
        register_session = WIS._register_session()
        process = types.SimpleNamespace(started_at=1234.0)
        with mock.patch.object(register_session, "build_process_lookup", return_value=lambda pid: process):
            result = WIS.cmd_interrupted(cwd=self.repo)
        self.assertEqual({}, result["interrupted"])

    def test_interrupted_resumes_only_listed_handles(self):
        self._claim_claude()
        connection = self._connection()
        try:
            STORE.start_session(
                connection, "claude", "a2bcd5c4-bf6d-4087-95e3-d7ba7f711875", self.repo,
                pid=999999999, pid_started_at=1234.0,
            )
        finally:
            connection.close()
        register_session = WIS._register_session()
        recovery = fake_recovery()
        with mock.patch.object(register_session, "build_process_lookup", return_value=lambda pid: None), \
                mock.patch.object(WIS, "_agent_recovery", return_value=recovery):
            result = WIS.cmd_interrupted(
                resume_handles=["claude:a2bcd5c4-bf6d-4087-95e3-d7ba7f711875"], cwd=self.repo,
            )
        recovery.open_session.assert_called_once()
        self.assertEqual([{"ok": True}], result["resumed"])

    def test_interrupted_resume_rejects_an_unlisted_handle(self):
        self._claim_claude()
        register_session = WIS._register_session()
        with mock.patch.object(register_session, "build_process_lookup", return_value=lambda pid: None):
            with self.assertRaises(ValueError):
                WIS.cmd_interrupted(resume_handles=["claude:not-interrupted"], cwd=self.repo)

    def test_end_records_reason_for_current_session(self):
        self._claim_claude()
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "a2bcd5c4-bf6d-4087-95e3-d7ba7f711875"}, clear=False):
            WIS.cmd_end("claude", "finished")
        connection = self._connection()
        try:
            row = STORE.get_session(connection, "claude:a2bcd5c4-bf6d-4087-95e3-d7ba7f711875")
        finally:
            connection.close()
        self.assertEqual("finished", row["end_reason"])
        self.assertIsNotNone(row["ended_at"])

    def test_item_updates_status_and_pr(self):
        self._claim_claude()
        WIS.cmd_item("example-feature/example-item", "done", pr="https://github.com/tj-agents/core/pull/1", cwd=self.repo)
        connection = self._connection()
        try:
            item = STORE.get_work_item(connection, "github.com/tj-agents/core:example-feature/example-item")
        finally:
            connection.close()
        self.assertEqual("done", item["status"])
        self.assertEqual("https://github.com/tj-agents/core/pull/1", item["pr"])

    def test_done_item_deletes_ended_sessions_and_keeps_live_foreign_and_caller_ones(self):
        live = "a2bcd5c4-bf6d-4087-95e3-d7ba7f711875"
        ended = "b3cde6d5-c07e-4198-a6f4-e8cb8a822986"
        foreign = "c4def7e6-d18f-42a9-b7a5-f9dc9b933a97"
        caller = "01a12aa7-5b58-7312-9400-ef710762147f"
        self._claim_claude(session_id=live)
        self._claim_claude(session_id=ended, role="bounded: notes")
        self._claim_claude(session_id=foreign, role="bounded: elsewhere")
        self._claim_codex(session_id=caller, role="bounded: review")
        started = WIS._register_session().build_process_lookup()(os.getpid()).started_at
        connection = self._connection()
        try:
            connection.execute("UPDATE sessions SET pid_started_at = ? WHERE handle = ?", (started, f"claude:{live}"))
            connection.execute("UPDATE sessions SET machine = 'another-host' WHERE handle = ?", (f"claude:{foreign}",))
            connection.commit()
            STORE.end_session(connection, f"claude:{ended}", "closed")
        finally:
            connection.close()
        with mock.patch.dict(os.environ, {"CODEX_THREAD_ID": caller, "CLAUDE_CODE_SESSION_ID": ""}):
            WIS.cmd_item("example-feature/example-item", "done", cwd=self.repo)
        connection = self._connection()
        try:
            remaining = sorted(row["handle"] for row in connection.execute("SELECT handle FROM sessions"))
        finally:
            connection.close()
        self.assertEqual(sorted([f"claude:{live}", f"claude:{foreign}", f"codex:{caller}"]), remaining)

    def test_done_item_judges_a_running_session_by_its_cli_registry_record(self):
        alive = "a2bcd5c4-bf6d-4087-95e3-d7ba7f711875"
        gone = "b3cde6d5-c07e-4198-a6f4-e8cb8a822986"
        self._claim_claude(session_id=alive)
        self._claim_claude(session_id=gone, role="bounded: notes")
        started = WIS._register_session().build_process_lookup()(os.getpid()).started_at
        registry = self.state / "cli-sessions"
        registry.mkdir(parents=True, exist_ok=True)
        for session_id, pid_started_at in ((alive, started), (gone, started + 1000)):
            (registry / f"{session_id}.json").write_text(json.dumps({
                "host": "claude", "session_id": session_id, "pid": os.getpid(), "pid_started_at": pid_started_at,
            }), encoding="utf-8")
        connection = self._connection()
        try:
            connection.execute("UPDATE sessions SET pid = NULL, pid_started_at = NULL")
            connection.commit()
        finally:
            connection.close()
        with mock.patch.dict(os.environ, {"CODEX_THREAD_ID": "", "CLAUDE_CODE_SESSION_ID": ""}):
            WIS.cmd_item("example-feature/example-item", "done", cwd=self.repo)
        connection = self._connection()
        try:
            remaining = [row["handle"] for row in connection.execute("SELECT handle FROM sessions")]
        finally:
            connection.close()
        self.assertEqual([f"claude:{alive}"], remaining)

    def test_done_item_deletes_an_ended_session_recorded_on_another_machine(self):
        other = "c4def7e6-d18f-42a9-b7a5-f9dc9b933a97"
        self._claim_claude(session_id=other, role="bounded: elsewhere")
        connection = self._connection()
        try:
            connection.execute("UPDATE sessions SET machine = 'another-host' WHERE handle = ?", (f"claude:{other}",))
            connection.commit()
            STORE.end_session(connection, f"claude:{other}", "closed")
        finally:
            connection.close()
        with mock.patch.dict(os.environ, {"CODEX_THREAD_ID": "", "CLAUDE_CODE_SESSION_ID": ""}):
            WIS.cmd_item("example-feature/example-item", "done", cwd=self.repo)
        connection = self._connection()
        try:
            remaining = [row["handle"] for row in connection.execute("SELECT handle FROM sessions")]
        finally:
            connection.close()
        self.assertEqual([], remaining)

    def test_invalid_work_item_grammar_is_rejected(self):
        with self.assertRaises(ValueError):
            WIS._parse_work_item("Not-Valid/Item")
        with self.assertRaises(ValueError):
            WIS._parse_work_item("missing-slash")

    def test_status_reports_foreign_before_checking_an_ended_row(self):
        self._claim_claude()
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "a2bcd5c4-bf6d-4087-95e3-d7ba7f711875"}, clear=False):
            WIS.cmd_end("claude", "finished")
        connection = self._connection()
        try:
            connection.execute(
                "UPDATE sessions SET machine = 'some-other-host' WHERE handle = ?",
                ("claude:a2bcd5c4-bf6d-4087-95e3-d7ba7f711875",),
            )
            connection.commit()
        finally:
            connection.close()
        result = WIS.cmd_status("example-feature/example-item", cwd=self.repo)
        self.assertEqual("foreign", result["sessions"][0]["status"])

    def test_status_and_resume_report_a_dead_not_ended_session_with_a_stale_worktree_as_stale(self):
        self._claim_claude()
        self._run_git(self.repo, "checkout", "-b", "other-branch")
        recovery = fake_recovery(liveness_result="closed")
        with mock.patch.object(WIS, "_agent_recovery", return_value=recovery):
            result = WIS.cmd_status("example-feature/example-item", cwd=self.repo)
        self.assertEqual("stale", result["sessions"][0]["status"])
        with mock.patch.object(WIS, "_agent_recovery", return_value=recovery):
            with self.assertRaises(ValueError):
                WIS.cmd_resume("example-feature/example-item", cwd=self.repo)
        recovery.open_session.assert_not_called()

    def test_resume_selects_an_owner_ended_closed_without_a_handle(self):
        self._claim_claude()
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "a2bcd5c4-bf6d-4087-95e3-d7ba7f711875"}, clear=False):
            WIS.cmd_end("claude", "closed")
        recovery = fake_recovery(liveness_result="closed")
        with mock.patch.object(WIS, "_agent_recovery", return_value=recovery):
            result = WIS.cmd_resume("example-feature/example-item", cwd=self.repo)
        self.assertEqual({"ok": True}, result)
        recovery.open_session.assert_called_once()

    def test_resume_does_not_select_a_finished_owner_without_a_handle(self):
        self._claim_claude()
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "a2bcd5c4-bf6d-4087-95e3-d7ba7f711875"}, clear=False):
            WIS.cmd_end("claude", "finished")
        recovery = fake_recovery(liveness_result="closed")
        with mock.patch.object(WIS, "_agent_recovery", return_value=recovery):
            with self.assertRaises(ValueError):
                WIS.cmd_resume("example-feature/example-item", cwd=self.repo)
        recovery.open_session.assert_not_called()

    def test_resume_refuses_a_handed_off_predecessor_handle_while_its_successor_owns_the_item(self):
        self._claim_claude()
        self._claim_codex(role="owner", predecessor="claude:a2bcd5c4-bf6d-4087-95e3-d7ba7f711875")
        recovery = fake_recovery(liveness_result="closed")
        with mock.patch.object(WIS, "_agent_recovery", return_value=recovery):
            with self.assertRaises(ValueError):
                WIS.cmd_resume(
                    "example-feature/example-item",
                    handle="claude:a2bcd5c4-bf6d-4087-95e3-d7ba7f711875",
                    cwd=self.repo,
                )
        recovery.open_session.assert_not_called()

    def test_resume_refuses_a_finished_session_handle(self):
        self._claim_claude()
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "a2bcd5c4-bf6d-4087-95e3-d7ba7f711875"}, clear=False):
            WIS.cmd_end("claude", "finished")
        recovery = fake_recovery(liveness_result="closed")
        with mock.patch.object(WIS, "_agent_recovery", return_value=recovery):
            with self.assertRaises(ValueError):
                WIS.cmd_resume(
                    "example-feature/example-item",
                    handle="claude:a2bcd5c4-bf6d-4087-95e3-d7ba7f711875",
                    cwd=self.repo,
                )
        recovery.open_session.assert_not_called()

    def test_concurrent_claims_wrap_the_live_owner_check_and_write_in_one_transaction(self):
        self._register_current_session("claude", "a2bcd5c4-bf6d-4087-95e3-d7ba7f711875", "Claude tab")
        self._register_current_session("codex", "01a12aa7-5b58-7312-9400-ef710762147f", "Codex tab")
        original_live_owners = STORE.live_owners

        def slow_live_owners(connection, key):
            result = original_live_owners(connection, key)
            time.sleep(0.3)
            return result

        results = {}
        errors = {}

        def claim(name, env_key, env_value, host):
            os.environ[env_key] = env_value
            try:
                results[name] = WIS.cmd_claim(
                    "example-feature/example-item", host, role="owner", title="Concurrent task", cwd=self.repo,
                )
            except ValueError as error:
                errors[name] = error
            finally:
                os.environ.pop(env_key, None)

        with mock.patch.object(STORE, "live_owners", side_effect=slow_live_owners):
            threads = [
                threading.Thread(
                    target=claim,
                    args=("a", "CLAUDE_CODE_SESSION_ID", "a2bcd5c4-bf6d-4087-95e3-d7ba7f711875", "claude"),
                ),
                threading.Thread(
                    target=claim,
                    args=("b", "CODEX_THREAD_ID", "01a12aa7-5b58-7312-9400-ef710762147f", "codex"),
                ),
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=10)

        self.assertEqual(1, len(results))
        self.assertEqual(1, len(errors))
        connection = self._connection()
        try:
            owners = STORE.live_owners(connection, "github.com/tj-agents/core:example-feature/example-item")
        finally:
            connection.close()
        self.assertEqual(1, len(owners))

    def test_invalid_role_is_rejected_before_writing_anything(self):
        for invalid in ("Owner", "owner ", "bounded:", "bounded: ", "random"):
            with self.subTest(role=invalid):
                with self.assertRaises(ValueError):
                    self._claim_claude(role=invalid)
        connection = self._connection()
        try:
            item = STORE.get_work_item(connection, "github.com/tj-agents/core:example-feature/example-item")
        finally:
            connection.close()
        self.assertIsNone(item)

    def test_valid_bounded_role_with_a_scope_is_accepted(self):
        self._claim_claude(role="bounded: docs")
        connection = self._connection()
        try:
            row = STORE.get_session(connection, "claude:a2bcd5c4-bf6d-4087-95e3-d7ba7f711875")
        finally:
            connection.close()
        self.assertEqual("bounded: docs", row["role"])

    def test_claim_on_the_default_branch_does_not_record_a_branch(self):
        self._run_git(self.repo, "branch", "-M", "main")
        self._claim_claude()
        connection = self._connection()
        try:
            item = STORE.get_work_item(connection, "github.com/tj-agents/core:example-feature/example-item")
        finally:
            connection.close()
        self.assertIsNone(item["branch"])

    def test_existing_branch_survives_status_and_owner_claims_from_another_branch(self):
        self._run_git(self.repo, "checkout", "-b", "owner-branch")
        self._claim_claude()
        self._run_git(self.repo, "checkout", "-b", "other-branch")
        WIS.cmd_item("example-feature/example-item", "paused", cwd=self.repo)
        self._claim_codex(
            role="owner", predecessor="claude:a2bcd5c4-bf6d-4087-95e3-d7ba7f711875",
        )
        connection = self._connection()
        try:
            item = STORE.get_work_item(connection, "github.com/tj-agents/core:example-feature/example-item")
        finally:
            connection.close()
        self.assertEqual("owner-branch", item["branch"])

    def test_claim_on_a_non_default_branch_records_the_branch(self):
        self._run_git(self.repo, "checkout", "-b", "feature-branch")
        self._claim_claude()
        connection = self._connection()
        try:
            item = STORE.get_work_item(connection, "github.com/tj-agents/core:example-feature/example-item")
        finally:
            connection.close()
        self.assertEqual("feature-branch", item["branch"])

    def test_default_branch_prefers_symbolic_ref_over_the_literal_fallback(self):
        with mock.patch.object(STORE, "_git", return_value="origin/trunk"):
            self.assertEqual("trunk", WIS._default_branch(self.repo, "not-trunk"))

    def test_default_branch_falls_back_to_literal_main_or_master(self):
        with mock.patch.object(STORE, "_git", return_value=None):
            self.assertEqual("main", WIS._default_branch(self.repo, "main"))
            self.assertIsNone(WIS._default_branch(self.repo, "feature"))

    def test_close_reclaim_close_then_resume_without_handle_selects_the_latest_owner(self):
        self._claim_claude()
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "a2bcd5c4-bf6d-4087-95e3-d7ba7f711875"}, clear=False):
            WIS.cmd_end("claude", "closed")
        self._claim_codex(role="owner")
        with mock.patch.dict(os.environ, {"CODEX_THREAD_ID": "01a12aa7-5b58-7312-9400-ef710762147f"}, clear=False):
            WIS.cmd_end("codex", "closed")
        connection = self._connection()
        try:
            former_owner = STORE.get_session(connection, "claude:a2bcd5c4-bf6d-4087-95e3-d7ba7f711875")
        finally:
            connection.close()
        self.assertIsNone(former_owner["role"])

        recovery = fake_recovery(liveness_result="closed")
        with mock.patch.object(WIS, "_agent_recovery", return_value=recovery):
            result = WIS.cmd_resume("example-feature/example-item", cwd=self.repo)
        self.assertEqual({"ok": True}, result)
        recovery.open_session.assert_called_once()
        args, _kwargs = recovery.open_session.call_args
        self.assertEqual(("codex", "01a12aa7-5b58-7312-9400-ef710762147f"), args)

    def test_interrupted_listing_includes_each_rows_status(self):
        self._claim_claude()
        connection = self._connection()
        try:
            STORE.start_session(
                connection, "claude", "a2bcd5c4-bf6d-4087-95e3-d7ba7f711875", self.repo,
                pid=999999999, pid_started_at=1234.0,
            )
        finally:
            connection.close()
        register_session = WIS._register_session()
        recovery = fake_recovery(liveness_result="closed")
        with mock.patch.object(register_session, "build_process_lookup", return_value=lambda pid: None), \
                mock.patch.object(WIS, "_agent_recovery", return_value=recovery):
            result = WIS.cmd_interrupted(cwd=self.repo)
        rows = result["interrupted"]["example-feature/example-item"]
        self.assertEqual(1, len(rows))
        self.assertEqual("closed", rows[0]["status"])

    def test_interrupted_resume_refuses_a_handle_that_is_not_closed(self):
        self._claim_claude()
        connection = self._connection()
        try:
            STORE.start_session(
                connection, "claude", "a2bcd5c4-bf6d-4087-95e3-d7ba7f711875", self.repo,
                pid=999999999, pid_started_at=1234.0,
            )
        finally:
            connection.close()
        self._run_git(self.repo, "checkout", "-b", "other-branch")
        register_session = WIS._register_session()
        recovery = fake_recovery(liveness_result="closed")
        with mock.patch.object(register_session, "build_process_lookup", return_value=lambda pid: None), \
                mock.patch.object(WIS, "_agent_recovery", return_value=recovery):
            with self.assertRaises(ValueError):
                WIS.cmd_interrupted(resume_handles=["claude:a2bcd5c4-bf6d-4087-95e3-d7ba7f711875"], cwd=self.repo)
            recovery.open_session.assert_not_called()

    def test_item_on_the_default_branch_does_not_record_a_branch(self):
        self._run_git(self.repo, "branch", "-M", "main")
        WIS.cmd_item("example-feature/example-item", "active", title="Example task", cwd=self.repo)
        connection = self._connection()
        try:
            item = STORE.get_work_item(connection, "github.com/tj-agents/core:example-feature/example-item")
        finally:
            connection.close()
        self.assertIsNone(item["branch"])

    def test_claim_fills_a_null_branch_when_claimed_again_from_a_feature_branch(self):
        self._run_git(self.repo, "branch", "-M", "main")
        self._claim_claude()
        connection = self._connection()
        try:
            item = STORE.get_work_item(connection, "github.com/tj-agents/core:example-feature/example-item")
        finally:
            connection.close()
        self.assertIsNone(item["branch"])

        self._run_git(self.repo, "checkout", "-b", "feature-branch")
        self._claim_claude()
        connection = self._connection()
        try:
            item = STORE.get_work_item(connection, "github.com/tj-agents/core:example-feature/example-item")
        finally:
            connection.close()
        self.assertEqual("feature-branch", item["branch"])

    def test_claim_bounded_role_does_not_fill_a_null_branch(self):
        self._run_git(self.repo, "branch", "-M", "main")
        self._claim_claude()
        self._run_git(self.repo, "checkout", "-b", "feature-branch")
        self._claim_codex(role="bounded: docs")
        connection = self._connection()
        try:
            item = STORE.get_work_item(connection, "github.com/tj-agents/core:example-feature/example-item")
        finally:
            connection.close()
        self.assertIsNone(item["branch"])

    def test_ambiguous_tag_and_branch_name_does_not_make_the_row_stale(self):
        self._run_git(self.repo, "checkout", "-b", "shared-name")
        self._run_git(self.repo, "tag", "shared-name")
        self._claim_claude()
        recovery = fake_recovery(liveness_result="closed")
        with mock.patch.object(WIS, "_agent_recovery", return_value=recovery):
            result = WIS.cmd_status("example-feature/example-item", cwd=self.repo)
        self.assertEqual("closed", result["sessions"][0]["status"])

    def test_item_status_fills_a_null_branch_from_a_non_default_branch(self):
        self._run_git(self.repo, "branch", "-M", "main")
        WIS.cmd_item("example-feature/example-item", "active", title="Example task", cwd=self.repo)
        connection = self._connection()
        try:
            item = STORE.get_work_item(connection, "github.com/tj-agents/core:example-feature/example-item")
        finally:
            connection.close()
        self.assertIsNone(item["branch"])

        self._run_git(self.repo, "checkout", "-b", "feature-branch")
        WIS.cmd_item("example-feature/example-item", "active", cwd=self.repo)
        connection = self._connection()
        try:
            item = STORE.get_work_item(connection, "github.com/tj-agents/core:example-feature/example-item")
        finally:
            connection.close()
        self.assertEqual("feature-branch", item["branch"])

    def test_interrupted_computes_each_rows_status_once_even_when_resuming(self):
        self._claim_claude()
        connection = self._connection()
        try:
            STORE.start_session(
                connection, "claude", "a2bcd5c4-bf6d-4087-95e3-d7ba7f711875", self.repo,
                pid=999999999, pid_started_at=1234.0,
            )
        finally:
            connection.close()
        register_session = WIS._register_session()
        recovery = fake_recovery(liveness_result="closed")
        original_status = WIS._read_time_status
        calls = []

        def counting_status(row, machine):
            calls.append(row["handle"])
            return original_status(row, machine)

        with mock.patch.object(register_session, "build_process_lookup", return_value=lambda pid: None), \
                mock.patch.object(WIS, "_agent_recovery", return_value=recovery), \
                mock.patch.object(WIS, "_read_time_status", side_effect=counting_status):
            WIS.cmd_interrupted(
                resume_handles=["claude:a2bcd5c4-bf6d-4087-95e3-d7ba7f711875"], cwd=self.repo,
            )
        self.assertEqual(1, calls.count("claude:a2bcd5c4-bf6d-4087-95e3-d7ba7f711875"))

    def test_claim_resolves_repo_branch_and_head_once(self):
        original = STORE._collect_repo_branch_head
        calls = []

        def counting(cwd, timeout=STORE.HOOK_GIT_TIMEOUT):
            calls.append(cwd)
            return original(cwd, timeout=timeout)

        with mock.patch.object(STORE, "_collect_repo_branch_head", side_effect=counting):
            self._claim_claude()
        self.assertEqual(2, len(calls))


if __name__ == "__main__":
    unittest.main()
