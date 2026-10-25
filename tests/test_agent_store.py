import importlib.util
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
INSPECT = ROOT / ".agents/agent-store/utility/inspect/scripts/inspect.py"
READER = ROOT / ".agents/agent-store/runtime/reader.py"


def load_reader():
    spec = importlib.util.spec_from_file_location("agent_store_reader", READER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class InspectTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.database = Path(self.temporary.name) / "sessions.db"
        connection = sqlite3.connect(self.database)
        connection.executescript(
            "CREATE TABLE work_items ("
            "key TEXT PRIMARY KEY, repo TEXT NOT NULL, feature TEXT, item TEXT, status TEXT NOT NULL, "
            "branch TEXT, pr TEXT, title TEXT, plan_path TEXT, updated_at REAL);"
            "CREATE TABLE sessions ("
            "handle TEXT PRIMARY KEY, host TEXT, session_id TEXT, machine TEXT, profile TEXT, title TEXT, "
            "cwd TEXT, repo TEXT, branch TEXT, head TEXT, work_item TEXT, role TEXT, pid INTEGER, "
            "pid_started_at REAL, started_at REAL, last_activity_at REAL, turn_state TEXT, ended_at REAL, "
            "end_reason TEXT, predecessor TEXT, successor TEXT);"
            "CREATE TABLE work_item_references ("
            "provider TEXT NOT NULL, scope TEXT NOT NULL, native_id TEXT NOT NULL, work_item TEXT NOT NULL, "
            "PRIMARY KEY (provider, scope, native_id));"
            "PRAGMA user_version = 4;"
        )
        connection.executemany(
            "INSERT INTO work_items VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                ("execution:11111111-1111-4111-8111-111111111111", "github.com/example/one", None, None, "active", None, None, None, None, 1.0),
                ("github.com/example/two:feature/item", "github.com/example/two", "feature", "item", "paused", None, None, None, None, 2.0),
            ],
        )
        connection.executemany(
            "INSERT INTO sessions (handle, repo, work_item) VALUES (?, ?, ?)",
            [
                ("codex:two", "github.com/example/one", "execution:11111111-1111-4111-8111-111111111111"),
                ("claude:one", "github.com/example/two", "github.com/example/two:feature/item"),
                ("claude:attached", "github.com/example/other", "execution:11111111-1111-4111-8111-111111111111"),
                ("codex:unbound", "github.com/example/one", None),
            ],
        )
        connection.executemany(
            "INSERT INTO work_item_references VALUES (?, ?, ?, ?)",
            [
                ("github", "github.com/example/one", "2", "execution:11111111-1111-4111-8111-111111111111"),
                ("github", "github.com/example/one", "1", "execution:11111111-1111-4111-8111-111111111111"),
            ],
        )
        connection.commit()
        connection.close()

    def inspect(self, *arguments):
        return subprocess.run(
            [sys.executable, "-B", str(INSPECT), "--database", str(self.database), *arguments],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=15,
        )

    def test_lists_recorded_state_without_writing_and_sorts_every_collection(self):
        before = self.database.read_bytes()
        result = self.inspect()
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("", result.stderr)
        value = json.loads(result.stdout)
        self.assertEqual(
            {"schema_version", "state_kind", "liveness", "work_items", "sessions", "references"},
            set(value),
        )
        self.assertEqual("recorded", value["state_kind"])
        self.assertEqual("not-observed", value["liveness"])
        self.assertEqual(["execution:11111111-1111-4111-8111-111111111111", "github.com/example/two:feature/item"], [row["key"] for row in value["work_items"]])
        self.assertEqual(["claude:attached", "claude:one", "codex:two", "codex:unbound"], [row["handle"] for row in value["sessions"]])
        self.assertEqual(["1", "2"], [row["native_id"] for row in value["references"]])
        self.assertEqual(before, self.database.read_bytes())

    def test_selectors_scope_rows_and_missing_execution_fails_without_stdout(self):
        key = "execution:11111111-1111-4111-8111-111111111111"
        selected = self.inspect("--execution-key", key)
        self.assertEqual(0, selected.returncode, selected.stderr)
        value = json.loads(selected.stdout)
        self.assertEqual([key], [row["key"] for row in value["work_items"]])
        self.assertEqual(["claude:attached", "codex:two"], [row["handle"] for row in value["sessions"]])
        repository = self.inspect("--repository", "github.com/example/one")
        self.assertEqual(0, repository.returncode, repository.stderr)
        value = json.loads(repository.stdout)
        self.assertEqual([key], [row["key"] for row in value["work_items"]])
        self.assertEqual(["claude:attached", "codex:two", "codex:unbound"], [row["handle"] for row in value["sessions"]])
        missing = self.inspect("--execution-key", "execution:00000000-0000-4000-8000-000000000000")
        self.assertEqual(1, missing.returncode)
        self.assertEqual("", missing.stdout)
        self.assertIn("not registered", json.loads(missing.stderr)["error"])
        invalid = self.inspect("--execution-key", key, "--repository", "github.com/example/one")
        self.assertEqual(2, invalid.returncode)

    def test_missing_and_unsupported_stores_fail_without_creating_or_migrating(self):
        self.database.unlink()
        missing = self.inspect()
        self.assertEqual(1, missing.returncode)
        self.assertFalse(self.database.exists())
        self.database.write_bytes(b"not sqlite")
        before = self.database.read_bytes()
        invalid = self.inspect()
        self.assertEqual(1, invalid.returncode)
        self.assertEqual("", invalid.stdout)
        self.assertIn("cannot be read", json.loads(invalid.stderr)["error"])
        self.assertEqual(before, self.database.read_bytes())
        self.database.unlink()
        connection = sqlite3.connect(self.database)
        connection.execute("PRAGMA user_version = 5")
        connection.commit()
        connection.close()
        unsupported = self.inspect()
        self.assertEqual(1, unsupported.returncode)
        self.assertIn("unsupported schema version 5", json.loads(unsupported.stderr)["error"])

    def test_inspection_uses_one_read_snapshot(self):
        connection = sqlite3.connect(self.database)
        connection.execute("PRAGMA journal_mode = WAL")
        connection.close()
        reader = load_reader().ReadOnlyWorkItemStore(self.database)
        references = reader._references

        def add_reference(connection, clause="", arguments=()):
            writer = sqlite3.connect(self.database)
            writer.execute(
                "INSERT INTO work_item_references VALUES (?, ?, ?, ?)",
                ("github", "github.com/example/one", "3", "execution:11111111-1111-4111-8111-111111111111"),
            )
            writer.commit()
            writer.close()
            return references(connection, clause, arguments)

        reader._references = add_reference
        value = reader.inspect()
        self.assertEqual(["1", "2"], [row["native_id"] for row in value["references"]])



if __name__ == "__main__":
    unittest.main()
