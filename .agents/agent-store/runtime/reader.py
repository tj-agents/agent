import os
import sqlite3
from pathlib import Path


WORK_ITEM_STATUSES = ("proposed", "active", "blocked", "paused", "delivery-gated", "done")
SCHEMA_VERSION = 4


class StoreReadError(ValueError):
    pass


class ReadOnlyWorkItemStore:
    def __init__(self, database=None):
        root = Path(os.environ.get("AGENT_STATE_DIRECTORY", Path.home() / ".agents-state"))
        self.database = Path(database) if database is not None else root / "sessions.db"

    def _connection(self):
        if not self.database.is_file():
            raise StoreReadError(f"work-item session store does not exist: {self.database}")
        connection = None
        try:
            connection = sqlite3.connect(f"{self.database.resolve().as_uri()}?mode=ro", uri=True)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA query_only = ON")
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version != SCHEMA_VERSION:
                raise StoreReadError(f"work-item session store has unsupported schema version {version}")
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
            }
            if not {"work_items", "sessions"}.issubset(tables):
                raise StoreReadError("work-item session store is missing required tables")
            required = {
                "work_items": {"key", "status", "pr"},
                "sessions": {"handle", "repo", "branch", "cwd", "work_item", "role", "ended_at"},
                "work_item_references": {"provider", "scope", "native_id", "work_item"},
            }
            for table, columns in required.items():
                available = {
                    row[1]
                    for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
                }
                missing = columns - available
                if missing:
                    raise StoreReadError(
                        f"work-item session store table {table} is missing required columns {sorted(missing)!r}"
                    )
            return connection
        except StoreReadError:
            if connection is not None:
                connection.close()
            raise
        except sqlite3.DatabaseError as error:
            if connection is not None:
                connection.close()
            raise StoreReadError(f"work-item session store cannot be read: {error}") from error

    def work_item(self, key):
        connection = self._connection()
        try:
            rows = connection.execute(
                "SELECT key, status, pr FROM work_items WHERE key = ?", (key,)
            ).fetchall()
            if not rows:
                raise StoreReadError(f"work item {key} is not registered in the session store")
            if len(rows) > 1:
                raise StoreReadError(f"work item {key} has conflicting registered rows")
            row = rows[0]
            value = dict(row)
            if value.get("status") not in WORK_ITEM_STATUSES:
                raise StoreReadError(f"work item {key} has unsupported status {value.get('status')!r}")
            return value
        except sqlite3.DatabaseError as error:
            raise StoreReadError(f"work-item session store query failed: {error}") from error
        finally:
            connection.close()

    def owner(self, key):
        connection = self._connection()
        try:
            rows = connection.execute(
                "SELECT handle, repo, branch, cwd FROM sessions "
                "WHERE work_item = ? AND role = 'owner' AND ended_at IS NULL",
                (key,),
            ).fetchall()
            if len(rows) > 1:
                raise StoreReadError(f"work item {key} has multiple active session owners")
            return dict(rows[0]) if rows else None
        except sqlite3.DatabaseError as error:
            raise StoreReadError(f"work-item session store query failed: {error}") from error
        finally:
            connection.close()

    def inspect(self, execution_key=None, repository=None):
        connection = self._connection()
        try:
            connection.execute("BEGIN")
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if execution_key is not None:
                work_items = connection.execute(
                    "SELECT * FROM work_items WHERE key = ? ORDER BY key", (execution_key,)
                ).fetchall()
                if not work_items:
                    raise StoreReadError(f"work item {execution_key} is not registered in the session store")
                sessions = connection.execute(
                    "SELECT * FROM sessions WHERE work_item = ? ORDER BY handle", (execution_key,)
                ).fetchall()
                references = self._references(connection, "WHERE work_item = ?", (execution_key,))
            elif repository is not None:
                work_items = connection.execute(
                    "SELECT * FROM work_items WHERE repo = ? ORDER BY key", (repository,)
                ).fetchall()
                keys = tuple(row["key"] for row in work_items)
                if keys:
                    placeholders = ", ".join("?" for _ in keys)
                    sessions = connection.execute(
                        f"SELECT * FROM sessions WHERE repo = ? OR work_item IN ({placeholders}) ORDER BY handle",
                        (repository, *keys),
                    ).fetchall()
                    references = self._references(
                        connection, f"WHERE work_item IN ({placeholders})", keys,
                    )
                else:
                    sessions = connection.execute(
                        "SELECT * FROM sessions WHERE repo = ? ORDER BY handle", (repository,)
                    ).fetchall()
                    references = []
            else:
                work_items = connection.execute("SELECT * FROM work_items ORDER BY key").fetchall()
                sessions = connection.execute("SELECT * FROM sessions ORDER BY handle").fetchall()
                references = self._references(connection)
            return {
                "schema_version": version,
                "state_kind": "recorded",
                "liveness": "not-observed",
                "work_items": [dict(row) for row in work_items],
                "sessions": [dict(row) for row in sessions],
                "references": [dict(row) for row in references],
            }
        except sqlite3.DatabaseError as error:
            raise StoreReadError(f"work-item session store query failed: {error}") from error
        finally:
            connection.close()

    @staticmethod
    def _references(connection, clause="", arguments=()):
        return connection.execute(
            "SELECT provider, scope, native_id, work_item FROM work_item_references "
            f"{clause} ORDER BY provider, scope, native_id",
            arguments,
        ).fetchall()
