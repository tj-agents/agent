import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "session_store", ROOT / ".agents/agent-store/runtime/session_store.py"
)
STORE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(STORE)

GIT = shutil.which("git")


def _git_environment():
    environment = os.environ.copy()
    for name in tuple(environment):
        if name == "GIT_CONFIG_COUNT" or name.startswith(("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_")):
            environment.pop(name)
    return environment


def _run_git(cwd, *args):
    result = subprocess.run(
        ["git", "-C", str(cwd), *args], capture_output=True, text=True, timeout=15,
        env=_git_environment(),
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr)
    return result.stdout.strip()


def _init_repo(path, origin=None):
    path.mkdir(parents=True, exist_ok=True)
    _run_git(path, "init")
    _run_git(path, "config", "user.email", "test@example.com")
    _run_git(path, "config", "user.name", "Test User")
    (path / "README.md").write_text("hello\n", encoding="utf-8")
    _run_git(path, "add", "README.md")
    _run_git(path, "commit", "-m", "initial")
    if origin:
        _run_git(path, "remote", "add", "origin", origin)
    return path


class SchemaTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="session store é ")
        self.addCleanup(self.temp.cleanup)
        self.db_path = Path(self.temp.name) / "state" / "sessions.db"

    def test_schema_created_with_expected_tables_and_user_version(self):
        connection = STORE.connect(self.db_path)
        self.addCleanup(connection.close)
        tables = {
            row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        self.assertEqual({"sessions", "work_items", "work_item_references"}, tables)
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        self.assertEqual(4, version)

    def test_reconnecting_does_not_lose_the_schema_version(self):
        STORE.connect(self.db_path).close()
        connection = STORE.connect(self.db_path)
        self.addCleanup(connection.close)
        self.assertEqual(4, connection.execute("PRAGMA user_version").fetchone()[0])

    def test_future_schema_is_refused_without_migration(self):
        self.db_path.parent.mkdir()
        connection = sqlite3.connect(self.db_path)
        connection.execute("PRAGMA user_version = 5")
        connection.commit()
        connection.close()

        with self.assertRaisesRegex(RuntimeError, "unsupported schema version 5"):
            STORE.connect(self.db_path)
        connection = sqlite3.connect(self.db_path)
        try:
            self.assertEqual(5, connection.execute("PRAGMA user_version").fetchone()[0])
            self.assertEqual([], connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall())
        finally:
            connection.close()

    def test_concurrent_reference_resolution_creates_one_execution(self):
        barrier = threading.Barrier(2)
        results = []
        errors = []

        def resolve():
            connection = STORE.connect(self.db_path)
            try:
                barrier.wait(timeout=5)
                results.append(STORE.resolve_or_create_work_item_reference(
                    connection, "github", "github.com/owner/repository", "42", "github.com/local/repo",
                ))
            except Exception as error:
                errors.append(error)
            finally:
                connection.close()

        threads = [threading.Thread(target=resolve) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

        self.assertEqual([], errors)
        self.assertEqual(2, len(results))
        self.assertEqual({results[0][0]["key"]}, {item["key"] for item, _created in results})
        self.assertEqual(1, sum(created for _item, created in results))
        connection = STORE.connect(self.db_path)
        try:
            self.assertEqual(1, connection.execute("SELECT COUNT(*) FROM work_items").fetchone()[0])
            self.assertEqual(1, connection.execute("SELECT COUNT(*) FROM work_item_references").fetchone()[0])
        finally:
            connection.close()

    def test_binding_is_idempotent_and_conflict_preserves_session_ownership(self):
        connection = STORE.connect(self.db_path)
        self.addCleanup(connection.close)
        STORE.upsert_work_item(connection, "local-a", "repo", "feature", "item")
        STORE.upsert_work_item(connection, "local-b", "repo", "feature", "other")
        STORE.ensure_session(connection, "codex", "owner")
        STORE.set_session_ownership(connection, "codex:owner", "owner", "local-a")

        self.assertTrue(STORE.bind_work_item_reference(
            connection, "local-a", "azure-devops", "DEV.AZURE.COM/Organization", "7",
        ))
        self.assertFalse(STORE.bind_work_item_reference(
            connection, "local-a", "azure-devops", "dev.azure.com/organization", "7",
        ))
        with self.assertRaisesRegex(ValueError, "already bound to local-a, not local-b"):
            STORE.bind_work_item_reference(
                connection, "local-b", "azure-devops", "dev.azure.com/organization", "7",
            )
        self.assertEqual("owner", STORE.get_session(connection, "codex:owner")["role"])
        self.assertEqual("local-a", STORE.get_session(connection, "codex:owner")["work_item"])
        self.assertEqual(
            "local-a",
            STORE.get_work_item_reference(connection, "azure-devops", "dev.azure.com/organization", "7")["work_item"],
        )

    def test_migration_rereads_the_version_under_an_immediate_transaction(self):
        class RecordingConnection(sqlite3.Connection):
            commands = []

            def execute(self, sql, *args, **kwargs):
                self.commands.append(sql)
                return super().execute(sql, *args, **kwargs)

        self.db_path.parent.mkdir(exist_ok=True)
        connection = sqlite3.connect(self.db_path, factory=RecordingConnection)
        self.addCleanup(connection.close)
        STORE._ensure_schema(connection)
        self.assertIn("BEGIN IMMEDIATE", connection.commands)
        self.assertLess(
            connection.commands.index("BEGIN IMMEDIATE"),
            len(connection.commands) - 1 - connection.commands[::-1].index("PRAGMA user_version"),
        )

    def test_connect_enables_wal_and_foreign_keys(self):
        connection = STORE.connect(self.db_path)
        self.addCleanup(connection.close)
        self.assertEqual("wal", connection.execute("PRAGMA journal_mode").fetchone()[0].lower())
        self.assertEqual(1, connection.execute("PRAGMA foreign_keys").fetchone()[0])

    def test_connect_retries_a_locked_wal_initialization(self):
        connection = mock.Mock()
        wal_attempts = 0

        def execute(statement):
            nonlocal wal_attempts
            if statement == "PRAGMA journal_mode = WAL":
                wal_attempts += 1
                if wal_attempts == 1:
                    raise sqlite3.OperationalError("database is locked")
            return mock.Mock()

        connection.execute.side_effect = execute
        with (
            mock.patch.object(STORE.sqlite3, "connect", return_value=connection),
            mock.patch.object(STORE, "_ensure_schema"),
            mock.patch.object(STORE, "_LOCK_RETRY_DELAY", 0),
        ):
            self.assertIs(connection, STORE.connect(self.db_path))
        self.assertEqual(2, wal_attempts)
        connection.close.assert_not_called()

    def test_connect_closes_a_connection_when_wal_initialization_fails(self):
        connection = mock.Mock()

        def execute(statement):
            if statement == "PRAGMA journal_mode = WAL":
                raise sqlite3.OperationalError("disk I/O error")
            return mock.Mock()

        connection.execute.side_effect = execute
        with (
            mock.patch.object(STORE.sqlite3, "connect", return_value=connection),
            mock.patch.object(STORE, "_ensure_schema"),
        ):
            with self.assertRaisesRegex(sqlite3.OperationalError, "disk I/O error"):
                STORE.connect(self.db_path)
        connection.close.assert_called_once_with()

    def test_partial_unique_index_enforces_one_owner_row_per_work_item(self):
        connection = STORE.connect(self.db_path)
        self.addCleanup(connection.close)
        connection.execute(
            "INSERT INTO work_items (key, repo, feature, item, status) VALUES ('key-1', 'repo', 'f', 'i', 'active')"
        )
        connection.execute(
            "INSERT INTO sessions (handle, host, session_id, role, work_item) "
            "VALUES ('claude:a', 'claude', 'a', 'owner', 'key-1')"
        )
        connection.commit()
        with self.assertRaises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO sessions (handle, host, session_id, role, work_item) "
                "VALUES ('codex:b', 'codex', 'b', 'owner', 'key-1')"
            )
    def test_github_references_allow_punctuation_prefixed_repository_names(self):
        for repository in (".github", "_tools", "-tools"):
            with self.subTest(repository=repository):
                self.assertEqual(
                    ("github", f"github.com/owner/{repository}", "42"),
                    STORE.normalise_work_item_reference("GitHub", f"GitHub.com/Owner/{repository}", "42"),
                )
        for provider, scope, native_id in (
            ("github", "gitlab.com/owner/.github", "42"),
            ("gitlab", "github.com/owner/.github", "42"),
            ("github", "github.com/owner/.github", "042"),
        ):
            with self.subTest(provider=provider, scope=scope, native_id=native_id):
                with self.assertRaises(ValueError):
                    STORE.normalise_work_item_reference(provider, scope, native_id)


@unittest.skipUnless(GIT, "git is required for repo-identity and hook lifecycle tests")
class RepoIdentityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="session store repo é ")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_https_remote_normalises_to_host_and_owner_name(self):
        repo = _init_repo(self.root / "https-repo", origin="https://github.com/tj-agents/core.git")
        self.assertEqual("github.com/tj-agents/core", STORE.repo_identity(repo))

    def test_ssh_scp_remote_normalises_to_host_and_owner_name(self):
        repo = _init_repo(self.root / "ssh-repo", origin="git@github.com:tj-agents/core.git")
        self.assertEqual("github.com/tj-agents/core", STORE.repo_identity(repo))

    def test_ssh_url_remote_normalises_to_host_and_owner_name(self):
        repo = _init_repo(self.root / "ssh-url-repo", origin="ssh://git@github.com/tj-agents/core.git")
        self.assertEqual("github.com/tj-agents/core", STORE.repo_identity(repo))

    def test_github_https_ssh_and_scp_remotes_are_the_same_identity(self):
        https = _init_repo(self.root / "gh-https", origin="https://github.com/tj-agents/core.git")
        ssh_url = _init_repo(self.root / "gh-ssh-url", origin="ssh://git@github.com/tj-agents/core.git")
        scp = _init_repo(self.root / "gh-scp", origin="git@github.com:tj-agents/core.git")
        identities = {STORE.repo_identity(https), STORE.repo_identity(ssh_url), STORE.repo_identity(scp)}
        self.assertEqual({"github.com/tj-agents/core"}, identities)

    def test_azure_devops_https_and_ssh_remotes_are_the_same_identity(self):
        https = _init_repo(self.root / "ado-https", origin="https://org@dev.azure.com/org/project/_git/repo")
        ssh = _init_repo(self.root / "ado-ssh", origin="git@ssh.dev.azure.com:v3/org/project/repo")
        self.assertEqual(STORE.repo_identity(https), STORE.repo_identity(ssh))
        self.assertEqual("dev.azure.com/org/project/_git/repo", STORE.repo_identity(https))

    def test_same_path_on_different_hosts_is_a_different_identity(self):
        github = _init_repo(self.root / "gh-collide", origin="https://github.com/tj-agents/core.git")
        gitlab = _init_repo(self.root / "gl-collide", origin="https://gitlab.com/tj-agents/core.git")
        self.assertNotEqual(STORE.repo_identity(github), STORE.repo_identity(gitlab))

    def test_userinfo_and_port_are_stripped_from_the_identity(self):
        repo = _init_repo(self.root / "port-repo", origin="https://user:token@github.com:443/tj-agents/core.git")
        self.assertEqual("github.com/tj-agents/core", STORE.repo_identity(repo))

    def test_no_remote_falls_back_to_absolute_git_common_dir(self):
        repo = _init_repo(self.root / "no-remote-repo")
        identity = STORE.repo_identity(repo)
        self.assertEqual(str((repo / ".git").resolve()), identity)

    def test_non_git_directory_returns_none(self):
        directory = self.root / "not-a-repo"
        directory.mkdir()
        self.assertIsNone(STORE.repo_identity(directory))

    def test_legacy_visualstudio_https_remote_normalises_like_dev_azure_com(self):
        repo = _init_repo(
            self.root / "legacy-vso-https",
            origin="https://org@ORG.visualstudio.com/DefaultCollection/Project/_git/Repo",
        )
        self.assertEqual("dev.azure.com/org/project/_git/repo", STORE.repo_identity(repo))

    def test_legacy_visualstudio_https_remote_without_defaultcollection(self):
        repo = _init_repo(
            self.root / "legacy-vso-https-no-dc", origin="https://org.visualstudio.com/project/_git/repo",
        )
        self.assertEqual("dev.azure.com/org/project/_git/repo", STORE.repo_identity(repo))

    def test_legacy_vs_ssh_remote_normalises_like_ssh_dev_azure_com(self):
        repo = _init_repo(self.root / "legacy-vso-ssh", origin="git@vs-ssh.visualstudio.com:v3/org/project/repo")
        self.assertEqual("dev.azure.com/org/project/_git/repo", STORE.repo_identity(repo))

    def test_legacy_visualstudio_short_form_without_defaultcollection_uses_repo_as_project(self):
        repo = _init_repo(self.root / "legacy-vso-short", origin="https://org.visualstudio.com/_git/repo")
        self.assertEqual("dev.azure.com/org/repo/_git/repo", STORE.repo_identity(repo))

    def test_legacy_visualstudio_short_form_with_defaultcollection_uses_repo_as_project(self):
        repo = _init_repo(
            self.root / "legacy-vso-short-dc", origin="https://org.visualstudio.com/DefaultCollection/_git/repo",
        )
        self.assertEqual("dev.azure.com/org/repo/_git/repo", STORE.repo_identity(repo))

    def test_identity_is_fully_case_folded(self):
        repo = _init_repo(self.root / "case-fold-repo", origin="https://GitHub.com/Tj-Agents/Core.git")
        self.assertEqual("github.com/tj-agents/core", STORE.repo_identity(repo))

    def test_unborn_head_keeps_repo_identity_via_git_common_dir_fallback(self):
        directory = self.root / "unborn-repo"
        directory.mkdir()
        _run_git(directory, "init")
        _run_git(directory, "config", "user.email", "test@example.com")
        _run_git(directory, "config", "user.name", "Test User")
        identity = STORE.repo_identity(directory)
        self.assertEqual(str((directory / ".git").resolve()), identity)

    def test_ambiguous_tag_and_branch_name_still_records_the_branch(self):
        repo = _init_repo(self.root / "ambiguous-name-repo")
        _run_git(repo, "checkout", "-b", "shared-name")
        _run_git(repo, "tag", "shared-name")
        branch, _head = STORE.current_branch_and_head(repo)
        self.assertEqual("shared-name", branch)

    def test_rev_parse_probe_fast_path_uses_one_git_call(self):
        repo = _init_repo(self.root / "fast-path-repo")
        calls = []
        original_run = STORE.subprocess.run

        def counting_run(*args, **kwargs):
            calls.append(args)
            return original_run(*args, **kwargs)

        with mock.patch.object(STORE.subprocess, "run", side_effect=counting_run):
            head, branch, common_dir, toplevel = STORE._rev_parse_probe(repo, STORE.GIT_TIMEOUT)
        self.assertEqual(1, len(calls))
        self.assertIsNotNone(head)
        self.assertIsNotNone(common_dir)
        self.assertEqual(Path(repo).resolve(), Path(toplevel).resolve())

    def test_rev_parse_probe_falls_back_on_unborn_head_within_three_calls(self):
        directory = self.root / "unborn-fallback-repo"
        directory.mkdir()
        _run_git(directory, "init")
        _run_git(directory, "config", "user.email", "test@example.com")
        _run_git(directory, "config", "user.name", "Test User")
        calls = []
        original_run = STORE.subprocess.run

        def counting_run(*args, **kwargs):
            calls.append(args)
            return original_run(*args, **kwargs)

        with mock.patch.object(STORE.subprocess, "run", side_effect=counting_run):
            head, branch, common_dir, toplevel = STORE._rev_parse_probe(directory, STORE.GIT_TIMEOUT)
        self.assertLessEqual(len(calls), 3)
        self.assertIsNone(head)
        self.assertIsNotNone(common_dir)

    def test_rev_parse_probe_on_a_non_repository_skips_symbolic_ref(self):
        directory = self.root / "not-a-repo-probe"
        directory.mkdir()
        calls = []
        original_run = STORE.subprocess.run

        def counting_run(*args, **kwargs):
            calls.append(args[0])
            return original_run(*args, **kwargs)

        with mock.patch.object(STORE.subprocess, "run", side_effect=counting_run):
            head, branch, common_dir, toplevel = STORE._rev_parse_probe(directory, STORE.GIT_TIMEOUT)
        self.assertIsNone(head)
        self.assertIsNone(common_dir)
        self.assertFalse(any("symbolic-ref" in call for call in calls))

    def test_rev_parse_probe_does_not_fall_back_on_timeout(self):
        repo = _init_repo(self.root / "timeout-probe-repo")
        calls = []

        def timeout_run(*args, **kwargs):
            calls.append(args[0])
            raise subprocess.TimeoutExpired(cmd=args[0], timeout=kwargs.get("timeout", 0))

        with mock.patch.object(STORE.subprocess, "run", side_effect=timeout_run):
            head, branch, common_dir, toplevel = STORE._rev_parse_probe(repo, STORE.GIT_TIMEOUT)
        self.assertEqual(1, len(calls))
        self.assertIsNone(head)
        self.assertIsNone(branch)
        self.assertIsNone(common_dir)


