"""tj status and tj reload over a fixture store and dummy hosts: no real agent-store, host or terminal."""
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
tj_reload = importlib.import_module('tj_reload')
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


class ReloadTests(Fixture):
    def test_reload_respawns_background_rows_and_resumes_dead_agents_in_tj_run_tabs(self):
        code, out = self.main(tj_reload, '98')
        self.assertEqual(0, code)
        self.assertEqual(['bg-1'], self.hosts[agent_cli.Host.CLAUDE].respawned)
        self.assertEqual([{'host': 'codex', 'directory': self.worktree, 'title': 'agent#98 review',
                           'arguments': ['resume', 'dead'], 'item': ITEM, 'parent': 'claude:live'}],
                         self.launched)
        self.assertEqual([('claude:background', 'respawned'), ('codex:dead', 'resumed')],
                         [(row['session'], row['action']) for row in json.loads(out)])

    def test_live_agents_and_cleanly_ended_ones_are_left_alone_unless_ended_is_asked_for(self):
        self.main(tj_reload, '98', '--ended')
        self.assertEqual(['codex:dead', 'codex:finished'],
                         [f"codex:{launch['arguments'][1]}" for launch in self.launched])

    def test_dry_run_restores_nothing(self):
        code, out = self.main(tj_reload, '--dry-run')
        self.assertEqual(0, code)
        self.assertEqual([], self.launched)
        self.assertEqual([], self.hosts[agent_cli.Host.CLAUDE].respawned)
        self.assertEqual(['dead', 'dead'], [row['status'] for row in json.loads(out)])

    def test_a_vanished_worktree_fails_that_agent_and_the_command(self):
        self.sessions[2]['worktree'] = self.sessions[2]['cwd'] = str(Path(self.worktree) / 'gone')
        code, out = self.main(tj_reload, '98')
        self.assertEqual(1, code)
        self.assertEqual('failed', json.loads(out)[-1]['action'])

    def test_one_live_session_is_never_given_a_second_writer(self):
        code, out = self.main(tj_reload, '--session', 'claude:live')
        self.assertEqual((0, []), (code, json.loads(out)))
        self.assertEqual([], self.launched)

    def test_one_dead_session_resumes_without_an_item(self):
        self.main(tj_reload, '--session', 'codex:dead')
        self.assertEqual([None], [launch['item'] for launch in self.launched])


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
