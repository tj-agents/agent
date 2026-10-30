#!/usr/bin/env python3
"""Single SQLite-backed store for session and work-item status, written by hooks (Python 3.9+)."""

import argparse
import contextlib
from dataclasses import dataclass, fields
import functools
import importlib.util
import json
import os
import re
import socket
import sqlite3
import subprocess
import sys
import time
from typing import Optional
import uuid
from pathlib import Path


@functools.lru_cache(maxsize=None)
def _load_repository_identity():
    path = Path(__file__).resolve().with_name("repository_identity.py")
    spec = importlib.util.spec_from_file_location("session_store_repository_identity", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"The shared repository_identity.py helper was not found relative to {path.parent}.")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


SCHEMA_VERSION = 4
GIT_TIMEOUT = 3
HOOK_GIT_TIMEOUT = 2
PROMPT_TITLE_LENGTH = 80
REGISTRY_REPLACE_ATTEMPTS = 40
REGISTRY_REPLACE_DELAY_SECONDS = 0.05
_LOCK_RETRY_DELAY = 0.05

WORK_ITEMS_SCHEMA = """
CREATE TABLE IF NOT EXISTS work_items (
    key TEXT PRIMARY KEY,
    repo TEXT NOT NULL,
    feature TEXT,
    item TEXT,
    status TEXT NOT NULL,
    branch TEXT,
    pr TEXT,
    title TEXT,
    plan_path TEXT,
    updated_at REAL,
    CHECK ((feature IS NULL AND item IS NULL) OR (feature IS NOT NULL AND item IS NOT NULL))
)
"""

WORK_ITEM_REFERENCES_SCHEMA = """
CREATE TABLE IF NOT EXISTS work_item_references (
    provider TEXT NOT NULL,
    scope TEXT NOT NULL,
    native_id TEXT NOT NULL,
    work_item TEXT NOT NULL REFERENCES work_items(key),
    PRIMARY KEY (provider, scope, native_id)
)
"""

WORK_ITEM_REFERENCES_ITEM_INDEX = """
CREATE INDEX IF NOT EXISTS idx_work_item_references_item ON work_item_references(work_item)
"""

_GITHUB_SCOPE = re.compile(r"github\.com/[a-z0-9][a-z0-9_.-]*/[a-z0-9_.-]+$")
_AZURE_SCOPE = re.compile(r"dev\.azure\.com/[a-z0-9][a-z0-9-]*$")
_NATIVE_ID = re.compile(r"[1-9][0-9]*$")
_EXECUTION_KEY = re.compile(r"execution:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")

