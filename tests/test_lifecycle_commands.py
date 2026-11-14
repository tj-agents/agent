"""tj status over a fixture store and dummy hosts."""
import contextlib
import importlib
import io
import json
from pathlib import Path
import sys
import tempfile
import threading
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
        if verb == 'prs':
            return {'prs': []}
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
    def test_linked_cross_repository_prs_are_distinct_from_branch_pr(self):
        prs = [{'url': f'https://github.com/tj-agents/{repo}/pull/{number}',
                'state': 'open', 'head': 'feature', 'title': repo}
               for repo, number in [('cli', 1), ('agent', 2)]]
        branch_pr = {'number': 19, 'state': 'CLOSED', 'ci': None, 'url': 'https://example.com/19'}
        with mock.patch.object(lifecycle, 'tj_item', return_value={'prs': prs}), \
                mock.patch.object(lifecycle, 'branch', return_value='branch'), \
                mock.patch.object(lifecycle, 'pull_request', return_value=branch_pr):
            [row] = tj_status.report([lifecycle.Item(ITEM, None)])
        self.assertEqual(branch_pr, row['pr'])
        self.assertEqual(['tj-agents/cli', 'tj-agents/agent'], [pr['repository'] for pr in row['linked_prs']])
        text = tj_status.render([row])
        self.assertIn('branch pr #19 closed', text)
        self.assertIn('linked pr tj-agents/cli open', text)
        self.assertIn('linked pr tj-agents/agent open', text)

    def test_linked_pr_failure_is_distinct_from_empty(self):
        with mock.patch.object(lifecycle, 'tj_item', side_effect=lifecycle.LifecycleError('offline')):
            [row] = tj_status.report([lifecycle.Item(ITEM, None)])
        self.assertIsNone(row['linked_prs'])
        self.assertEqual('offline', row['linked_prs_error'])
        self.assertIn('linked prs unavailable: offline', tj_status.render([row]))
        with mock.patch.object(lifecycle, 'tj_item', return_value={'prs': []}):
            [row] = tj_status.report([lifecycle.Item(ITEM, None)])
        self.assertEqual([], row['linked_prs'])
        self.assertIsNone(row['linked_prs_error'])
        self.assertIn('linked prs none', tj_status.render([row]))
        with mock.patch.object(lifecycle, 'tj_item', return_value={'prs': {}}):
            [row] = tj_status.report([lifecycle.Item(ITEM, None)])
        self.assertIsNone(row['linked_prs'])
        self.assertIn('invalid linked pull request list', row['linked_prs_error'])

    def test_azure_devops_linked_pr_is_retained_and_displayed(self):
        url = 'https://dev.azure.com/org/project/_git/repo/pullrequest/42'
        with mock.patch.object(lifecycle, 'tj_item', return_value={'prs': [{'url': url, 'state': 'open'}]}):
            [row] = tj_status.report([lifecycle.Item(ITEM, None)])
        self.assertEqual(url, row['linked_prs'][0]['url'])
        self.assertIsNone(row['linked_prs'][0]['repository'])
        self.assertIsNone(row['linked_prs_error'])
        self.assertIn(f'linked pr open  {url}', tj_status.render([row]))

    def test_invalid_linked_pr_url_type_reports_unavailable(self):
        with mock.patch.object(lifecycle, 'tj_item', return_value={'prs': [{'url': 123, 'state': 'open'}]}):
            [row] = tj_status.report([lifecycle.Item(ITEM, None)])
        self.assertIsNone(row['linked_prs'])
        self.assertIn('linked prs unavailable', tj_status.render([row]))

    def test_item_and_report_lookups_overlap_and_preserve_order(self):
        refs = ['first', 'second']
        barrier = threading.Barrier(2)
        def store(*arguments):
            ref = arguments[-1]
            barrier.wait(timeout=5)
            return {'item': {'ref': ref}, 'sessions': []}
        with mock.patch.object(lifecycle, 'liveness', return_value={}), \
                mock.patch.object(agent_store, 'run', side_effect=store), \
                mock.patch.object(lifecycle, 'tj_item', return_value={'state': 'Open'}):
            items = lifecycle.open_items(refs)
        self.assertEqual(refs, [item.ref for item in items])
        barrier = threading.Barrier(2)
        def linked(_verb, ref, cwd=None):
            barrier.wait(timeout=5)
            return {'prs': [{'url': f'https://github.com/tj-agents/{ref}/pull/1', 'state': 'open'}]}
        with mock.patch.object(lifecycle, 'tj_item', side_effect=linked):
            rows = tj_status.report(items)
        self.assertEqual(refs, [row['item'] for row in rows])
        self.assertEqual(['tj-agents/first', 'tj-agents/second'],
                         [row['linked_prs'][0]['repository'] for row in rows])

    def test_item_store_failure_remains_fatal(self):
        with mock.patch.object(lifecycle, 'liveness', return_value={}), \
                mock.patch.object(agent_store, 'run', side_effect=agent_store.AgentStoreError('error', 'store failed')), \
                self.assertRaises(agent_store.AgentStoreError):
            lifecycle.open_items(['first'])

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
