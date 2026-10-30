import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SOURCE_RUNTIME = ROOT / ".agents/agent-store/runtime"
RUNTIMES = (
    SOURCE_RUNTIME,
    ROOT / "plugins/agent/.agents/agent-store/runtime",
    ROOT / "plugins/agent-store/.agents/agent-store/runtime",
)
sys.path.insert(0, str(SOURCE_RUNTIME))

import tj_agent_store  # noqa: E402

PUBLIC_API = [
    "GIT_TIMEOUT", "HOOK_GIT_TIMEOUT", "OWNER", "SCHEMA_VERSION", "SESSIONS_ONE_OWNER_INDEX", "SESSIONS_SCHEMA",
    "WORK_ITEMS_SCHEMA", "WORK_ITEM_REFERENCES_ITEM_INDEX", "WORK_ITEM_REFERENCES_SCHEMA", "WORK_ITEM_STATUSES",
    "Checkout", "CliSessionRegistry", "ClientRegistration", "ReadOnlyWorkItemStore", "Session", "SessionStore",
    "StoreReadError", "WorkItem", "WorkItemReference", "current_machine", "database_path", "git",
    "identify_repository", "normalise_remote", "normalise_work_item_reference", "profile_directory", "prompt_title",
    "repository_identity", "session_handle", "state_directory", "validate_execution_key", "work_item_key",
]
CORE_SIGNATURES = {
    "repository_identity": "(cwd, git)",
    "identify_repository": "(cwd, timeout=3)",
    "work_item_key": "(repo, feature, item)",
    "ReadOnlyWorkItemStore": "(database=None)",
    "ReadOnlyWorkItemStore.work_item": "(self, key)",
    "ReadOnlyWorkItemStore.owner": "(self, key)",
    "SessionStore": "(path=None)",
    "SessionStore.upsert_work_item": (
        "(self, key, repo, feature, item, status=None, branch=None, pr=None, title=None, plan_path=None)"
    ),
}
SURFACE = """
import inspect, json, sys
import tj_agent_store
signatures = {}
for name in sys.argv[1:]:
    owner, _, member = name.partition(".")
    value = getattr(tj_agent_store, owner)
    signatures[name] = str(inspect.signature(getattr(value, member) if member else value))
print(json.dumps({"all": tj_agent_store.__all__, "file": tj_agent_store.__file__, "signatures": signatures}))
"""


class AgentStorePackageTests(unittest.TestCase):
    def test_the_public_api_is_pinned_and_resolves(self):
        self.assertEqual(PUBLIC_API, tj_agent_store.__all__)
        for name in PUBLIC_API:
            self.assertTrue(hasattr(tj_agent_store, name), name)

    def test_every_shipped_runtime_imports_the_package_by_name_with_the_core_signatures(self):
        for runtime in RUNTIMES:
            with self.subTest(runtime=runtime):
                result = subprocess.run(
                    [sys.executable, "-I", "-B", "-c", f"import sys; sys.path.insert(0, {str(runtime)!r})\n{SURFACE}",
                     *CORE_SIGNATURES],
                    capture_output=True, text=True, timeout=30,
                )
                self.assertEqual(0, result.returncode, result.stderr)
                surface = json.loads(result.stdout)
                self.assertEqual(PUBLIC_API, surface["all"])
                self.assertEqual(runtime / "tj_agent_store" / "__init__.py", Path(surface["file"]))
                self.assertEqual(CORE_SIGNATURES, surface["signatures"])

    def test_agent_runtime_consumers_import_the_store_from_their_own_bundle(self):
        bundle = ROOT / "plugins/agent"
        runtime = bundle / ".agents/agent/runtime"
        for consumer in ("work_item_sessions", "agent_recovery"):
            with self.subTest(consumer=consumer):
                result = subprocess.run(
                    [sys.executable, "-I", "-B", "-c",
                     f"import sys; sys.path.insert(0, {str(runtime)!r}); import {consumer}; "
                     f"print({consumer}.tj_agent_store.__file__)"],
                    capture_output=True, text=True, timeout=30,
                )
                self.assertEqual(0, result.returncode, result.stderr)
                self.assertEqual(bundle / ".agents/agent-store/runtime/tj_agent_store/__init__.py",
                                 Path(result.stdout.strip()))

    def test_repository_identity_takes_a_directory_and_a_git_callable(self):
        calls = []

        def git(directory, *arguments):
            calls.append((directory, arguments))
            return "git@github.com:Owner/Repo.git"

        self.assertEqual("github.com/owner/repo", tj_agent_store.repository_identity("/work", git))
        self.assertEqual([("/work", ("config", "--get", "remote.origin.url"))], calls)
        self.assertEqual("github.com/owner/repo", tj_agent_store.normalise_remote("https://github.com/owner/repo.git"))

    def test_the_reader_refuses_a_missing_store_with_its_own_error(self):
        self.assertTrue(issubclass(tj_agent_store.StoreReadError, Exception))
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(tj_agent_store.StoreReadError):
                tj_agent_store.ReadOnlyWorkItemStore(Path(directory) / "missing.db").work_item("key")

    def test_the_schema_definitions_create_the_store_tables(self):
        for name in ("WORK_ITEMS_SCHEMA", "SESSIONS_SCHEMA", "WORK_ITEM_REFERENCES_SCHEMA"):
            self.assertIn("CREATE TABLE IF NOT EXISTS", getattr(tj_agent_store, name))
        self.assertEqual(4, tj_agent_store.SCHEMA_VERSION)


if __name__ == "__main__":
    unittest.main()
