"""SQLite repository for sessions, work items and their remote references (Python 3.9+)."""

import contextlib
from dataclasses import fields
import hashlib
import importlib.util
import os
from pathlib import Path
import socket
import sqlite3
import sys
import time

_LOADER_PATH = Path(__file__).resolve().with_name("agent_store_loader.py")
_LOADER_KEY = f"agent_store_loader_{hashlib.sha256(str(_LOADER_PATH).encode('utf-8')).hexdigest()[:16]}"
if _LOADER_KEY not in sys.modules:
    _spec = importlib.util.spec_from_file_location(_LOADER_KEY, _LOADER_PATH)
    _loader = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(_loader)
    sys.modules[_LOADER_KEY] = _loader
load_sibling = sys.modules[_LOADER_KEY].load_sibling

_checkout = load_sibling(__file__, "checkout")
_session = load_sibling(__file__, "session")
_work_item = load_sibling(__file__, "work_item")

Checkout = _checkout.Checkout
GIT_TIMEOUT = _checkout.GIT_TIMEOUT
HOOK_GIT_TIMEOUT = _checkout.HOOK_GIT_TIMEOUT
git = _checkout.git
identify_repository = _checkout.identify_repository
ClientRegistration = _session.ClientRegistration
Session = _session.Session
OWNER = _session.OWNER
session_handle = _session.session_handle
prompt_title = _session.prompt_title
WorkItem = _work_item.WorkItem
WorkItemReference = _work_item.WorkItemReference
work_item_key = _work_item.work_item_key
validate_execution_key = _work_item.validate_execution_key
normalise_work_item_reference = _work_item.normalise_work_item_reference

__all__ = [
    "Checkout", "ClientRegistration", "GIT_TIMEOUT", "HOOK_GIT_TIMEOUT", "OWNER", "SCHEMA_VERSION", "Session",
    "SessionStore", "WorkItem", "WorkItemReference", "current_machine", "database_path", "git",
    "identify_repository", "normalise_work_item_reference", "profile_directory", "prompt_title", "session_handle",
    "state_directory", "validate_execution_key", "work_item_key",
]

SCHEMA_VERSION = 4
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


def _columns(record_type):
    return tuple(field.name for field in fields(record_type))


def _upsert_statement(table, columns, key):
    assignments = ", ".join(f"{column} = excluded.{column}" for column in columns if column != key)
    return (
        f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({', '.join('?' for _ in columns)}) "
        f"ON CONFLICT({key}) DO UPDATE SET {assignments}"
    )


_SESSION_COLUMNS = _columns(Session)
_WORK_ITEM_COLUMNS = _columns(WorkItem)
_REFERENCE_COLUMNS = _columns(WorkItemReference)
_SAVE_SESSION = _upsert_statement("sessions", _SESSION_COLUMNS, "handle")
_SAVE_WORK_ITEM = _upsert_statement("work_items", _WORK_ITEM_COLUMNS, "key")


def _from_row(record_type, row, prefix=""):
    if row is None:
        return None
    return record_type(**{name: row[f"{prefix}{name}"] for name in _columns(record_type)})


def _values(record, columns):
    return tuple(getattr(record, column) for column in columns)


def state_directory():
    override = os.environ.get("AGENT_STATE_DIRECTORY")
    return Path(override) if override else Path.home() / ".agents-state"


def database_path():
    return state_directory() / "sessions.db"


def current_machine():
    return socket.gethostname()


