import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class StartupBindingTests(unittest.TestCase):
    def setUp(self):
        self.binding = load('startup_binding_tests', ROOT / '.agents/agent/runtime/startup_binding.py')

    def test_windows_terminal_uses_application_title(self):
        cli = load('agent_cli_windows_tests', ROOT / '.agents/agent/runtime/agent_cli.py')
        calls = []

        def run(arguments, **kwargs):
            calls.append((arguments, kwargs))
            return SimpleNamespace(returncode=0, stdout='', stderr='')

        with mock.patch.object(cli.shutil, 'which', return_value='wt.exe'):
            cli._launch_windows_terminal(Path.cwd(), 'native.exe', 'initial title', (), (), {}, {}, run, None)

        arguments = calls[0][0]
        self.assertIn('--useApplicationTitle', arguments)
        self.assertNotIn('--suppressApplicationTitle', arguments)

    def test_prelaunch_snapshot_captures_tab_before_native_start(self):
        identity = SimpleNamespace(pid=71, started_at=8.0)
        terminal = {'kind': 'windows-terminal', 'session': 'terminal-session'}
        tab = {'runtime_id': 'uia-tab', 'observed_title': 'launch nonce',
               'evidence': 'windows-uia-unique-title-at-launch'}
        captured = []

        def capture(value):
            captured.append(dict(value))
            return tab

        with mock.patch.object(self.binding.register_session, 'terminal_identity', return_value=terminal), \
                mock.patch.object(self.binding.register_session, 'build_process_lookup', return_value=lambda _pid: identity), \
                mock.patch.object(self.binding, '_windows_tab_binding', side_effect=capture):
            snapshot = self.binding._prelaunch_terminal()

        self.assertEqual('uia-tab', snapshot['tab_binding']['runtime_id'])
        self.assertEqual({'pid': 71, 'started_at': 8.0}, snapshot['binding'])
        self.assertEqual([{'kind': 'windows-terminal', 'session': 'terminal-session',
                           'binding': {'pid': 71, 'started_at': 8.0}}], captured)

    def test_immediate_ack_records_once_with_prelaunch_terminal(self):
        process = mock.Mock(pid=72)
        process.wait.return_value = 0
        terminal = {'kind': 'windows-terminal', 'session': 'terminal-session',
                    'binding': {'pid': 71, 'started_at': 8.0},
                    'tab_binding': {'runtime_id': 'uia-tab', 'observed_title': 'launch nonce',
                                    'evidence': 'windows-uia-unique-title-at-launch'}}
        with mock.patch.object(self.binding.subprocess, 'Popen', return_value=process), \
                mock.patch.object(self.binding.cli_client, 'prepare', return_value=([], {}, None)), \
                mock.patch.object(self.binding.required_plugins, 'check', return_value={}), \
                mock.patch.object(self.binding, '_acknowledged', return_value=True), \
                mock.patch.object(self.binding, '_record_launch_binding') as record:
            result = self.binding.run_session('codex', 'native.exe', Path.cwd(), (),
                                              require_session_start=True, terminal=terminal)

        self.assertEqual(0, result)
        record.assert_called_once_with('codex', 72, terminal)

    def test_ordinary_status_retains_prelaunch_terminal_binding(self):
        directory = tempfile.TemporaryDirectory(prefix='startup status ')
        self.addCleanup(directory.cleanup)
        status_path = Path(directory.name) / 'status.json'
        terminal = {'kind': 'windows-terminal', 'session': 'terminal-session',
                    'binding': {'pid': 71, 'started_at': 8.0},
                    'tab_binding': {'runtime_id': 'uia-tab', 'observed_title': 'launch nonce',
                                    'evidence': 'windows-uia-unique-title-at-launch'}}
        calls = []

        def run_session(*_args, **kwargs):
            calls.append(('native-start', kwargs['terminal']))
            kwargs['on_running'](72)
            return 0

        with mock.patch.object(self.binding.required_plugins, 'check_launch', return_value=None), \
                mock.patch.object(self.binding.launch_check, 'check', return_value='not-applicable'), \
                mock.patch.object(self.binding, '_prelaunch_terminal', side_effect=lambda: calls.append(('capture', terminal)) or terminal), \
                mock.patch.object(self.binding, 'run_session', side_effect=run_session):
            result = self.binding.main(['--host', 'codex', '--project', directory.name, '--target', 'native.exe',
                                        '--status-file', str(status_path), '--'])

        self.assertEqual(0, result)
        self.assertEqual(['capture', 'native-start'], [entry[0] for entry in calls])
        status = json.loads(status_path.read_text(encoding='utf-8'))
        self.assertEqual('exited', status['state'])
        self.assertEqual(terminal, status['terminal'])


if __name__ == '__main__':
    unittest.main()
