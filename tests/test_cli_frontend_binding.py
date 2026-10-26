import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / '.agents/agent/utility/peer/scripts'
sys.path.insert(0, str(SCRIPTS))
import session_close


class FrontendBindingTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.state = Path(temporary.name)
        environment = mock.patch.dict(os.environ, {'AGENT_STATE_DIRECTORY': str(self.state)})
        environment.start()
        self.addCleanup(environment.stop)
        self.entry = {'host': 'codex', 'session_id': 'own', 'cwd': str(ROOT),
                      'pid': 10, 'pid_started_at': 100.0, 'started_at': 205.0,
                      'title': 'backend tab', 'terminal': {'kind': 'windows-terminal', 'session': 'backend'}}
        self.entries = [('own.json', self.entry), ('peer.json', {**self.entry, 'session_id': 'peer'})]
        entries = mock.patch.object(session_close, 'entries', side_effect=lambda: iter(self.entries))
        entries.start()
        self.addCleanup(entries.stop)
        process = session_close.register_session.ProcessInfo
        self.processes = {10: process(10, 1, 'codex.exe', 100.0),
                          20: process(20, 1, 'python.exe', 201.0),
                          21: process(21, 20, 'codex.exe', 202.0)}
        lookup = mock.patch.object(session_close, 'process_info', side_effect=self.processes.get)
        lookup.start()
        self.addCleanup(lookup.stop)
        self.receipt = {'host': 'codex', 'session': 'own', 'state': 'launched', 'created_at': 200.0,
                        'wrapper': {'pid': 20, 'started_at': 201.0,
                                    'child': {'pid': 21, 'started_at': 202.0},
                                    'title': 'own tab',
                                    'terminal': {'kind': 'windows-terminal', 'runtime_id': 'own-tab'}}}

    def receipt_file(self):
        directory = self.state / 'recovery-launches'
        directory.mkdir(exist_ok=True)
        (directory / 'attempt.json').write_text(json.dumps(self.receipt), encoding='utf-8')

    def test_shared_backend_resolves_only_its_bound_frontend_and_terminal(self):
        self.receipt_file()
        target = session_close.close_target(self.entry)
        self.assertEqual((21, 202.0, 'own tab'), (target['pid'], target['pid_started_at'], target['title']))
        self.assertEqual('own-tab', target['terminal']['runtime_id'])
        self.assertEqual(10, self.entry['pid'])

    def test_missing_or_legacy_receipt_never_uses_the_backends_terminal(self):
        for wrapper in (None, {'pid': 20, 'child': {'pid': 21, 'started_at': 202.0}}):
            self.receipt['wrapper'] = wrapper
            self.receipt_file()
            with self.assertRaisesRegex(session_close.Refusal, 'exact frontend launch binding'):
                session_close.close_target(self.entry)

    def test_unbound_codex_refuses_even_when_no_peer_registration_exists(self):
        self.entries[:] = [('own.json', self.entry)]
        with self.assertRaisesRegex(session_close.Refusal, 'exact frontend launch binding'):
            session_close.close_target(self.entry)

    def test_reused_process_detached_child_or_older_registration_is_refused(self):
        process = session_close.register_session.ProcessInfo
        for replacement in (process(21, 20, 'codex.exe', 203.0),
                            process(21, 99, 'codex.exe', 202.0),
                            process(21, 20, 'other.exe', 202.0), None):
            self.processes[21] = replacement
            self.receipt_file()
            with self.assertRaisesRegex(session_close.Refusal, 'stale or unverifiable'):
                session_close.close_target(self.entry)

    def test_native_session_id_prevents_latest_shared_backend_attachment_winning(self):
        self.entries[1][1]['started_at'] = 300.0
        client = self.processes[21]
        details = {'client_identity': 'managed-input', 'launch_cwd': str(ROOT), 'title': 'own tab',
                   'client_message_id': 'own-input', 'native_turn_id': 'own-turn',
                   'recording_path': str(self.state / 'events.jsonl'), 'backend': None,
                   'profile': str(self.state / 'profile'), 'terminal': self.entry['terminal']}

        def current(session_id, nearest):
            self.assertEqual(self.processes[10], nearest)
            if session_id != 'own':
                raise RuntimeError('the explicit native session has no attributable frontend')
            return client, details

        with mock.patch.dict(os.environ, {'CODEX_THREAD_ID': 'own'}), \
                mock.patch.object(session_close.os, 'getppid', return_value=10), \
                mock.patch.object(session_close, 'current_codex_client', side_effect=current) as selected:
            entry = session_close.own_host_and_entry(ROOT)
            self.assertEqual('own', entry['session_id'])
            self.assertEqual(client.pid, entry['pid'])
            selected.assert_called_once_with('own', self.processes[10])
        with mock.patch.dict(os.environ, {'CODEX_THREAD_ID': 'missing'}), \
                mock.patch.object(session_close.os, 'getppid', return_value=10), \
                mock.patch.object(session_close, 'current_codex_client', side_effect=current):
            with self.assertRaisesRegex(session_close.Refusal, 'explicit native session'):
                session_close.own_host_and_entry(ROOT)

    def test_windows_runtime_id_guard_uses_the_frontends_observed_title(self):
        import close_tab
        self.receipt_file()
        target = session_close.close_target(self.entry)
        actual = {'title': 'own tab', 'identity': {'kind': 'windows-terminal', 'runtime_id': 'own-tab'}}
        with mock.patch.object(session_close, 'liveness', return_value=True), \
                mock.patch.object(close_tab, 'is_windows', return_value=True), \
                mock.patch.object(close_tab, 'windows_inventory', return_value=[actual]), \
                mock.patch.object(close_tab, 'close_actual') as close:
            close_tab.close_entry(target)
        close.assert_called_once_with(actual, force=True)


if __name__ == '__main__':
    unittest.main()
