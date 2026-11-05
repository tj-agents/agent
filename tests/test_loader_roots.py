import importlib.util
from pathlib import Path
import shutil
import sys
import tempfile
import time
import types
import unittest
from unittest import mock


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

    def followup(self, root):
        script = root / '.agents/agent/utility/message/scripts/codex_followup.py'
        script.parent.mkdir(parents=True)
        shutil.copy2(ROOT / 'plugins/agent/.agents/agent/utility/message/scripts/codex_followup.py', script)
        return load('nested_codex_followup_' + root.name, script)

    def client_runtime(self, followup):
        modules = {name: module for name, module in sys.modules.items()
                   if name == 'codex' or name.startswith('codex.')}
        paths = list(sys.path)
        for name in modules:
            sys.modules.pop(name)
        try:
            return followup.client_runtime()
        finally:
            for name in tuple(sys.modules):
                if name == 'codex' or name.startswith('codex.'):
                    sys.modules.pop(name)
            sys.modules.update(modules)
            sys.path[:] = paths

    def test_lane_loader_refuses_missing_inner_resolver(self):
        runtime = self.inner / '.agents/agent/runtime'
        runtime.mkdir(parents=True)
        for name in ('agent_cli.py', 'package_root.py'):
            shutil.copy2(ROOT / 'plugins/agent/.agents/agent/runtime' / name, runtime / name)
        sentinel = self.outer / '.agents/agent/resources/lanes/resolve.py'
        sentinel.parent.mkdir(parents=True)
        sentinel.write_text("raise AssertionError('outer resolver loaded')\n", encoding='utf-8')
        cli = load('nested_agent_cli', runtime / 'agent_cli.py')
        with self.assertRaisesRegex(cli.LaunchError, 'not found under'):
            cli._lanes_module()

    def test_store_loader_refuses_missing_inner_store(self):
        scripts = self.inner / '.agents/agent/utility/peer/scripts'
        scripts.mkdir(parents=True)
        shutil.copy2(ROOT / 'plugins/agent/.agents/agent/utility/peer/scripts/register_session.py', scripts / 'register_session.py')
        sentinel = self.outer / '.agents/agent-store/runtime/session_store.py'
        sentinel.parent.mkdir(parents=True)
        sentinel.write_text("raise AssertionError('outer store loaded')\n", encoding='utf-8')
        register = load('nested_register_session', scripts / 'register_session.py')
        self.assertIsNone(register._load_session_store())

    def test_followup_loader_refuses_missing_inner_transport(self):
        followup = self.followup(self.inner)
        sentinel = self.outer / '.agents/agent/runtime/codex/hook_trust.py'
        sentinel.parent.mkdir(parents=True)
        sentinel.write_text("raise AssertionError('outer transport loaded')\n", encoding='utf-8')
        with self.assertRaisesRegex(followup.FollowupError, 'transport was not found'):
            followup.Proxy('codex', self.inner, time.monotonic() + 1)

    def test_followup_client_refuses_outer_runtime_after_an_inner_marker(self):
        followup = self.followup(self.inner)
        sentinel = self.outer / '.agents/agent/runtime/codex/client.py'
        sentinel.parent.mkdir(parents=True)
        sentinel.with_name('__init__.py').write_text('', encoding='utf-8')
        sentinel.write_text("raise AssertionError('outer client loaded')\n", encoding='utf-8')
        with self.assertRaisesRegex(followup.FollowupError, 'client is unavailable'):
            self.client_runtime(followup)

    def test_followup_client_accepts_the_nearest_packaged_runtime(self):
        followup = self.followup(self.inner)
        client = self.inner / '.agents/agent/runtime/codex/client.py'
        client.parent.mkdir(parents=True)
        client.with_name('__init__.py').write_text('', encoding='utf-8')
        client.write_text("SOURCE = 'inner'\n", encoding='utf-8')
        runtime = self.client_runtime(followup)
        self.assertEqual('inner', runtime.SOURCE)
        self.assertEqual(client.resolve(), Path(runtime.__file__).resolve())

    def test_followup_client_refuses_when_no_package_marker_exists(self):
        orphan = Path(self.temp.name) / 'orphan'
        followup = self.followup(orphan)
        with self.assertRaisesRegex(followup.FollowupError, 'client is unavailable'):
            self.client_runtime(followup)

    def test_followup_client_refuses_a_cached_outer_origin(self):
        followup = self.followup(self.inner)
        client = self.inner / '.agents/agent/runtime/codex/client.py'
        client.parent.mkdir(parents=True)
        client.with_name('__init__.py').write_text('', encoding='utf-8')
        client.write_text("SOURCE = 'inner'\n", encoding='utf-8')
        cached = types.ModuleType('codex.client')
        cached.__file__ = str(self.outer / '.agents/agent/runtime/codex/client.py')
        package = types.ModuleType('codex')
        package.client = cached
        with mock.patch.dict(sys.modules, {'codex': package, 'codex.client': cached}):
            with self.assertRaisesRegex(followup.FollowupError, 'did not load'):
                followup.client_runtime()


if __name__ == '__main__':
    unittest.main()
