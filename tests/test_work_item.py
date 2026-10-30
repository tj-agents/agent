import importlib.util
from pathlib import Path
import unittest


RUNTIME = Path(__file__).resolve().parents[1] / ".agents/agent-store/runtime"
SPEC = importlib.util.spec_from_file_location("test_work_item_module", RUNTIME / "work_item.py")
WORK_ITEM = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(WORK_ITEM)
WorkItem = WORK_ITEM.WorkItem
WorkItemReference = WORK_ITEM.WorkItemReference


class WorkItemTests(unittest.TestCase):
    def test_create_defaults_to_active_and_checks_its_identity(self):
        item = WorkItem.create("repo:feature/item", "repo", "feature", "item")
        self.assertEqual(("active", "feature/item"), (item.status, item.label))
        self.assertIsNotNone(item.updated_at)
        with self.assertRaisesRegex(ValueError, "both be present or both be absent"):
            WorkItem.create("repo:feature/", "repo", "feature", None)
        with self.assertRaisesRegex(ValueError, "execution key"):
            WorkItem.create("not-an-execution-key", "repo")

    def test_a_branch_item_is_an_open_unnamed_execution(self):
        item = WorkItem.for_branch("repo", "Fix/Thing", title="Fix the thing")
        WORK_ITEM.validate_execution_key(item.key)
        self.assertEqual((item.key, "Fix/Thing", "Fix the thing"), (item.label, item.branch, item.title))
        self.assertTrue(item.is_open)
        self.assertFalse(item.is_named)

    def test_revise_keeps_unset_fields_and_sets_the_branch_once(self):
        item = WorkItem.create("repo:f/i", "repo", "f", "i", status="proposed", title="Old", pr="1")
        item.revise(branch="first")
        item.revise(status="active", branch="second", title="New")
        self.assertEqual(("active", "first", "1", "New"), (item.status, item.branch, item.pr, item.title))

    def test_only_an_open_unnamed_unreferenced_unclaimed_item_can_be_absorbed(self):
        automatic = WorkItem.for_branch("repo", "topic")
        self.assertTrue(automatic.can_be_absorbed(referenced=False, claimed=False))
        self.assertFalse(automatic.can_be_absorbed(referenced=True, claimed=False))
        self.assertFalse(automatic.can_be_absorbed(referenced=False, claimed=True))
        done = WorkItem.for_branch("repo", "topic")
        done.revise(status="done")
        self.assertFalse(done.can_be_absorbed(referenced=False, claimed=False))
        named = WorkItem.create("repo:f/i", "repo", "f", "i", branch="topic")
        self.assertFalse(named.can_be_absorbed(referenced=False, claimed=False))


class WorkItemReferenceTests(unittest.TestCase):
    def test_bind_normalises_and_rebinding_is_idempotent_only_for_the_same_item(self):
        reference = WorkItemReference.bind("key-a", "GitHub", " GitHub.com/Owner/Repo ", "42")
        self.assertEqual(WorkItemReference("github", "github.com/owner/repo", "42", "key-a"), reference)
        self.assertFalse(reference.rebind("key-a"))
        with self.assertRaisesRegex(ValueError, "already bound to key-a, not key-b"):
            reference.rebind("key-b")

    def test_bind_refuses_an_unknown_provider(self):
        with self.assertRaisesRegex(ValueError, "provider must be"):
            WorkItemReference.bind("key-a", "gitlab", "gitlab.com/owner/repo", "1")


if __name__ == "__main__":
    unittest.main()