@unittest.skipUnless(GIT, "git is required for hook lifecycle tests")
class HookLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="session store hooks é ")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state = self.root / "state"
        self.environ = mock.patch.dict(os.environ, {"AGENT_STATE_DIRECTORY": str(self.state)}, clear=False)
        self.environ.start()
        self.addCleanup(self.environ.stop)
        self.repo = _init_repo(self.root / "repo with spaces", origin="https://github.com/tj-agents/core.git")

    def _invoke(self, event, host, payload):
        with mock.patch.object(sys, "stdin", io.StringIO(json.dumps(payload))):
            return STORE.main(["hook", event, "--host", host])

    def _row(self, handle):
        connection = STORE.connect()
        try:
            return STORE.get_session(connection, handle)
        finally:
            connection.close()

    def test_start_prompt_stop_session_end_lifecycle(self):
        payload = {"session_id": "a2bcd5c4-bf6d-4087-95e3-d7ba7f711875", "cwd": str(self.repo)}
        self.assertEqual(0, self._invoke("session-start", "claude", payload))
        handle = "claude:a2bcd5c4-bf6d-4087-95e3-d7ba7f711875"
        row = self._row(handle)
        self.assertEqual("working", row["turn_state"])
        self.assertIsNone(row["ended_at"])

        self.assertEqual(0, self._invoke("prompt", "claude", payload))
        self.assertEqual("working", self._row(handle)["turn_state"])

        self.assertEqual(0, self._invoke("stop", "claude", payload))
        row = self._row(handle)
        self.assertEqual("awaiting_input", row["turn_state"])
        self.assertEqual("github.com/tj-agents/core", row["repo"])

        self.assertEqual(0, self._invoke("session-end", "claude", payload))
        row = self._row(handle)
        self.assertIsNotNone(row["ended_at"])
        self.assertEqual("closed", row["end_reason"])

    def test_stop_moves_the_registered_cwd_to_where_the_session_now_runs(self):
        registry = self.state / "cli-sessions"
        registry.mkdir(parents=True)
        entry = {"session_id": "moved-id", "host": "claude", "cwd": str(self.root), "pid": 7,
                 "pid_started_at": 1.5, "terminal": {"kind": "windows-terminal", "session": "tab"}}
        (registry / "moved-id.json").write_text(json.dumps(entry), encoding="utf-8")
        foreign = dict(entry, session_id="other-id", host="codex")
        (registry / "other-id.json").write_text(json.dumps(foreign), encoding="utf-8")
        payload = {"session_id": "moved-id", "cwd": str(self.repo)}
        self._invoke("session-start", "claude", payload)
        self.assertEqual(0, self._invoke("stop", "claude", payload))
        moved = json.loads((registry / "moved-id.json").read_text(encoding="utf-8"))
        self.assertEqual(self.repo.resolve(), Path(moved.pop("cwd")).resolve())
        self.assertEqual(dict({key: value for key, value in entry.items() if key != "cwd"}, launch_cwd=str(self.root)), moved)
        self.assertEqual(self.repo.resolve(), Path(self._row("claude:moved-id")["cwd"]).resolve())
        self.assertEqual(0, self._invoke("stop", "claude", {"session_id": "other-id", "cwd": str(self.repo)}))
        self.assertEqual(foreign, json.loads((registry / "other-id.json").read_text(encoding="utf-8")))
        self.assertEqual(0, self._invoke("stop", "claude", {"session_id": "unregistered", "cwd": str(self.repo)}))
        self.assertFalse((registry / "unregistered.json").exists())

    def test_a_locked_registry_keeps_the_store_cwd_so_both_still_agree(self):
        registry = self.state / "cli-sessions"
        registry.mkdir(parents=True)
        entry = {"session_id": "locked-id", "host": "claude", "cwd": str(self.root), "pid": 7, "pid_started_at": 1.5}
        (registry / "locked-id.json").write_text(json.dumps(entry), encoding="utf-8")
        self._invoke("session-start", "claude", {"session_id": "locked-id", "cwd": str(self.root)})
        with mock.patch.object(STORE.os, "replace", side_effect=PermissionError("in use")), \
                mock.patch.object(STORE, "REGISTRY_REPLACE_DELAY_SECONDS", 0):
            self.assertEqual(0, self._invoke("stop", "claude", {"session_id": "locked-id", "cwd": str(self.repo)}))
        self.assertEqual(entry, json.loads((registry / "locked-id.json").read_text(encoding="utf-8")))
        row = self._row("claude:locked-id")
        self.assertEqual("awaiting_input", row["turn_state"])
        self.assertEqual((str(self.root), None, None, None, None),
                         (row["cwd"], row["repo"], row["branch"], row["head"], row["work_item"]))
        self.assertEqual([], [path.name for path in registry.iterdir() if path.name.endswith(".tmp")])

    def test_stop_records_the_checkout_root_not_a_subdirectory_or_the_enclosing_checkout(self):
        registry = self.state / "cli-sessions"
        registry.mkdir(parents=True)
        (self.repo / "tests").mkdir()
        nested = self.repo / ".worktrees" / "feature"
        _run_git(self.repo, "worktree", "add", "-q", "-b", "feature", str(nested))
        entry = {"session_id": "rooted-id", "host": "codex", "cwd": str(self.root), "pid": 7, "pid_started_at": 1.5}
        (registry / "rooted-id.json").write_text(json.dumps(entry), encoding="utf-8")
        for directory, expected in ((self.repo / "tests", self.repo), (nested, nested)):
            with self.subTest(directory=directory.name):
                payload = {"session_id": "rooted-id", "cwd": str(directory)}
                self._invoke("session-start", "codex", payload)
                self.assertEqual(0, self._invoke("stop", "codex", payload))
                registered = json.loads((registry / "rooted-id.json").read_text(encoding="utf-8"))
                self.assertEqual(expected.resolve(), Path(registered["cwd"]).resolve())
                self.assertEqual(expected.resolve(), Path(self._row("codex:rooted-id")["cwd"]).resolve())

    def test_resume_start_clears_ended_at_and_end_reason(self):
        payload = {"session_id": "exact-id", "cwd": str(self.repo)}
        handle = "codex:exact-id"
        self._invoke("session-start", "codex", payload)
        self._invoke("session-end", "codex", payload)
        self.assertIsNotNone(self._row(handle)["ended_at"])

        self._invoke("session-start", "codex", payload)
        row = self._row(handle)
        self.assertIsNone(row["ended_at"])
        self.assertIsNone(row["end_reason"])
        self.assertEqual("working", row["turn_state"])

    def test_first_end_reason_wins(self):
        connection = STORE.connect()
        try:
            STORE.start_session(connection, "claude", "id-1", self.repo)
            STORE.end_session(connection, "claude:id-1", "finished")
            STORE.end_session(connection, "claude:id-1", "closed")
            row = STORE.get_session(connection, "claude:id-1")
        finally:
            connection.close()
        self.assertEqual("finished", row["end_reason"])

    def test_reopen_clears_only_the_matching_end_reason(self):
        connection = STORE.connect()
        try:
            STORE.start_session(connection, "claude", "id-1", self.repo)
            STORE.end_session(connection, "claude:id-1", "finished")
            STORE.reopen_session(connection, "claude:id-1", "closed")
            kept = STORE.get_session(connection, "claude:id-1")
            STORE.reopen_session(connection, "claude:id-1", "finished")
            reopened = STORE.get_session(connection, "claude:id-1")
        finally:
            connection.close()
        self.assertEqual("finished", kept["end_reason"])
        self.assertIsNone(reopened["ended_at"])
        self.assertIsNone(reopened["end_reason"])

    def test_session_start_hook_malformed_stdin_exits_zero(self):
        with mock.patch.object(sys, "stdin", io.StringIO("not json {{{")):
            self.assertEqual(0, STORE.main(["hook", "session-start", "--host", "claude"]))

    def test_session_start_hook_unwritable_db_path_exits_zero(self):
        blocker = self.root / "blocker-file"
        blocker.write_text("not a directory", encoding="utf-8")
        with mock.patch.dict(os.environ, {"AGENT_STATE_DIRECTORY": str(blocker / "nested")}, clear=False):
            payload = {"session_id": "id", "cwd": str(self.repo)}
            with mock.patch.object(sys, "stdin", io.StringIO(json.dumps(payload))):
                self.assertEqual(0, STORE.main(["hook", "session-start", "--host", "claude"]))

    def test_work_item_auto_attach_requires_exactly_one_match(self):
        connection = STORE.connect()
        try:
            repo = "github.com/tj-agents/core"
            STORE.upsert_work_item(connection, "github.com/tj-agents/core:f/a", repo, "f", "a", status="active", branch="topic")
            STORE.start_session(connection, "claude", "attach-id", self.repo)
            STORE.record_stop(connection, "claude:attach-id", self.repo)
        finally:
            connection.close()

        _run_git(self.repo, "checkout", "-b", "topic")
        connection = STORE.connect()
        try:
            STORE.record_stop(connection, "claude:attach-id", self.repo)
            row = STORE.get_session(connection, "claude:attach-id")
            self.assertEqual("github.com/tj-agents/core:f/a", row["work_item"])
        finally:
            connection.close()

        connection = STORE.connect()
        try:
            STORE.upsert_work_item(connection, "github.com/tj-agents/core:f/b", "github.com/tj-agents/core", "f", "b", status="active", branch="topic")
            STORE.start_session(connection, "claude", "attach-id-2", self.repo)
            STORE.record_stop(connection, "claude:attach-id-2", self.repo)
            row = STORE.get_session(connection, "claude:attach-id-2")
            self.assertIsNone(row["work_item"])
        finally:
            connection.close()

    def test_concurrent_writers_from_threads_keep_every_row(self):
        STORE.connect().close()
        errors = []

        def write(index):
            try:
                connection = STORE.connect()
                try:
                    STORE.start_session(connection, "claude", f"thread-{index}", self.repo)
                finally:
                    connection.close()
            except Exception as error:  # noqa: BLE001
                errors.append(error)

        threads = [threading.Thread(target=write, args=(index,)) for index in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual([], errors)
        connection = STORE.connect()
        try:
            handles = {row["handle"] for row in connection.execute("SELECT handle FROM sessions").fetchall()}
        finally:
            connection.close()
        self.assertEqual({f"claude:thread-{index}" for index in range(8)}, handles)

    def test_resume_start_after_handoff_clears_role_but_keeps_work_item(self):
        connection = STORE.connect()
        try:
            STORE.upsert_work_item(connection, "some-key", "repo", "f", "a", status="active")
            STORE.start_session(connection, "claude", "id-1", self.repo)
            STORE.set_session_ownership(connection, "claude:id-1", "owner", "some-key")
            STORE.end_session(connection, "claude:id-1", "handed_off", successor="codex:id-2")
            STORE.start_session(connection, "claude", "id-1", self.repo)
            row = STORE.get_session(connection, "claude:id-1")
        finally:
            connection.close()
        self.assertIsNone(row["role"])
        self.assertEqual("some-key", row["work_item"])

    def test_resume_start_keeps_role_when_not_handed_off(self):
        connection = STORE.connect()
        try:
            STORE.upsert_work_item(connection, "some-key", "repo", "f", "a", status="active")
            STORE.start_session(connection, "claude", "id-2", self.repo)
            STORE.set_session_ownership(connection, "claude:id-2", "owner", "some-key")
            STORE.end_session(connection, "claude:id-2", "closed")
            STORE.start_session(connection, "claude", "id-2", self.repo)
            row = STORE.get_session(connection, "claude:id-2")
        finally:
            connection.close()
        self.assertEqual("owner", row["role"])

    def test_git_decodes_with_utf8_and_replaces_errors(self):
        with mock.patch.object(STORE.subprocess, "run") as run:
            run.return_value = subprocess.CompletedProcess(args=[], returncode=0, stdout="ok\n")
            STORE._git(self.repo, "status")
        _, kwargs = run.call_args
        self.assertEqual("utf-8", kwargs.get("encoding"))
        self.assertEqual("replace", kwargs.get("errors"))

    def test_session_start_and_stop_each_issue_at_most_two_git_subprocesses(self):
        payload = {"session_id": "budget-id", "cwd": str(self.repo)}
        calls = []
        original_run = STORE.subprocess.run

        def counting_run(*args, **kwargs):
            calls.append(args)
            return original_run(*args, **kwargs)

        with mock.patch.object(STORE.subprocess, "run", side_effect=counting_run):
            calls.clear()
            self.assertEqual(0, self._invoke("session-start", "claude", payload))
            self.assertLessEqual(len(calls), 2)

            calls.clear()
            self.assertEqual(0, self._invoke("stop", "claude", payload))
            self.assertLessEqual(len(calls), 2)


class ClaimWorkItemTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="claim work item é ")
        self.addCleanup(self.temp.cleanup)
        self.db_path = Path(self.temp.name) / "state" / "sessions.db"

    def _connection(self):
        return STORE.connect(self.db_path)

    def _seed_work_item(self, connection, key):
        STORE.upsert_work_item(connection, key, "repo", "feature", "item")

    def test_claim_work_item_demotes_every_other_owner_row_for_the_key(self):
        connection = self._connection()
        try:
            self._seed_work_item(connection, "key-1")
            STORE.ensure_session(connection, "claude", "a")
            STORE.set_session_ownership(connection, "claude:a", "owner", "key-1")
            STORE.end_session(connection, "claude:a", "closed")
            STORE.ensure_session(connection, "codex", "b")
            removed = STORE.claim_work_item(connection, "codex:b", "key-1", "owner")
            predecessor = STORE.get_session(connection, "claude:a")
            successor = STORE.get_session(connection, "codex:b")
        finally:
            connection.close()
        self.assertIsNone(removed)
        self.assertIsNone(predecessor["role"])
        self.assertEqual("owner", successor["role"])

    def test_claim_work_item_rollback_leaves_predecessor_untouched_on_write_failure(self):
        connection = self._connection()
        try:
            self._seed_work_item(connection, "key-1")
            STORE.ensure_session(connection, "claude", "a")
            STORE.set_session_ownership(connection, "claude:a", "owner", "key-1")
            STORE.ensure_session(connection, "codex", "b")
            with mock.patch.object(STORE, "_set_session_ownership_locked", side_effect=RuntimeError("boom")):
                with self.assertRaises(RuntimeError):
                    STORE.claim_work_item(connection, "codex:b", "key-1", "owner", predecessor="claude:a")
            predecessor = STORE.get_session(connection, "claude:a")
            successor = STORE.get_session(connection, "codex:b")
        finally:
            connection.close()
        self.assertIsNone(predecessor["ended_at"])
        self.assertEqual("owner", predecessor["role"])
        self.assertIsNone(successor["role"])

    def test_concurrent_handoff_claims_never_yield_two_owners(self):
        connection = self._connection()
        try:
            self._seed_work_item(connection, "key-1")
            STORE.ensure_session(connection, "claude", "a")
            STORE.set_session_ownership(connection, "claude:a", "owner", "key-1")
            STORE.ensure_session(connection, "codex", "b")
            STORE.ensure_session(connection, "codex", "c")
        finally:
            connection.close()

        original_live_owners = STORE.live_owners

        def slow_live_owners(connection, key):
            result = original_live_owners(connection, key)
            time.sleep(0.3)
            return result

        errors = {}

        def claim(name, handle):
            own_connection = self._connection()
            try:
                STORE.claim_work_item(own_connection, handle, "key-1", "owner", predecessor="claude:a")
            except Exception as error:  # noqa: BLE001
                errors[name] = error
            finally:
                own_connection.close()

        with mock.patch.object(STORE, "live_owners", side_effect=slow_live_owners):
            threads = [
                threading.Thread(target=claim, args=("b", "codex:b")),
                threading.Thread(target=claim, args=("c", "codex:c")),
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=10)

        connection = self._connection()
        try:
            owners = connection.execute("SELECT handle FROM sessions WHERE role = 'owner'").fetchall()
        finally:
            connection.close()
        self.assertEqual(1, len(owners))

    def test_demoted_former_owner_resumed_via_session_start_is_not_an_owner(self):
        connection = self._connection()
        try:
            self._seed_work_item(connection, "key-1")
            STORE.ensure_session(connection, "claude", "a")
            STORE.set_session_ownership(connection, "claude:a", "owner", "key-1")
            STORE.end_session(connection, "claude:a", "closed")
            STORE.ensure_session(connection, "codex", "b")
            STORE.claim_work_item(connection, "codex:b", "key-1", "owner")
            STORE.start_session(connection, "claude", "a", self.temp.name)
            row = STORE.get_session(connection, "claude:a")
        finally:
            connection.close()
        self.assertIsNone(row["role"])

    def test_claim_work_item_refused_owner_claim_leaves_branch_unchanged(self):
        connection = self._connection()
        try:
            self._seed_work_item(connection, "key-1")
            STORE.ensure_session(connection, "claude", "a")
            STORE.set_session_ownership(connection, "claude:a", "owner", "key-1")
            STORE.ensure_session(connection, "codex", "b")
            with self.assertRaises(ValueError):
                STORE.claim_work_item(connection, "codex:b", "key-1", "owner", branch="feature-branch")
            item = STORE.get_work_item(connection, "key-1")
        finally:
            connection.close()
        self.assertIsNone(item["branch"])

    def test_claim_work_item_bounded_claim_leaves_branch_unchanged(self):
        connection = self._connection()
        try:
            self._seed_work_item(connection, "key-1")
            STORE.ensure_session(connection, "codex", "b")
            STORE.claim_work_item(connection, "codex:b", "key-1", "bounded: docs", branch="feature-branch")
            item = STORE.get_work_item(connection, "key-1")
        finally:
            connection.close()
        self.assertIsNone(item["branch"])

    def test_claim_work_item_successful_owner_claim_fills_branch(self):
        connection = self._connection()
        try:
            self._seed_work_item(connection, "key-1")
            STORE.ensure_session(connection, "codex", "b")
            STORE.claim_work_item(connection, "codex:b", "key-1", "owner", branch="feature-branch")
            item = STORE.get_work_item(connection, "key-1")
        finally:
            connection.close()
        self.assertEqual("feature-branch", item["branch"])

    def test_registered_claim_rejects_blank_title_without_writing_a_session_or_work_item(self):
        connection = self._connection()
        try:
            with self.assertRaisesRegex(ValueError, "title must be non-empty"):
                STORE.claim_registered_work_item(
                    connection, "claude", "registered", self.temp.name, "key-1", "repo", "repo", "feature", "item",
                    "owner", title="  ", registration={"title": "Claude tab", "pid": 1, "pid_started_at": 1.0},
                )
            self.assertIsNone(STORE.get_work_item(connection, "key-1"))
            self.assertIsNone(STORE.get_session(connection, "claude:registered"))
        finally:
            connection.close()



if __name__ == "__main__":
    unittest.main()
