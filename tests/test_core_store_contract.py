import importlib.util
import inspect
from pathlib import Path
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
RUNTIMES = (
    ROOT / ".agents/agent-store/runtime",
    ROOT / "plugins/agent/.agents/agent-store/runtime",
    ROOT / "plugins/agent-store/.agents/agent-store/runtime",
)


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class CoreStoreContractTests(unittest.TestCase):
    def test_repository_identity_takes_a_directory_and_a_git_callable(self):
        for runtime in RUNTIMES:
            with self.subTest(runtime=runtime):
                identity = load(runtime / "repository_identity.py", "core_contract_repository_identity")
                self.assertEqual(["cwd", "git"], list(inspect.signature(identity.repository_identity).parameters))
                calls = []

                def git(directory, *arguments):
                    calls.append((directory, arguments))
                    return "git@github.com:Owner/Repo.git"

                self.assertEqual("github.com/owner/repo", identity.repository_identity("/work", git))
                self.assertEqual([("/work", ("config", "--get", "remote.origin.url"))], calls)
                self.assertEqual("github.com/owner/repo", identity.normalise_remote("https://github.com/owner/repo.git"))

    def test_reader_exposes_the_read_only_work_item_store(self):
        for runtime in RUNTIMES:
            with self.subTest(runtime=runtime):
                reader = load(runtime / "reader.py", "core_contract_reader")
                self.assertTrue(issubclass(reader.StoreReadError, Exception))
                self.assertEqual(["database"], list(inspect.signature(reader.ReadOnlyWorkItemStore).parameters))
                self.assertEqual(["self", "key"], list(inspect.signature(reader.ReadOnlyWorkItemStore.work_item).parameters))
                with tempfile.TemporaryDirectory() as directory:
                    with self.assertRaises(reader.StoreReadError):
                        reader.ReadOnlyWorkItemStore(Path(directory) / "missing.db").work_item("key")

    def test_session_store_keeps_its_schema_definitions(self):
        for runtime in RUNTIMES:
            with self.subTest(runtime=runtime):
                store = load(runtime / "session_store.py", "core_contract_session_store")
                for name in ("WORK_ITEMS_SCHEMA", "SESSIONS_SCHEMA", "WORK_ITEM_REFERENCES_SCHEMA"):
                    self.assertIn("CREATE TABLE IF NOT EXISTS", getattr(store, name))


if __name__ == "__main__":
    unittest.main()
