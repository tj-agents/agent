import importlib.util
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
SCRIPTS = ROOT / '.agents/agent/utility/peer/scripts'
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
            session_close.start_observer(['--result', 'unused'])
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


if __name__ == '__main__':
    unittest.main()
