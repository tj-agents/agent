import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
import sys
from types import SimpleNamespace
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'plugins/agent/.agents/agent/runtime'))
from host import Host  # noqa: E402
from host_sessions import SESSIONS  # noqa: E402


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class StartupBindingTests(unittest.TestCase):
    def setUp(self):
        self.binding = load('startup_binding_tests', ROOT / 'plugins/agent/.agents/agent/runtime/startup_binding.py')

    def test_windows_terminal_uses_application_title(self):
        cli = load('agent_cli_windows_tests', ROOT / 'plugins/agent/.agents/agent/runtime/agent_cli.py')
        calls = []

        def run(arguments, **kwargs):
            calls.append((arguments, kwargs))
            return SimpleNamespace(returncode=0, stdout='', stderr='')

        with mock.patch.object(cli.shutil, 'which', return_value='wt.exe'):
            cli._launch_windows_terminal(Path.cwd(), 'native.exe', 'initial title', (), (), {}, {}, run, None)

        arguments = calls[0][0]
        self.assertIn('--useApplicationTitle', arguments)
        self.assertNotIn('--suppressApplicationTitle', arguments)

    def test_startup_receipts_cover_each_selected_dispatcher(self):
        temporary = tempfile.TemporaryDirectory(prefix='startup manifests ')
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        roots = {}
        for name in ('agent', 'base'):
            plugin = root / name
            manifest = plugin / '.codex-plugin/plugin.json'
            hooks = plugin / 'hooks.json'
            manifest.parent.mkdir(parents=True)
            hooks.write_text(json.dumps({'hooks': {'SessionStart': [{'hooks': [{'command': 'python hook_dispatch.py'}]}]}}), encoding='utf-8')
            manifest.write_text(json.dumps({'hooks': ['hooks.json']}), encoding='utf-8')
            roots[name + '@tj-agents'] = plugin
        self.assertEqual(sorted(roots), self.binding.startup_participants(Host.CODEX, roots))

    def test_required_startup_failure_stops_the_owned_native_child(self):
        temporary = tempfile.TemporaryDirectory(prefix='startup failure ')
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        script = root / 'failure.py'
        survived = root / 'survived'
        script.write_text("import os,time\nfrom pathlib import Path\n"
                          "directory=Path(os.environ['TJ_AGENTS_STARTUP_ACK_DIRECTORY'])\n"
                          "token=os.environ['TJ_AGENTS_STARTUP_ACK_TOKEN']\n"
                          "(directory/(token+'.failed')).write_text('required fixture hook failed')\n"
                          "time.sleep(3)\n"
                          f"Path({str(survived)!r}).write_text('ordinary work')\n", encoding='utf-8')

        def prepare(argv, _project, environment, **_kwargs):
            return argv, environment, None

        with mock.patch.object(self.binding.required_plugins, 'check', return_value={}), \
                mock.patch.object(SESSIONS[Host.CODEX], 'prepare_frontend', side_effect=prepare):
            with self.assertRaisesRegex(self.binding.StartupBindingError, 'fixture hook failed'):
                self.binding.run_session(Host.CODEX, sys.executable, root, [str(script)], require_session_start=True)
        self.assertFalse(survived.exists())

    def test_every_host_launch_drops_inherited_frontend_recording_state(self):
        process = mock.Mock(pid=72)
        process.wait.return_value = 0
        inherited = {name: 'parent' for native in Host for name in SESSIONS[native].FRONTEND_STATE}
        self.assertTrue(inherited)
        with mock.patch.dict(os.environ, inherited),                 mock.patch.object(self.binding.subprocess, 'Popen', return_value=process) as spawn:
            self.assertEqual(0, self.binding.run_session(Host.CLAUDE, 'native.exe', Path.cwd(), ()))
        self.assertFalse(set(inherited) & set(spawn.call_args.kwargs['env']))

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
                mock.patch.object(SESSIONS[Host.CODEX], 'prepare_frontend', return_value=([], {}, None)), \
                mock.patch.object(self.binding.required_plugins, 'check', return_value={}), \
                mock.patch.object(self.binding, '_acknowledged', return_value=True), \
                mock.patch.object(self.binding, '_record_launch_binding') as record:
            result = self.binding.run_session(Host.CODEX, 'native.exe', Path.cwd(), (),
                                              require_session_start=True, terminal=terminal)

        self.assertEqual(0, result)
        record.assert_called_once_with(Host.CODEX, 72, terminal)

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

        with mock.patch.object(self.binding.launch_check, 'prepare_launch_roots', return_value=(None, None)), \
                mock.patch.object(self.binding.launch_check, 'check', autospec=True, return_value='not-applicable'), \
                mock.patch.object(self.binding, '_prelaunch_terminal', side_effect=lambda: calls.append(('capture', terminal)) or terminal), \
                mock.patch.object(self.binding, 'run_session', side_effect=run_session):
            result = self.binding.main(['--host', 'codex', '--project', directory.name, '--target', 'native.exe',
                                        '--status-file', str(status_path), '--'])

        self.assertEqual(0, result)
        self.assertEqual(['capture', 'native-start'], [entry[0] for entry in calls])
        status = json.loads(status_path.read_text(encoding='utf-8'))
        self.assertEqual('exited', status['state'])
        self.assertEqual(terminal, status['terminal'])

    def test_update_check_uses_the_calling_host_with_the_launch_check_signature(self):
        directory = tempfile.TemporaryDirectory(prefix='startup update check ')
        self.addCleanup(directory.cleanup)
        calls = []

        def check(project, fetch=None, update=None, now=None, hosts=tuple(Host), updated=None, *, recover=True):
            calls.append((project, hosts))
            return 'current'

        with mock.patch.object(self.binding.launch_check, 'prepare_launch_roots', return_value=(None, None)), \
                mock.patch.object(self.binding.launch_check, 'check', side_effect=check), \
                mock.patch.object(self.binding, 'run_session', return_value=0):
            result = self.binding.main(['--host', 'claude', '--project', directory.name, '--target', 'native-claude'])

        self.assertEqual(0, result)
        self.assertEqual([(Path(directory.name).resolve(), (Host.CLAUDE,))], calls)

    def test_failed_plugin_setup_still_starts_the_host_without_the_session_start_handshake(self):
        directory = tempfile.TemporaryDirectory(prefix='startup degraded ')
        self.addCleanup(directory.cleanup)
        calls = []

        def run_session(*_args, **kwargs):
            calls.append(kwargs)
            return 0

        failure = RuntimeError('Required plugin setup failed: react@tj-agents needs machine@tj-agents')
        with mock.patch.object(self.binding.launch_check, 'prepare_launch_roots', side_effect=failure),                 mock.patch.object(self.binding.launch_check, 'check', autospec=True, return_value='failed'),                 mock.patch.object(self.binding, 'run_session', side_effect=run_session):
            result = self.binding.main(['--host', 'claude', '--project', directory.name, '--target', 'native-claude',
                                        '--require-session-start'])

        self.assertEqual(0, result)
        self.assertEqual(1, len(calls))
        self.assertNotIn('require_session_start', calls[0])

    def test_claude_override_roster_is_passed_from_preflight_to_startup(self):
        directory = tempfile.TemporaryDirectory(prefix='startup claude override ')
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        roots = {'agent@tj-agents': root / 'override-agent'}
        calls = []

        def run_session(*_args, **kwargs):
            calls.append(kwargs)
            kwargs['on_running'](72)
            return 0

        arguments = [
            '--settings', 'override.json', '--setting-sources=project,local',
            '--plugin-dir', 'plugins', '--plugin-url=https://example.test/plugin', '--', '--settings', 'literal prompt',
        ]
        with mock.patch.object(self.binding.launch_check, 'prepare_launch_roots', return_value=(root, roots)) as prepare, \
                mock.patch.object(self.binding.launch_check, 'check', autospec=True, return_value='current'), \
                mock.patch.object(self.binding, 'run_session', side_effect=run_session):
            result = self.binding.main([
                '--host', 'claude', '--project', directory.name, '--target', 'native-claude',
                '--require-session-start', '--', *arguments,
            ])

        self.assertEqual(0, result)
        self.assertEqual([
            mock.call(root.resolve(), Host.CLAUDE, 'native-claude', arguments),
            mock.call(root.resolve(), Host.CLAUDE, 'native-claude', arguments),
        ], prepare.call_args_list)
        self.assertIs(roots, calls[0]['hook_roots'])

    def test_updated_plugin_roster_replaces_removed_startup_participants_before_spawn(self):
        directory = tempfile.TemporaryDirectory(prefix='startup updated roster ')
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)

        def dispatcher(name):
            plugin = root / name
            manifest = plugin / '.claude-plugin/plugin.json'
            hooks = plugin / 'hooks.json'
            manifest.parent.mkdir(parents=True)
            hooks.write_text(json.dumps({'hooks': {'SessionStart': [
                {'hooks': [{'command': 'python hook_dispatch.py'}]},
            ]}}), encoding='utf-8')
            manifest.write_text(json.dumps({'hooks': ['hooks.json']}), encoding='utf-8')
            return plugin

        old_roots = {'old@tj-agents': dispatcher('old')}
        new_roots = {'new@tj-agents': dispatcher('new')}
        installed = {'roots': old_roots}

        def prepare_launch_roots(*_args):
            return root, installed['roots']

        def update_roster(*_args, **_kwargs):
            installed['roots'] = new_roots
            return 'updated'

        process = mock.Mock(pid=72)
        process.wait.return_value = 0

        def native_prepare(argv, _project, environment, **_kwargs):
            return argv, environment, None

        with mock.patch.object(self.binding.launch_check, 'prepare_launch_roots', side_effect=prepare_launch_roots) as preflight, \
                mock.patch.object(self.binding.launch_check, 'check', autospec=True, side_effect=update_roster), \
                mock.patch.object(SESSIONS[Host.CLAUDE], 'prepare_frontend', side_effect=native_prepare), \
                mock.patch.object(self.binding.subprocess, 'Popen', return_value=process) as spawn, \
                mock.patch.object(self.binding, '_acknowledged', return_value=True), \
                mock.patch.object(self.binding, '_record_launch_binding'):
            result = self.binding.main([
                '--host', 'claude', '--project', directory.name, '--target', 'native-claude',
                '--require-session-start', '--', '--settings', 'override.json', '--', 'literal prompt',
            ])

        self.assertEqual(0, result)
        self.assertEqual([
            mock.call(root.resolve(), Host.CLAUDE, 'native-claude',
                      ['--settings', 'override.json', '--', 'literal prompt']),
            mock.call(root.resolve(), Host.CLAUDE, 'native-claude',
                      ['--settings', 'override.json', '--', 'literal prompt']),
        ], preflight.call_args_list)
        required = json.loads(spawn.call_args.kwargs['env'][self.binding.REQUIRED_HOOKS_ENV])
        self.assertEqual(['new@tj-agents'], required)


if __name__ == '__main__':
    unittest.main()
