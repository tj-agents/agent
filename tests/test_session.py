import importlib.util
from pathlib import Path
import unittest


RUNTIME = Path(__file__).resolve().parents[1] / ".agents/agent-store/runtime"


def _load(name):
    spec = importlib.util.spec_from_file_location(f"test_session_{name}", RUNTIME / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


SESSION = _load("session")
CHECKOUT = _load("checkout")
Session = SESSION.Session
Checkout = CHECKOUT.Checkout


def started(host="claude", session_id="one", **changes):
    session = Session.start(host, session_id, "machine", "profile", cwd="/work",
                            checkout=Checkout("/work", "github.com/o/r", "topic", "abc", False))
    for name, value in changes.items():
        setattr(session, name, value)
    return session


class PromptTitleTests(unittest.TestCase):
    def test_commands_markup_and_blank_prompts_have_no_title(self):
        for prompt in (None, 3, "   ", "/recover", "<command>x</command>"):
            with self.subTest(prompt=prompt):
                self.assertIsNone(SESSION.prompt_title(prompt))

    def test_long_prompts_are_cut_at_a_word_boundary(self):
        title = SESSION.prompt_title("word " * 40)
        self.assertLessEqual(len(title), SESSION.PROMPT_TITLE_LENGTH + 1)
        self.assertTrue(title.endswith("word…"))


class SessionLifecycleTests(unittest.TestCase):
    def test_start_records_the_checkout_and_a_working_turn(self):
        session = started()
        self.assertEqual(("claude:one", "/work", "github.com/o/r", "topic", "abc", "working"),
                         (session.handle, session.cwd, session.repo, session.branch, session.head,
                          session.turn_state))
        self.assertEqual(session.started_at, session.last_activity_at)
        self.assertFalse(session.ended)

    def test_the_first_titled_prompt_names_the_session_once(self):
        session = started()
        session.prompted("/clear")
        self.assertIsNone(session.title)
        session.prompted("  Fix the\n crash ")
        session.prompted("something else")
        self.assertEqual("Fix the crash", session.title)
        self.assertEqual("working", session.turn_state)

    def test_stop_awaits_input_and_keeps_what_the_checkout_does_not_know(self):
        session = started()
        session.stopped(Checkout("/moved", None, "other", None, False))
        self.assertEqual(("awaiting_input", "/moved", "github.com/o/r", "other", "abc"),
                         (session.turn_state, session.cwd, session.repo, session.branch, session.head))
        session.stopped(None)
        self.assertEqual("/moved", session.cwd)

    def test_the_first_end_reason_and_successor_win(self):
        session = started()
        session.end("finished")
        ended_at = session.ended_at
        session.end("closed", successor="codex:two")
        self.assertEqual(("finished", "codex:two", ended_at), (session.end_reason, session.successor, session.ended_at))
        session.end("closed", successor="codex:three")
        self.assertEqual("codex:two", session.successor)

    def test_reopen_clears_only_the_matching_end_reason(self):
        session = started()
        session.end("finished")
        self.assertFalse(session.reopen("closed"))
        self.assertFalse(session.reopen(None))
        self.assertEqual("finished", session.end_reason)
        self.assertTrue(session.reopen("finished"))
        self.assertIsNone(session.ended_at)
        self.assertIsNone(session.end_reason)

    def test_resume_after_a_handoff_drops_the_role_but_keeps_the_work_item(self):
        session = started(role="owner", work_item="key")
        session.end("handed_off", successor="codex:two")
        session.resume("machine", "profile", cwd="/work", title=None)
        self.assertEqual((None, "key", None, None, "codex:two"),
                         (session.role, session.work_item, session.ended_at, session.end_reason, session.successor))

    def test_resume_after_a_close_keeps_the_role_and_an_existing_title(self):
        session = started(role="owner", title="Named")
        session.end("closed")
        session.resume("machine", "profile", cwd="/work")
        self.assertEqual(("owner", "Named", None), (session.role, session.title, session.repo))

    def test_an_ended_session_ignores_a_client_attachment(self):
        session = started(pid=1, pid_started_at=1.0)
        session.attach_client(2, 2.0, "/client")
        self.assertEqual((2, 2.0, "/client"), (session.pid, session.pid_started_at, session.cwd))
        session.end("closed")
        session.attach_client(3, 3.0, "/late")
        self.assertEqual((2, "/client"), (session.pid, session.cwd))


class SessionOwnershipTests(unittest.TestCase):
    def test_a_live_owner_hands_off_only_to_a_claimant_naming_it(self):
        owner = started(role="owner", work_item="key")
        self.assertTrue(owner.is_live_owner)
        for named in (None, "claude:other"):
            with self.subTest(named=named):
                with self.assertRaisesRegex(ValueError, "already owns this work item"):
                    owner.hand_off("codex:two", named)
                self.assertFalse(owner.ended)
        owner.hand_off("codex:two", "claude:one")
        self.assertEqual(("handed_off", "codex:two"), (owner.end_reason, owner.successor))
        self.assertFalse(owner.is_live_owner)

    def test_claim_sets_role_and_item_and_keeps_an_earlier_predecessor(self):
        session = started()
        session.claim("key", "owner", predecessor="claude:zero")
        session.claim("key", "bounded: docs")
        self.assertEqual(("bounded: docs", "key", "claude:zero"),
                         (session.role, session.work_item, session.predecessor))
        self.assertTrue(session.claimed)
        self.assertFalse(session.is_owner)
        session.demote()
        self.assertFalse(session.claimed)

    def test_reference_claims_refuse_a_session_attached_elsewhere(self):
        session = started(work_item="other")
        session.ensure_may_claim("key")
        session.ensure_may_claim("other", require_existing_binding=True)
        with self.assertRaisesRegex(ValueError, "attached to other"):
            session.ensure_may_claim("key", require_existing_binding=True)

    def test_registered_claims_need_a_current_session(self):
        session = started()
        session.ensure_current()
        session.end("closed")
        with self.assertRaisesRegex(ValueError, "not a current native session"):
            session.ensure_current()

    def test_only_an_unattached_session_takes_a_branch_work_item(self):
        session = started()
        self.assertTrue(session.attach_work_item("first"))
        self.assertFalse(session.attach_work_item("second"))
        self.assertEqual("first", session.work_item)
        session.transfer_work_item("named")
        self.assertEqual("named", session.work_item)


class CheckoutTests(unittest.TestCase):
    def test_only_a_known_repository_branch_identifies_branch_work(self):
        self.assertTrue(Checkout("/r", "repo", "topic", None, False).on_branch)
        self.assertFalse(Checkout("/r", None, "topic", None, False).on_branch)
        self.assertFalse(Checkout("/r", "repo", None, None, False).on_branch)


if __name__ == "__main__":
    unittest.main()
