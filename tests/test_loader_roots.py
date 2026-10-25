import importlib.util
from pathlib import Path
import shutil
import tempfile
import time
import unittest


ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class LoaderRootTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='agent loader root ')
        self.addCleanup(self.temp.cleanup)
        self.outer = Path(self.temp.name) / 'outer'
        (self.outer / '.git').mkdir(parents=True)
        self.inner = self.outer / 'plugins/agent'
        marker = self.inner / '.codex-plugin/plugin.json'
        marker.parent.mkdir(parents=True)
        marker.write_text('{}', encoding='utf-8')

    def test_lane_loader_refuses_missing_inner_resolver(self):
        runtime = self.inner / '.agents/agent/runtime'
        runtime.mkdir(parents=True)
        for name in ('agent_cli.py', 'package_root.py'):
            shutil.copy2(ROOT / '.agents/agent/runtime' / name, runtime / name)
        sentinel = self.outer / '.agents/agent/resources/lanes/resolve.py'
        sentinel.parent.mkdir(parents=True)
        sentinel.write_text("raise AssertionError('outer resolver loaded')\n", encoding='utf-8')
        cli = load('nested_agent_cli', runtime / 'agent_cli.py')
        with self.assertRaisesRegex(cli.LaunchError, 'not found under'):
            cli._lanes_module()

    def test_store_loader_refuses_missing_inner_store(self):
        scripts = self.inner / '.agents/agent/utility/peer/scripts'
        scripts.mkdir(parents=True)
        shutil.copy2(ROOT / '.agents/agent/utility/peer/scripts/register_session.py', scripts / 'register_session.py')
        sentinel = self.outer / '.agents/agent-store/runtime/session_store.py'
        sentinel.parent.mkdir(parents=True)
        sentinel.write_text("raise AssertionError('outer store loaded')\n", encoding='utf-8')
        register = load('nested_register_session', scripts / 'register_session.py')
        self.assertIsNone(register._load_session_store())

    def test_followup_loader_refuses_missing_inner_transport(self):
        script = self.inner / '.agents/agent/utility/message/scripts/codex_followup.py'
        script.parent.mkdir(parents=True)
        shutil.copy2(ROOT / '.agents/agent/utility/message/scripts/codex_followup.py', script)
        sentinel = self.outer / '.agents/agent/runtime/codex_hook_trust.py'
        sentinel.parent.mkdir(parents=True)
        sentinel.write_text("raise AssertionError('outer transport loaded')\n", encoding='utf-8')
        followup = load('nested_codex_followup', script)
        with self.assertRaisesRegex(followup.FollowupError, 'transport was not found'):
            followup.Proxy('codex', self.inner, time.monotonic() + 1)


if __name__ == '__main__':
    unittest.main()
