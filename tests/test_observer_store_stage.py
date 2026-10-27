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
        if arguments[0] == 'whoami.exe':
            return SimpleNamespace(returncode=0, stdout='"EXAMPLE\\user","S-1-5-21-123-456-789-1001"\r\n', stderr='')
        return SimpleNamespace(returncode=0, stdout='', stderr='')

    def start_stage(self):
        with mock.patch.dict(os.environ, {'AGENT_STATE_DIRECTORY': str(self.state)}), \
                mock.patch.object(session_close.subprocess, 'run', side_effect=self.scheduler), \
                mock.patch.object(session_close.subprocess, 'Popen', return_value=SimpleNamespace(pid=1)):
            session_close.start_detached(SCRIPTS / 'client_close_watcher.py', ['--request', 'unused'])
        return next((self.state / 'merge-cleanup' / 'observers').iterdir())

    def test_start_detached_stage_loads_the_receipted_store(self):
        staging = self.start_stage()
        staged = load_register(staging / 'register_session.py', 'receipted_staged_register_session')

        store = staged._load_session_store()

        self.assertIsNotNone(store)
        self.assertTrue(hasattr(store, 'delete_session'))
        receipt = (staging / '.tj-agents-observer-store.json').read_text(encoding='utf-8')
        self.assertIn('tj-agents-agent-observer-store', receipt)

    def test_unmarked_or_tampered_sibling_store_is_refused(self):
        unmarked = Path(self.temporary.name) / 'unmarked'
        unmarked.mkdir()
        store_runtime = session_close.register_session.session_store_path().parent
        for name in ('register_session.py',):
            shutil.copy2(SCRIPTS / name, unmarked / name)
        for name in ('session_store.py', 'repository_identity.py'):
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


if __name__ == '__main__':
    unittest.main()
