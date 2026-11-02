import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock
import xml.etree.ElementTree as ET


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / 'plugins/agent/.agents/agent/utility/peer/scripts'
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
import session_close


def load_register(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ObserverStoreStageTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='observer store ')
        self.addCleanup(self.temporary.cleanup)
        self.state = Path(self.temporary.name) / 'state'

    @staticmethod
    def scheduler(arguments, **_kwargs):
        if Path(arguments[0]).name == 'whoami.exe':
            return SimpleNamespace(returncode=0, stdout='"EXAMPLE\\user","S-1-5-21-123-456-789-1001"\r\n', stderr='')
        return SimpleNamespace(returncode=0, stdout='', stderr='')

    def start_stage(self):
        with mock.patch.dict(os.environ, {'AGENT_STATE_DIRECTORY': str(self.state)}), \
                mock.patch('tj_cli.process.run', side_effect=self.scheduler), \
                mock.patch.object(session_close, 'windows_observer_command',
                                  side_effect=lambda command: ['C:/tj/tj_hook.exe', '--', *command]), \
                mock.patch.object(session_close.subprocess, 'Popen', return_value=SimpleNamespace(pid=1)):
            session_close.start_observer(['--result', 'unused'])
        return next((self.state / 'merge-cleanup' / 'observers').iterdir())

    def test_start_detached_stage_loads_the_receipted_store(self):
        staging = self.start_stage()
        staged = load_register(staging / 'register_session.py', 'receipted_staged_register_session')

        store = staged._load_session_store()

        self.assertIsNotNone(store)
        self.assertTrue(hasattr(store.SessionStore, 'delete_session'))
        receipt = (staging / '.tj-agents-observer-store.json').read_text(encoding='utf-8')
        self.assertIn('tj-agents-agent-observer-store', receipt)

    def test_unmarked_or_tampered_sibling_store_is_refused(self):
        unmarked = Path(self.temporary.name) / 'unmarked'
        unmarked.mkdir()
        store_runtime = session_close.register_session.session_store_path().parent
        for name in ('register_session.py',):
            shutil.copy2(SCRIPTS / name, unmarked / name)
        for name in session_close.OBSERVER_STORE_FILES[1:]:
            shutil.copy2(store_runtime / name, unmarked / name)
        self.assertIsNone(load_register(unmarked / 'register_session.py', 'unmarked_register_session')._load_session_store())

        staging = self.start_stage()
        (staging / 'session_store.py').write_text('tampered\n', encoding='utf-8')
        self.assertIsNone(load_register(staging / 'register_session.py', 'tampered_register_session')._load_session_store())

    def test_detached_launcher_restores_explicit_state_and_profiles(self):
        launcher = SCRIPTS / 'detached_launch.py'
        state, codex, claude = self.state, self.state / 'codex', self.state / 'claude'
        output, probe = self.state / 'context.json', self.state / 'probe.py'
        self.state.mkdir()
        probe.write_text(
            'import json, os, sys\nfrom pathlib import Path\n'
            'Path(sys.argv[1]).write_text(json.dumps({key: os.environ.get(key) for key in '
            '("AGENT_STATE_DIRECTORY", "CODEX_HOME", "CLAUDE_CONFIG_DIR")}))\n', encoding='utf-8')
        result = subprocess.run([sys.executable, '-I', '-B', str(launcher), '--state-directory', str(state),
                                 '--codex-home', str(codex), '--claude-config-dir', str(claude), '--',
                                 str(probe), str(output)], capture_output=True, text=True, encoding='utf-8')
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual({"AGENT_STATE_DIRECTORY": str(state.resolve()), "CODEX_HOME": str(codex.resolve()),
                          "CLAUDE_CONFIG_DIR": str(claude.resolve())}, json.loads(output.read_text(encoding='utf-8')))

    def test_scheduler_command_passes_only_owned_detached_context(self):
        with mock.patch.dict(os.environ, {'AGENT_STATE_DIRECTORY': str(self.state), 'CODEX_HOME': str(self.state / 'codex'),
                                          'CLAUDE_CONFIG_DIR': str(self.state / 'claude'), 'UNRELATED': 'secret'}, clear=True):
            command = session_close.detached_command(self.state / 'watcher.py', ['--request', 'exact'], SCRIPTS / 'detached_launch.py')
        self.assertIn('--state-directory', command)
        self.assertIn('--codex-home', command)
        self.assertIn('--claude-config-dir', command)
        self.assertNotIn('UNRELATED', ' '.join(command))

    def test_windows_task_starts_the_hidden_launcher_with_exact_arguments(self):
        directory = Path(self.temporary.name) / 'installed cli'
        directory.mkdir()
        launcher = directory / 'tj_hook.exe'
        launcher.touch()
        launcher.chmod(0o700)
        child = [sys.executable, '-I', '-B', 'C:/agent scripts/watcher.py', '--request', 'quoted "value"']
        with mock.patch.object(session_close.os, 'get_exec_path', return_value=[str(directory)]):
            command = session_close.windows_observer_command(child)
        document = ET.fromstring(session_close.windows_task_xml('S-1-5-21-1', command, self.state))
        namespace = {'task': session_close.TASK_SCHEDULER_NAMESPACE}
        self.assertEqual(str(launcher.absolute()), document.findtext('.//task:Command', namespaces=namespace))
        self.assertEqual(subprocess.list2cmdline(['--', *child]),
                         document.findtext('.//task:Arguments', namespaces=namespace))

    def test_missing_hidden_launcher_refuses_before_scheduling(self):
        with mock.patch.object(session_close.os, 'get_exec_path', return_value=[self.temporary.name]):
            with self.assertRaisesRegex(session_close.Refusal, 'tj_hook.exe is not installed'):
                session_close.windows_observer_command([sys.executable, 'watcher.py'])

    def test_scheduler_controls_use_the_windows_system_directory(self):
        system = Path(self.temporary.name) / 'Windows/System32'
        system.mkdir(parents=True)
        command = system / 'whoami.exe'
        command.touch()
        with mock.patch.dict(os.environ, {'SystemRoot': str(system.parent), 'PATH': str(self.state)}), \
                mock.patch('tj_cli.process.run', side_effect=self.scheduler) as run:
            self.assertEqual('S-1-5-21-123-456-789-1001', session_close.current_windows_user({}, 1))
        self.assertEqual(str(command), run.call_args.args[0][0])

    def test_host_exit_preserves_pending_cleanup_obligation(self):
        reaper = load_register(SCRIPTS / 'finish_reaper.py', 'pending_cleanup_reaper')
        obligation = self.state / 'merge-cleanup/obligations/pending.json'
        obligation.parent.mkdir(parents=True)
        content = {'worktree': self.temporary.name, 'session_id': 'session', 'cleanup_status': 'pending'}
        obligation.write_text(json.dumps(content), encoding='utf-8')
        output = self.state / 'exited.json'
        arguments = SimpleNamespace(state_directory=str(self.state), host_names=None,
                                    host_pid=123, host_start=1.0, worktree=self.temporary.name,
                                    result=str(output), session_id='session', invocation_id='invocation',
                                    parent_pid=None, parent_start=None, accept_timeout=1.0)
        with mock.patch.object(reaper, 'arguments', return_value=arguments), \
                mock.patch.object(reaper.session_close, 'process_info',
                                  return_value=SimpleNamespace(pid=456, started_at=2.0)), \
                mock.patch.object(reaper, 'await_acceptance', return_value=True), \
                mock.patch.object(reaper, 'await_exit', return_value=True), \
                mock.patch.object(reaper.session_close, 'delete_own_session'), \
                mock.patch.dict(os.environ, {'AGENT_STATE_DIRECTORY': str(self.state)}):
            self.assertEqual(0, reaper.main())
        self.assertEqual(content, json.loads(obligation.read_text(encoding='utf-8')))
        self.assertEqual('session-closed', json.loads(output.read_text(encoding='utf-8'))['status'])


if __name__ == '__main__':
    unittest.main()
