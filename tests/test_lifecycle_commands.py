"""tj status over a fixture store and dummy hosts."""
import contextlib
import importlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'plugins/agent/.agents/agent/runtime'))
from launch_contract import Liveness  # noqa: E402

agent_cli = importlib.import_module('agent_cli')
agent_store = importlib.import_module('agent_store')
lifecycle = importlib.import_module('lifecycle')
tj_status = importlib.import_module('tj_status')

ITEM = 'github:github.com/tj-agents/agent:98'


class DummyHost:
    def __init__(self, live):
        self.live = live
        self.respawned = []

    def liveness(self):
        return self.live

    def respawn(self, background):
        self.respawned.append(background)

    def resume_arguments(self, directory, title, session_id):
        return ['resume', session_id]


class Fixture(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='lifecycle ')
        self.addCleanup(temp.cleanup)
        self.worktree = str(Path(temp.name))
        self.sessions = [
            self.session('claude', 'live', 'active'),
            self.session('claude', 'background', 'active', title='agent#98 bg'),
            self.session('codex', 'dead', 'active', title='agent#98 review', parent='claude:live'),
            self.session('codex', 'finished', 'ended'),
        ]
        self.hosts = {
            agent_cli.Host.CLAUDE: DummyHost({'live': Liveness(True, 'busy'),
                                              'background': Liveness(False, background='bg-1')}),
            agent_cli.Host.CODEX: DummyHost({}),
        }
        self.cards = {ITEM: {'ref': ITEM, 'title': 'Cut over', 'state': 'In Progress'}}
        self.launched = []
        for target, value in ((agent_store, 'run'), (lifecycle, 'tj_item'), (agent_cli, 'host_launch'),
                              (agent_cli, 'launch')):
            patcher = mock.patch.object(target, value, getattr(self, f'fake_{value}'))
            patcher.start()
            self.addCleanup(patcher.stop)

    def session(self, host, native, state, title=None, parent=None):
        return {'session': f'{host}:{native}', 'host': host, 'native_id': native, 'state': state,
                'title': title, 'parent': parent, 'worktree': self.worktree, 'cwd': self.worktree}

    def fake_run(self, *arguments):
        if arguments == ('items',):
            return {'items': [{'item': {'ref': ITEM}}]}
        if arguments[:2] == ('sessions', '--item'):
            return {'item': {'ref': ITEM}, 'sessions': self.sessions}
        if arguments[0] == 'show':
            return {'session': next(s for s in self.sessions if s['session'] == arguments[1])}
        raise AssertionError(arguments)

    def fake_tj_item(self, verb, *arguments, cwd=None):
        if verb == 'mine':
            return []
        return self.cards[arguments[0]]

    def fake_host_launch(self, host):
        return self.hosts[agent_cli.Host(host)]

    def fake_launch(self, host, directory, title, arguments, item=None, parent=None, want_session=False):
        self.launched.append({'host': str(host), 'directory': str(directory), 'title': title,
                              'arguments': arguments, 'item': item, 'parent': parent})
        return {'launch': {'launch_id': f'L{len(self.launched)}'}}

    def main(self, module, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), mock.patch.object(lifecycle.Path, 'cwd', return_value=Path(self.worktree)):
            code = module.main(list(argv))
        return code, out.getvalue()


class StatusTests(Fixture):
    def test_status_lists_open_items_with_each_agent_from_host_liveness(self):
        with mock.patch.object(lifecycle, 'branch', return_value='Feature/98-AgentStoreCutover'), \
                mock.patch.object(lifecycle, 'pull_request', return_value=None):
            code, out = self.main(tj_status, '--json')
        self.assertEqual(0, code)
        [row] = json.loads(out)
        self.assertEqual((ITEM, 'In Progress', self.worktree), (row['item'], row['card'], row['worktree']))
        self.assertEqual(['busy', 'dead', 'dead', 'ended'], [agent['status'] for agent in row['agents']])

    def test_items_the_tracker_marks_done_are_not_open(self):
        self.cards[ITEM]['state'] = 'Done'
        self.assertEqual([], lifecycle.open_items(cwd=Path(self.worktree)))

    def test_ci_rolls_up_failing_before_pending_before_passing(self):
        self.assertEqual('failing', lifecycle.checks([{'conclusion': 'SUCCESS'}, {'conclusion': 'FAILURE'}]))
        self.assertEqual('pending', lifecycle.checks([{'conclusion': 'SUCCESS'}, {'status': 'IN_PROGRESS'}]))
        self.assertEqual('passing', lifecycle.checks([{'conclusion': 'SUCCESS'}, {'conclusion': 'SKIPPED'}]))
        self.assertIsNone(lifecycle.checks([]))


if __name__ == '__main__':
    unittest.main()