SESSIONS_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    handle TEXT PRIMARY KEY,
    host TEXT NOT NULL,
    session_id TEXT NOT NULL,
    machine TEXT,
    profile TEXT,
    title TEXT,
    cwd TEXT,
    repo TEXT,
    branch TEXT,
    head TEXT,
    work_item TEXT REFERENCES work_items(key),
    role TEXT,
    pid INTEGER,
    pid_started_at REAL,
    started_at REAL,
    last_activity_at REAL,
    turn_state TEXT,
    ended_at REAL,
    end_reason TEXT,
    predecessor TEXT,
    successor TEXT
)
"""

SESSIONS_ONE_OWNER_INDEX = """
CREATE UNIQUE INDEX IF NOT EXISTS idx_sessions_one_owner_per_work_item
ON sessions(work_item) WHERE role = 'owner'
"""


@dataclass(eq=False)
class Session:
    handle: str
    host: str
    session_id: str
    machine: Optional[str]
    profile: Optional[str]
    title: Optional[str]
    cwd: Optional[str]
    repo: Optional[str]
    branch: Optional[str]
    head: Optional[str]
    work_item: Optional[str]
    role: Optional[str]
    pid: Optional[int]
    pid_started_at: Optional[float]
    started_at: Optional[float]
    last_activity_at: Optional[float]
    turn_state: Optional[str]
    ended_at: Optional[float]
    end_reason: Optional[str]
    predecessor: Optional[str]
    successor: Optional[str]

    @property
    def ended(self):
        return self.ended_at is not None

    def __eq__(self, other):
        if not isinstance(other, Session):
            return NotImplemented
        return self.handle == other.handle

    def __hash__(self):
        return hash(self.handle)


@dataclass(eq=False)
class WorkItem:
    key: str
    repo: str
    feature: Optional[str]
    item: Optional[str]
    status: str
    branch: Optional[str]
    pr: Optional[str]
    title: Optional[str]
    plan_path: Optional[str]
    updated_at: Optional[float]

    @property
    def label(self):
        return f"{self.feature}/{self.item}" if self.feature is not None else self.key

    def __eq__(self, other):
        if not isinstance(other, WorkItem):
            return NotImplemented
        return self.key == other.key

    def __hash__(self):
        return hash(self.key)


@dataclass(frozen=True)
class WorkItemReference:
    provider: str
    scope: str
    native_id: str
    work_item: str


def _from_row(record_type, row):
    if row is None:
        return None
    return record_type(**{field.name: row[field.name] for field in fields(record_type)})


def state_directory():
    override = os.environ.get("AGENT_STATE_DIRECTORY")
    return Path(override) if override else Path.home() / ".agents-state"


def database_path():
    return state_directory() / "sessions.db"


def follow_registered_cwd(host, session_id, cwd):
    path = state_directory() / "cli-sessions" / f"{session_id}.json"
    try:
        entry = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if (not isinstance(entry, dict) or entry.get("session_id") != session_id
            or entry.get("host") not in (None, host) or entry.get("cwd") == str(cwd)):
        return None
    entry.setdefault("launch_cwd", entry.get("cwd"))
    entry["cwd"] = str(cwd)
    staging = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        staging.write_text(json.dumps(entry, sort_keys=True) + "\n", encoding="utf-8")
        for _attempt in range(REGISTRY_REPLACE_ATTEMPTS):
            try:
                os.replace(staging, path)
                return True
            except PermissionError:
                time.sleep(REGISTRY_REPLACE_DELAY_SECONDS)
        return False
    except OSError:
        return False
    finally:
        staging.unlink(missing_ok=True)


def _execute_with_lock_retry(connection, statement):
    deadline = time.monotonic() + 5
    while True:
        try:
            return connection.execute(statement)
        except sqlite3.OperationalError as error:
            message = str(error).lower()
            if ("locked" not in message and "busy" not in message) or time.monotonic() >= deadline:
                raise
            time.sleep(_LOCK_RETRY_DELAY)


@contextlib.contextmanager
def _immediate_transaction(connection):
    connection.execute("BEGIN IMMEDIATE")
    try:
        yield
        connection.commit()
    except Exception:
        connection.rollback()
        raise


def current_machine():
    return socket.gethostname()


def profile_directory(host):
    if host == "claude":
        value = os.environ.get("CLAUDE_CONFIG_DIR") or str(Path.home() / ".claude")
    else:
        value = os.environ.get("CODEX_HOME") or str(Path.home() / ".codex")
    return str(Path(value))


def _run_git(cwd, args, timeout):
    try:
        result = subprocess.run(
            ["git", "-C", str(cwd), *args],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return None, True
    except (OSError, subprocess.SubprocessError):
        return None, False
    if result.returncode != 0:
        return None, False
    output = result.stdout.strip()
    return (output or None), False


def _git(cwd, *args, timeout=GIT_TIMEOUT):
    output, _timed_out = _run_git(cwd, args, timeout)
    return output


def _branch_from_symbolic_ref(value):
    if not value:
        return None
    prefix = "refs/heads/"
    return value[len(prefix):] if value.startswith(prefix) else None


def _rev_parse_probe(cwd, timeout):
    output, timed_out = _run_git(
        cwd, ["rev-parse", "HEAD", "--git-common-dir", "--symbolic-full-name", "HEAD", "--show-toplevel"], timeout,
    )
    if output:
        lines = output.splitlines()
        if len(lines) >= 3:
            head = lines[0].strip() or None
            common_dir = lines[1].strip() or None
            branch = _branch_from_symbolic_ref(lines[2].strip())
            toplevel = os.path.normpath(lines[3].strip()) if len(lines) >= 4 and lines[3].strip() else None
            return head, branch, common_dir, toplevel
    if timed_out:
        return None, None, None, None
    common_dir = _git(cwd, "rev-parse", "--git-common-dir", timeout=timeout)
    if not common_dir:
        return None, None, None, None
    symbolic = _git(cwd, "symbolic-ref", "-q", "HEAD", timeout=timeout)
    branch = _branch_from_symbolic_ref(symbolic)
    return None, branch, common_dir, None


def _is_default_branch(cwd, common_dir, branch):
    if branch in ("main", "master"):
        return True
    if not branch or not common_dir:
        return False
    path = Path(common_dir)
    if not path.is_absolute():
        path = Path(cwd) / path
    try:
        ref = (path / "refs" / "remotes" / "origin" / "HEAD").read_text(encoding="utf-8").strip()
    except OSError:
        return False
    return ref == f"ref: refs/remotes/origin/{branch}"


def _collect_checkout(cwd, timeout=HOOK_GIT_TIMEOUT):
    head, branch, common_dir, toplevel = _rev_parse_probe(cwd, timeout)
    repo = _load_repository_identity().repository_identity(
        cwd,
        lambda directory, *arguments: _git(directory, *arguments, timeout=timeout),
    )
    return toplevel or str(cwd), repo, branch, head, _is_default_branch(cwd, common_dir, branch)


def _collect_repo_branch_head(cwd, timeout=HOOK_GIT_TIMEOUT):
    _checkout, repo, branch, head, _default = _collect_checkout(cwd, timeout)
    return repo, branch, head


def repo_identity(cwd):
    repo, _branch, _head = _collect_repo_branch_head(cwd, timeout=GIT_TIMEOUT)
    return repo


def current_branch_and_head(cwd):
    _repo, branch, head = _collect_repo_branch_head(cwd, timeout=GIT_TIMEOUT)
    return branch, head


def session_handle(host, session_id):
    return f"{host}:{session_id}"


def work_item_key(repo, feature, item):
    return f"{repo}:{feature}/{item}"


def execution_key():
    return f"execution:{uuid.uuid4()}"


def validate_execution_key(value):
    if not isinstance(value, str) or not _EXECUTION_KEY.fullmatch(value):
        raise ValueError("execution key must be execution:<lowercase UUID>")
    return value


def normalise_work_item_reference(provider, scope, native_id):
    if not all(isinstance(value, str) and value.strip() for value in (provider, scope, native_id)):
        raise ValueError("provider, scope and native id are required")
    provider = provider.strip().casefold()
    scope = scope.strip().casefold()
    native_id = native_id.strip()
    if provider == "github":
        if not _GITHUB_SCOPE.fullmatch(scope):
            raise ValueError("GitHub scope must be github.com/<lowercase-owner>/<lowercase-repository>")
    elif provider == "azure-devops":
        if not _AZURE_SCOPE.fullmatch(scope):
            raise ValueError("Azure DevOps scope must be dev.azure.com/<lowercase-organization>")
    else:
        raise ValueError("provider must be github or azure-devops")
    if not _NATIVE_ID.fullmatch(native_id):
        raise ValueError("native id must be a positive canonical decimal number")
    return provider, scope, native_id


def prompt_title(prompt):
    if not isinstance(prompt, str):
        return None
    text = " ".join(prompt.split())
    if not text or text.startswith(("/", "<")):
        return None
    if len(text) <= PROMPT_TITLE_LENGTH:
        return text
    cut = text[:PROMPT_TITLE_LENGTH].rsplit(" ", 1)[0]
    return f"{cut}…"


class SessionStore:
    def __init__(self, path=None):
        self._connection = self._connect(path)

    def __enter__(self):
        return self

    def __exit__(self, *_exception):
        self.close()

    def close(self):
        self._connection.close()

    @classmethod
    def _connect(cls, path):
        target = Path(path) if path is not None else database_path()
        target.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(str(target), timeout=5)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA busy_timeout = 5000")
            _execute_with_lock_retry(connection, "PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA foreign_keys = ON")
            cls._ensure_schema(connection)
            return connection
        except Exception:
            connection.close()
            raise

    @staticmethod
    def _ensure_schema(connection):
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        if version not in (0, SCHEMA_VERSION):
            raise RuntimeError(f"session store has unsupported schema version {version}")
        with _immediate_transaction(connection):
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, SCHEMA_VERSION):
                raise RuntimeError(f"session store has unsupported schema version {version}")
            connection.execute(WORK_ITEMS_SCHEMA)
            connection.execute(SESSIONS_SCHEMA)
            connection.execute(SESSIONS_ONE_OWNER_INDEX)
            connection.execute(WORK_ITEM_REFERENCES_SCHEMA)
            connection.execute(WORK_ITEM_REFERENCES_ITEM_INDEX)
            if version != SCHEMA_VERSION:
                connection.execute("PRAGMA user_version = %d" % SCHEMA_VERSION)

    def _sessions(self, clause="", arguments=()):
        rows = self._connection.execute(f"SELECT * FROM sessions {clause}", arguments).fetchall()
        return [_from_row(Session, row) for row in rows]

    def get_session(self, handle):
        row = self._connection.execute("SELECT * FROM sessions WHERE handle = ?", (handle,)).fetchone()
        return _from_row(Session, row)

    def sessions_for_work_item(self, item):
        return self._sessions("WHERE work_item = ? ORDER BY started_at", (item,))

    def sessions_with_work_item(self):
        return self._sessions("WHERE work_item IS NOT NULL")

    def live_owners(self, item):
        return self._sessions("WHERE work_item = ? AND role = 'owner' AND ended_at IS NULL", (item,))

    def sessions_by_repo(self, repo):
        return self._sessions("WHERE repo = ?", (repo,))

    def sessions_on_hosts(self, hosts, session_id=None):
        placeholders = ", ".join("?" for _ in hosts)
        exact = " AND session_id = ?" if session_id else ""
        return self._sessions(
            f"WHERE host IN ({placeholders}){exact}", (*hosts, *((session_id,) if session_id else ())),
        )

    def interrupted_candidates(self, repo=None):
        if repo:
            return self._sessions("WHERE ended_at IS NULL AND repo = ?", (repo,))
        return self._sessions("WHERE ended_at IS NULL")

    def get_work_item(self, key):
        row = self._connection.execute("SELECT * FROM work_items WHERE key = ?", (key,)).fetchone()
        return _from_row(WorkItem, row)

    def get_work_item_reference(self, provider, scope, native_id):
        provider, scope, native_id = normalise_work_item_reference(provider, scope, native_id)
        row = self._connection.execute(
            "SELECT provider, scope, native_id, work_item FROM work_item_references "
            "WHERE provider = ? AND scope = ? AND native_id = ?",
            (provider, scope, native_id),
        ).fetchone()
        return _from_row(WorkItemReference, row)

    def work_item_references(self, key):
        rows = self._connection.execute(
            "SELECT provider, scope, native_id, work_item FROM work_item_references WHERE work_item = ? "
            "ORDER BY provider, scope, native_id",
            (key,),
        ).fetchall()
        return [_from_row(WorkItemReference, row) for row in rows]

    def _insert_work_item_reference_locked(self, key, provider, scope, native_id):
        reference = self.get_work_item_reference(provider, scope, native_id)
        if reference is not None:
            if reference.work_item == key:
                return False
            raise ValueError(
                f"{provider}/{scope}/{native_id} is already bound to {reference.work_item}, not {key}"
            )
        if self.get_work_item(key) is None:
            raise ValueError(f"work item does not exist: {key}")
        self._connection.execute(
            "INSERT INTO work_item_references (provider, scope, native_id, work_item) VALUES (?, ?, ?, ?)",
            (provider, scope, native_id, key),
        )
        return True

    def bind_work_item_reference(self, key, provider, scope, native_id):
        provider, scope, native_id = normalise_work_item_reference(provider, scope, native_id)
        with _immediate_transaction(self._connection):
            return self._insert_work_item_reference_locked(key, provider, scope, native_id)

    def resolve_or_create_work_item_reference(self, provider, scope, native_id, repo, branch=None):
        provider, scope, native_id = normalise_work_item_reference(provider, scope, native_id)
        with _immediate_transaction(self._connection):
            reference = self.get_work_item_reference(provider, scope, native_id)
            if reference is not None:
                item = self.get_work_item(reference.work_item)
                if item is None:
                    raise RuntimeError(f"reference {provider}/{scope}/{native_id} has a missing work item")
                return item, False
            adoptable = self._branch_work_items_to_absorb_locked("", repo, branch) if branch else []
            if len(adoptable) == 1:
                key = adoptable[0]
                created = False
            else:
                key = execution_key()
                self._upsert_work_item_locked(key, repo, None, None, status="active", branch=branch)
                created = True
            self._insert_work_item_reference_locked(key, provider, scope, native_id)
            return self.get_work_item(key), created

    def _start_session_locked(
        self, host, session_id, cwd, title=None, pid=None, pid_started_at=None, repo=None, branch=None, head=None,
    ):
        handle = session_handle(host, session_id)
        machine = current_machine()
        profile = profile_directory(host)
        now = time.time()
        self._connection.execute(
            """
            INSERT INTO sessions (
                handle, host, session_id, machine, profile, title, cwd, repo, branch, head,
                pid, pid_started_at, started_at, last_activity_at, turn_state, ended_at, end_reason
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'working', NULL, NULL)
            ON CONFLICT(handle) DO UPDATE SET
                machine = excluded.machine,
                profile = excluded.profile,
                title = COALESCE(excluded.title, sessions.title),
                cwd = excluded.cwd,
                repo = excluded.repo,
                branch = excluded.branch,
                head = excluded.head,
                pid = excluded.pid,
                pid_started_at = excluded.pid_started_at,
                started_at = excluded.started_at,
                last_activity_at = excluded.started_at,
                turn_state = 'working',
                ended_at = NULL,
                end_reason = NULL,
                role = CASE WHEN sessions.end_reason = 'handed_off' THEN NULL ELSE sessions.role END
            """,
            (handle, host, session_id, machine, profile, title, str(cwd), repo, branch, head,
             pid, pid_started_at, now, now),
        )
        return handle

    def start_session(self, host, session_id, cwd, title=None, pid=None, pid_started_at=None):
        repo, branch, head = _collect_repo_branch_head(cwd)
        handle = self._start_session_locked(
            host, session_id, cwd, title=title, pid=pid, pid_started_at=pid_started_at,
            repo=repo, branch=branch, head=head,
        )
        self._connection.commit()
        if repo and branch:
            self.attach_work_item_if_unique(repo, branch, handle)
            self._connection.commit()
        return handle

    def ensure_session(self, host, session_id, cwd=None):
        handle = session_handle(host, session_id)
        if self.get_session(handle) is not None:
            return handle
        machine = current_machine()
        profile = profile_directory(host)
        repo, branch, head = _collect_repo_branch_head(cwd) if cwd else (None, None, None)
        now = time.time()
        self._connection.execute(
            """
            INSERT INTO sessions (
                handle, host, session_id, machine, profile, cwd, repo, branch, head,
                started_at, last_activity_at, turn_state
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'working')
            ON CONFLICT(handle) DO NOTHING
            """,
            (handle, host, session_id, machine, profile, str(cwd) if cwd else None, repo, branch, head, now, now),
        )
        self._connection.commit()
        return handle

    def attach_session_client(self, handle, pid, pid_started_at, cwd):
        self._connection.execute(
            "UPDATE sessions SET pid = ?, pid_started_at = ?, cwd = ? WHERE handle = ? AND ended_at IS NULL",
            (pid, pid_started_at, str(cwd), handle),
        )
        self._connection.commit()

    def record_prompt(self, handle, prompt=None):
        self._connection.execute(
            "UPDATE sessions SET turn_state = 'working', last_activity_at = ?, title = COALESCE(title, ?) "
            "WHERE handle = ?",
            (time.time(), prompt_title(prompt), handle),
        )
        self._connection.commit()

    def record_stop(self, handle, cwd, follow=None):
        checkout, repo, branch, head, default = _collect_checkout(cwd)
        if follow is not None and follow(checkout) is False:
            checkout = repo = branch = head = None
        now = time.time()
        self._connection.execute(
            """
            UPDATE sessions SET turn_state = 'awaiting_input', last_activity_at = ?, cwd = COALESCE(?, cwd),
                repo = COALESCE(?, repo), branch = COALESCE(?, branch), head = COALESCE(?, head)
            WHERE handle = ?
            """,
            (now, checkout, repo, branch, head, handle),
        )
        self._connection.commit()
        if repo and branch:
            self.attach_work_item_if_unique(repo, branch, handle, create=not default)
            self._connection.commit()

    def _end_session_locked(self, handle, reason, successor=None):
        self._connection.execute(
            """
            UPDATE sessions SET
                ended_at = COALESCE(ended_at, ?),
                end_reason = COALESCE(end_reason, ?),
                successor = COALESCE(successor, ?)
            WHERE handle = ?
            """,
            (time.time(), reason, successor, handle),
        )

    def end_session(self, handle, reason, successor=None):
        self._end_session_locked(handle, reason, successor=successor)
        self._connection.commit()

    def reopen_session(self, handle, reason):
        self._connection.execute(
            "UPDATE sessions SET ended_at = NULL, end_reason = NULL WHERE handle = ? AND end_reason = ?",
            (handle, reason),
        )
        self._connection.commit()

    def delete_session(self, session_id):
        self._connection.execute("DELETE FROM sessions WHERE session_id = ?", (session_id,))
        self._connection.commit()

    def attach_work_item_if_unique(self, repo, branch, handle, create=False):
        with _immediate_transaction(self._connection):
            session = self.get_session(handle)
            if session is None or session.work_item is not None:
                return
            matches = self._connection.execute(
                "SELECT key FROM work_items WHERE repo = ? AND branch = ? AND status != 'done'",
                (repo, branch),
            ).fetchall()
            if len(matches) == 1:
                key = matches[0]["key"]
            elif not matches and create:
                key = execution_key()
                self._upsert_work_item_locked(
                    key, repo, None, None, status="active", branch=branch, title=session.title or branch,
                )
            else:
                return
            self._connection.execute("UPDATE sessions SET work_item = ? WHERE handle = ?", (key, handle))

    def _branch_work_items_to_absorb_locked(self, key, repo, branch):
        return [row["key"] for row in self._connection.execute(
            """
            SELECT w.key FROM work_items w
            WHERE w.repo = ? AND w.branch = ? AND w.key != ? AND w.feature IS NULL AND w.status != 'done'
                AND NOT EXISTS (SELECT 1 FROM work_item_references r WHERE r.work_item = w.key)
                AND NOT EXISTS (SELECT 1 FROM sessions s WHERE s.work_item = w.key AND s.role IS NOT NULL)
            """,
            (repo, branch, key),
        ).fetchall()]

    def _absorb_branch_work_items_locked(self, key, repo, branch):
        for absorbed in self._branch_work_items_to_absorb_locked(key, repo, branch):
            self._connection.execute("UPDATE sessions SET work_item = ? WHERE work_item = ?", (key, absorbed))
            self._connection.execute("DELETE FROM work_items WHERE key = ?", (absorbed,))

    def _upsert_work_item_locked(
        self, key, repo, feature, item, status=None, branch=None, pr=None, title=None, plan_path=None,
    ):
        if (feature is None) != (item is None):
            raise ValueError("work item feature and item must both be present or both be absent")
        if feature is None:
            validate_execution_key(key)
        now = time.time()
        self._connection.execute(
            """
            INSERT INTO work_items (key, repo, feature, item, status, branch, pr, title, plan_path, updated_at)
            VALUES (?, ?, ?, ?, COALESCE(?, 'active'), ?, ?, ?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                status = COALESCE(?, work_items.status),
                branch = COALESCE(work_items.branch, ?),
                pr = COALESCE(?, work_items.pr),
                title = COALESCE(?, work_items.title),
                plan_path = COALESCE(?, work_items.plan_path),
                updated_at = ?
            """,
            (key, repo, feature, item, status, branch, pr, title, plan_path, now,
             status, branch, pr, title, plan_path, now),
        )

    def upsert_work_item(
        self, key, repo, feature, item, status=None, branch=None, pr=None, title=None, plan_path=None,
    ):
        self._upsert_work_item_locked(
            key, repo, feature, item, status=status, branch=branch, pr=pr, title=title, plan_path=plan_path,
        )
        if feature is not None and branch:
            self._absorb_branch_work_items_locked(key, repo, branch)
        self._connection.commit()

    def set_work_item_status(self, key, status, pr=None, branch=None):
        self._connection.execute(
            """
            UPDATE work_items SET status = ?, pr = COALESCE(?, pr), branch = COALESCE(branch, ?), updated_at = ?
            WHERE key = ?
            """,
            (status, pr, branch, time.time(), key),
        )
        self._connection.commit()

    def _set_session_ownership_locked(self, handle, role, item, predecessor=None):
        self._connection.execute(
            "UPDATE sessions SET role = ?, work_item = ?, predecessor = COALESCE(?, predecessor) WHERE handle = ?",
            (role, item, predecessor, handle),
        )

    def set_session_ownership(self, handle, role, item, predecessor=None):
        self._set_session_ownership_locked(handle, role, item, predecessor=predecessor)
        self._connection.commit()

    def _demote_other_owners_locked(self, item, keep_handle):
        self._connection.execute(
            "UPDATE sessions SET role = NULL WHERE work_item = ? AND role = 'owner' AND handle != ?",
            (item, keep_handle),
        )

    def _claim_work_item_locked(
        self, handle, key, role, predecessor=None, branch=None, require_existing_binding=False,
    ):
        current = self.get_session(handle)
        if current is None:
            raise ValueError(f"{handle} is not a registered current native session")
        if require_existing_binding and current.work_item not in (None, key):
            raise ValueError(
                f"{handle} is attached to {current.work_item}; bind the remote reference explicitly before claiming"
            )
        removed = None
        if role == "owner":
            others = [session for session in self.live_owners(key) if session.handle != handle]
            if others:
                other = others[0]
                if predecessor and predecessor == other.handle:
                    self._end_session_locked(predecessor, "handed_off", successor=handle)
                    removed = predecessor
                else:
                    raise ValueError(
                        f"{other.handle} already owns this work item; hand off before taking ownership"
                    )
            self._demote_other_owners_locked(key, handle)
        self._set_session_ownership_locked(handle, role, key, predecessor=predecessor)
        if role == "owner" and branch:
            self._connection.execute(
                "UPDATE work_items SET branch = COALESCE(branch, ?), updated_at = ? WHERE key = ?",
                (branch, time.time(), key),
            )
        return removed

    def claim_work_item(self, handle, key, role, predecessor=None, branch=None, require_existing_binding=False):
        with _immediate_transaction(self._connection):
            return self._claim_work_item_locked(
                handle, key, role, predecessor=predecessor, branch=branch,
                require_existing_binding=require_existing_binding,
            )

    def claim_registered_work_item(
        self, host, session_id, cwd, key, work_item_repo, current_repo, feature, item, role,
        status=None, branch=None, session_branch=None, head=None, title=None, plan_path=None,
        predecessor=None, registration=None, require_existing_binding=False,
    ):
        handle = session_handle(host, session_id)
        with _immediate_transaction(self._connection):
            session = self.get_session(handle)
            if session is None:
                if registration is None:
                    raise ValueError(f"{handle} is not a registered current native session")
                self._start_session_locked(
                    host, session_id, cwd, title=registration.get("title"),
                    pid=registration.get("pid"), pid_started_at=registration.get("pid_started_at"),
                    repo=current_repo, branch=session_branch, head=head,
                )
            elif session.ended:
                raise ValueError(f"{handle} is not a current native session")
            existing = self.get_work_item(key)
            if title is not None and (not isinstance(title, str) or not title.strip()):
                raise ValueError("work item title must be non-empty")
            if existing is None and title is None:
                raise ValueError("a non-empty --title is required when creating a work item")
            item_branch = branch if existing is None or role == "owner" else None
            self._upsert_work_item_locked(
                key, work_item_repo, feature, item, status=status, branch=item_branch,
                title=title, plan_path=plan_path,
            )
            return self._claim_work_item_locked(
                handle, key, role, predecessor=predecessor, branch=branch,
                require_existing_binding=require_existing_binding,
            )


def _read_json_stdin():
    try:
        raw = sys.stdin.read()
    except (OSError, ValueError):
        return {}
    try:
        value = json.loads(raw) if raw.strip() else {}
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def run_hook(event, host):
    try:
        payload = _read_json_stdin()
        session_id = payload.get("session_id") or payload.get("sessionId")
        if not isinstance(session_id, str) or not session_id:
            return 0
        cwd = payload.get("cwd") or os.getcwd()
        handle = session_handle(host, session_id)
        with SessionStore() as store:
            if event == "session-start":
                store.start_session(host, session_id, cwd)
            elif event == "prompt":
                store.record_prompt(handle, payload.get("prompt"))
            elif event == "stop":
                store.record_stop(handle, cwd, follow=lambda checkout: follow_registered_cwd(host, session_id, checkout))
            elif event == "session-end":
                store.end_session(handle, "closed")
    except Exception:
        pass
    return 0


def _build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    hook_parser = subparsers.add_parser("hook")
    hook_parser.add_argument("event", choices=("session-start", "prompt", "stop", "session-end"))
    hook_parser.add_argument("--host", required=True, choices=("claude", "codex"))
    return parser


def main(argv=None):
    args = _build_parser().parse_args(argv)
    if args.command == "hook":
        return run_hook(args.event, args.host)
    return 0


if __name__ == "__main__":
    sys.exit(main())
