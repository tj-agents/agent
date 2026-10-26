"""Verified native rollout completion cannot be inferred from hook state."""

import json
from pathlib import Path
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / '.agents/agent/runtime'))
import cli_client


class NativeCompletionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='agent native completion ')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.profile = self.root / 'profile'
        self.rollout = self.profile / 'sessions' / 'rollout.jsonl'
        self.rollout.parent.mkdir(parents=True)
        self.recording = self.root / 'events.jsonl'
        self.recording.write_text(json.dumps({'dir': 'meta', 'kind': 'session_start'}) + '\n' + json.dumps({
            'dir': 'from_tui', 'kind': 'op', 'payload': {'UserTurn': {'client_user_message_id': 'input'}},
        }) + '\n', encoding='utf-8')
        self.entry = {
            'host': 'codex', 'session_id': 'thread', 'client_message_id': 'input', 'native_turn_id': 'turn',
            'native_rollout': str(self.rollout), 'profile': str(self.profile), 'recording_path': str(self.recording),
        }
        self.append({'type': 'session_meta', 'payload': {'id': 'thread', 'source': 'cli'}})
        self.event('task_started')
        self.event('item_completed', item={'type': 'UserMessage', 'client_id': 'input'})

    def append(self, row):
        with self.rollout.open('a', encoding='utf-8') as stream:
            stream.write(json.dumps(row) + '\n')

    def event(self, kind, turn='turn', **extra):
        self.append({'type': 'event_msg', 'payload': {'type': kind, 'turn_id': turn, **extra}})

    def test_exact_task_complete_requires_the_pinned_turn(self):
        guard = cli_client.NativeCompletion(self.entry)
        self.assertFalse(guard.poll())
        self.event('task_complete')
        self.assertTrue(guard.poll())

    def test_new_turn_peer_input_and_abort_cancel_the_bound_turn(self):
        for kind, extra in (
                ('task_started', {'turn': 'later'}), ('turn_aborted', {}),
                ('item_completed', {'item': {'type': 'UserMessage', 'client_id': 'peer'}})):
            with self.subTest(kind=kind):
                saved = self.rollout.read_bytes()
                guard = cli_client.NativeCompletion(self.entry)
                self.event('task_complete')
                self.event(kind, **extra)
                with self.assertRaises(RuntimeError):
                    guard.poll()
                self.rollout.write_bytes(saved)

    def test_matching_input_is_rejected_when_its_turn_is_foreign_or_missing(self):
        rows = [json.loads(line) for line in self.rollout.read_text(encoding='utf-8').splitlines()]
        for turn in ('foreign', None):
            with self.subTest(turn=turn):
                changed = [{**row, 'payload': dict(row['payload'])} for row in rows]
                if turn is None:
                    changed[-1]['payload'].pop('turn_id')
                else:
                    changed[-1]['payload']['turn_id'] = turn
                self.rollout.write_text(''.join(json.dumps(row) + '\n' for row in changed), encoding='utf-8')
                guard = cli_client.NativeCompletion(self.entry)
                self.event('task_complete')
                with self.assertRaisesRegex(RuntimeError, 'did not belong'):
                    guard.poll()
        self.rollout.write_text(''.join(json.dumps(row) + '\n' for row in rows), encoding='utf-8')

    def test_pin_rejects_replaced_or_truncated_rollout(self):
        guard = cli_client.NativeCompletion(self.entry)
        pin = guard.pin()
        original = self.rollout.read_bytes()
        self.rollout.unlink()
        self.rollout.write_bytes(original)
        with self.assertRaisesRegex(RuntimeError, 'replaced or truncated'):
            cli_client.NativeCompletion(self.entry, pin)
        self.rollout.unlink()
        self.rollout.write_bytes(original[:10])
        with self.assertRaisesRegex(RuntimeError, 'replaced or truncated'):
            cli_client.NativeCompletion(self.entry, pin)

    def test_untrusted_profile_or_session_metadata_refuses(self):
        foreign = {**self.entry, 'native_rollout': str(self.root / 'foreign.jsonl')}
        Path(foreign['native_rollout']).write_text(self.rollout.read_text(encoding='utf-8'), encoding='utf-8')
        with self.assertRaisesRegex(RuntimeError, 'outside'):
            cli_client.NativeCompletion(foreign)
        self.rollout.write_bytes(self.rollout.read_bytes().replace(b'"thread"', b'"other"'))
        with self.assertRaisesRegex(RuntimeError, 'does not belong'):
            cli_client.NativeCompletion(self.entry).poll()

    def test_partial_record_defers_completion_until_it_is_complete(self):
        guard = cli_client.NativeCompletion(self.entry)
        self.event('task_complete')
        with self.rollout.open('ab') as stream:
            stream.write(b'{')
        self.assertFalse(guard.poll())


if __name__ == '__main__':
    unittest.main()