def profile_directory(host):
    if host == "claude":
        value = os.environ.get("CLAUDE_CONFIG_DIR") or str(Path.home() / ".claude")
    else:
        value = os.environ.get("CODEX_HOME") or str(Path.home() / ".codex")
    return str(Path(value))


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

    @contextlib.contextmanager
    def transaction(self):
        if self._connection.in_transaction:
            yield
            return
        with _immediate_transaction(self._connection):
            yield

    def _sessions(self, clause="", arguments=()):
        rows = self._connection.execute(f"SELECT * FROM sessions {clause}", arguments).fetchall()
        return [_from_row(Session, row) for row in rows]

    def get_session(self, handle):
        row = self._connection.execute("SELECT * FROM sessions WHERE handle = ?", (handle,)).fetchone()
        return _from_row(Session, row)

    def sessions_for_work_item(self, key):
        return self._sessions("WHERE work_item = ? ORDER BY started_at", (key,))

    def sessions_with_work_item(self):
        return self._sessions("WHERE work_item IS NOT NULL")

    def live_owners(self, key):
        return [session for session in self.sessions_for_work_item(key) if session.is_live_owner]

    def sessions_with_work_items_on_hosts(self, hosts, session_id=None):
        placeholders = ", ".join("?" for _ in hosts)
        exact = " AND s.session_id = ?" if session_id else ""
        selected = ", ".join(
            [f"s.{column}" for column in _SESSION_COLUMNS]
            + [f"w.{column} AS work_item_{column}" for column in _WORK_ITEM_COLUMNS]
        )
        rows = self._connection.execute(
            f"SELECT {selected} FROM sessions s LEFT JOIN work_items w ON w.key = s.work_item "
            f"WHERE s.host IN ({placeholders}){exact}",
            (*hosts, *((session_id,) if session_id else ())),
        ).fetchall()
        return [
            (
                _from_row(Session, row),
                None if row["work_item_key"] is None else _from_row(WorkItem, row, prefix="work_item_"),
            )
            for row in rows
        ]

    def interrupted_candidates(self, repo=None):
        if repo:
            return self._sessions("WHERE ended_at IS NULL AND repo = ?", (repo,))
        return self._sessions("WHERE ended_at IS NULL")

    def get_work_item(self, key):
        row = self._connection.execute("SELECT * FROM work_items WHERE key = ?", (key,)).fetchone()
        return _from_row(WorkItem, row)

    def _work_items_on_branch(self, repo, branch):
        rows = self._connection.execute(
            "SELECT * FROM work_items WHERE repo = ? AND branch = ?", (repo, branch),
        ).fetchall()
        return [_from_row(WorkItem, row) for row in rows]

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

    def _save_session(self, session):
        self._connection.execute(_SAVE_SESSION, _values(session, _SESSION_COLUMNS))

    def _save_work_item(self, item):
        self._connection.execute(_SAVE_WORK_ITEM, _values(item, _WORK_ITEM_COLUMNS))

    def _add_reference(self, reference):
        self._connection.execute(
            f"INSERT INTO work_item_references ({', '.join(_REFERENCE_COLUMNS)}) VALUES (?, ?, ?, ?)",
            _values(reference, _REFERENCE_COLUMNS),
        )

    def update_session(self, handle, change):
        with self.transaction():
            session = self.get_session(handle)
            if session is None:
                return None
            change(session)
            self._save_session(session)
            return session

    def delete_session(self, session_id):
        with self.transaction():
            self._connection.execute("DELETE FROM sessions WHERE session_id = ?", (session_id,))

    def start_session(self, host, session_id, cwd=None, checkout=None, title=None, pid=None, pid_started_at=None):
        handle = session_handle(host, session_id)
        with self.transaction():
            session = self.get_session(handle)
            machine, profile = current_machine(), profile_directory(host)
            if session is None:
                session = Session.start(
                    host, session_id, machine, profile, cwd=cwd, checkout=checkout, title=title,
                    pid=pid, pid_started_at=pid_started_at,
                )
            else:
                session.resume(
                    machine, profile, cwd=cwd, checkout=checkout, title=title, pid=pid, pid_started_at=pid_started_at,
                )
            self._save_session(session)
            if checkout is not None and checkout.on_branch:
                self._attach_branch_work_item(session, checkout.repo, checkout.branch, create=False)
        return handle

    def record_stop(self, handle, checkout=None):
        with self.transaction():
            session = self.update_session(handle, lambda current: current.stopped(checkout))
            if session is not None and checkout is not None and checkout.on_branch:
                self._attach_branch_work_item(session, checkout.repo, checkout.branch, create=not checkout.is_default)

    def _attach_branch_work_item(self, session, repo, branch, create):
        if not session.unattached:
            return
        matches = [item for item in self._work_items_on_branch(repo, branch) if item.is_open]
        if len(matches) == 1:
            key = matches[0].key
        elif not matches and create:
            item = WorkItem.for_branch(repo, branch, title=session.title or branch)
            self._save_work_item(item)
            key = item.key
        else:
            return
        session.attach_work_item(key)
        self._save_session(session)

    def _absorbable_branch_items(self, repo, branch, keep=None):
        return [
            item for item in self._work_items_on_branch(repo, branch)
            if item.key != keep and item.can_be_absorbed(
                referenced=bool(self.work_item_references(item.key)),
                claimed=any(session.claimed for session in self.sessions_for_work_item(item.key)),
            )
        ]

    def _absorb_branch_work_items(self, key):
        item = self.get_work_item(key)
        if item is None or not item.absorbs_branch_items:
            return
        for absorbed in self._absorbable_branch_items(item.repo, item.branch, keep=key):
            for session in self.sessions_for_work_item(absorbed.key):
                session.transfer_work_item(key)
                self._save_session(session)
            self._connection.execute("DELETE FROM work_items WHERE key = ?", (absorbed.key,))

    def _put_work_item(self, key, repo, feature, item, status=None, branch=None, pr=None, title=None, plan_path=None):
        existing = self.get_work_item(key)
        if existing is None:
            self._save_work_item(WorkItem.create(
                key, repo, feature, item, status=status, branch=branch, pr=pr, title=title, plan_path=plan_path,
            ))
            return
        WorkItem.require_identity(key, feature, item)
        existing.revise(status=status, branch=branch, pr=pr, title=title, plan_path=plan_path)
        self._save_work_item(existing)

    def upsert_work_item(self, key, repo, feature, item, status=None, branch=None, pr=None, title=None,
                         plan_path=None):
        with self.transaction():
            self._put_work_item(
                key, repo, feature, item, status=status, branch=branch, pr=pr, title=title, plan_path=plan_path,
            )
            self._absorb_branch_work_items(key)

    def _bind_reference(self, key, provider, scope, native_id):
        reference = WorkItemReference.bind(key, provider, scope, native_id)
        existing = self.get_work_item_reference(reference.provider, reference.scope, reference.native_id)
        if existing is not None:
            return existing.rebind(key)
        if self.get_work_item(key) is None:
            raise ValueError(f"work item does not exist: {key}")
        self._add_reference(reference)
        return True

    def bind_work_item_reference(self, key, provider, scope, native_id):
        with self.transaction():
            return self._bind_reference(key, provider, scope, native_id)

    def resolve_or_create_work_item_reference(self, provider, scope, native_id, repo, branch=None):
        provider, scope, native_id = normalise_work_item_reference(provider, scope, native_id)
        with self.transaction():
            reference = self.get_work_item_reference(provider, scope, native_id)
            if reference is not None:
                item = self.get_work_item(reference.work_item)
                if item is None:
                    raise RuntimeError(f"reference {provider}/{scope}/{native_id} has a missing work item")
                return item, False
            adoptable = self._absorbable_branch_items(repo, branch) if branch else []
            if len(adoptable) == 1:
                item = adoptable[0]
                created = False
            else:
                item = WorkItem.for_branch(repo, branch)
                self._save_work_item(item)
                created = True
            self._bind_reference(item.key, provider, scope, native_id)
            return self.get_work_item(item.key), created

    def claim_work_item(self, handle, key, role, predecessor=None, branch=None, require_existing_binding=False):
        with self.transaction():
            session = self.get_session(handle)
            if session is None:
                raise ValueError(f"{handle} is not a registered current native session")
            return self._claim(session, key, role, predecessor, branch, require_existing_binding)

    def _claim(self, session, key, role, predecessor, branch, require_existing_binding):
        session.ensure_may_claim(key, require_existing_binding)
        removed = None
        if role == OWNER:
            holder = next((other for other in self.live_owners(key) if other != session), None)
            if holder is not None:
                holder.hand_off(session.handle, predecessor)
                self._save_session(holder)
                removed = holder.handle
            for other in self.sessions_for_work_item(key):
                if other.is_owner and other != session:
                    other.demote()
                    self._save_session(other)
        session.claim(key, role, predecessor)
        self._save_session(session)
        if role == OWNER and branch:
            item = self.get_work_item(key)
            if item is not None:
                item.record_branch(branch)
                self._save_work_item(item)
        if role == OWNER:
            self._absorb_branch_work_items(key)
        return removed

    def claim_registered_work_item(
        self, host, session_id, cwd, checkout, key, work_item_repo, feature, item, role,
        status=None, branch=None, title=None, plan_path=None, predecessor=None, registration=None,
        require_existing_binding=False,
    ):
        handle = session_handle(host, session_id)
        with self.transaction():
            session = self.get_session(handle)
            if session is None:
                if registration is None:
                    raise ValueError(f"{handle} is not a registered current native session")
                session = Session.start(
                    host, session_id, current_machine(), profile_directory(host), cwd=cwd, checkout=checkout,
                    title=registration.title, pid=registration.pid, pid_started_at=registration.pid_started_at,
                )
                self._save_session(session)
            else:
                session.ensure_current()
            existing = self.get_work_item(key)
            if title is not None and (not isinstance(title, str) or not title.strip()):
                raise ValueError("work item title must be non-empty")
            if existing is None and title is None:
                raise ValueError("a non-empty --title is required when creating a work item")
            item_branch = branch if existing is None or role == OWNER else None
            self._put_work_item(
                key, work_item_repo, feature, item, status=status, branch=item_branch, title=title,
                plan_path=plan_path,
            )
            return self._claim(session, key, role, predecessor, branch, require_existing_binding)
