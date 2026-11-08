"""session_hook.py: what each host event records in agent-store, with the store and hosts faked."""
import importlib
import io
from pathlib import Path
import sys
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'plugins/agent/.agents/agent/runtime'))
hook = importlib.import_module('session_hook')
Host = hook.Host


class StartTests(unittest.TestCase):
    def start(self, host, environ, title=None):
        run = mock.Mock(return_value={'session': {'title': title}})
        adapter = mock.Mock()
        with mock.patch.object(hook.agent_store, 'run', run), \
                mock.patch.object(hook, 'worktree', return_value='/repo'), \
                mock.patch('agent_cli.host_launch', return_value=adapter):
            hook.start(host, 'n1', '/repo/sub', environ=environ)
        return list(run.call_args.args), adapter

    def test_a_tj_run_session_binds_to_its_launch_and_a_codex_thread_takes_its_title(self):
        arguments, adapter = self.start(Host.CODEX, {hook.LAUNCH_ENV: 'L1', 'WT_SESSION': 'w'}, title='agent#98')
        self.assertEqual(['register', '--session', 'codex:n1', '--cwd', '/repo/sub', '--worktree', '/repo',
                          '--launch-id', 'L1'], arguments)
        adapter.name_session.assert_called_once_with('n1', 'agent#98')

    def test_a_session_outside_tj_run_records_its_terminal_and_title(self):
        arguments, adapter = self.start(Host.CLAUDE, {'TMUX_PANE': '%3', hook.TITLE_ENV: ' tab '})
        self.assertEqual(['--terminal-kind', 'tmux', '--terminal-id', '%3', '--title', 'tab'], arguments[7:])
        adapter.name_session.assert_not_called()


class EventTests(unittest.TestCase):
    def main(self, *argv, stdin='{"session_id": "n1"}'):
        with mock.patch.object(sys, 'stdin', io.StringIO(stdin)), mock.patch.object(sys, 'stderr', io.StringIO()):
            return hook.main(list(argv))

    def test_end_closes_the_session_and_an_unknown_one_is_not_an_error(self):
        run = mock.Mock(side_effect=hook.agent_store.AgentStoreError('not-found', 'unknown'))
        with mock.patch.object(hook.agent_store, 'run', run):
            self.assertEqual(0, self.main('end', '--host', 'claude'))
        run.assert_called_once_with('end', '--session', 'claude:n1')

    def test_a_store_failure_never_stops_the_session(self):
        with mock.patch.object(hook, 'start', side_effect=hook.agent_store.AgentStoreError('missing', 'x')):
            self.assertEqual(0, self.main('start', '--host', 'codex'))

    def test_a_prompt_cancels_the_pending_closeout(self):
        with mock.patch.object(hook.shutil, 'which', return_value='tj'), \
                mock.patch.object(hook.process, 'run') as run:
            self.assertEqual(0, self.main('prompt', '--host', 'claude'))
        self.assertEqual(['tj', 'done', '--cancel'], run.call_args.args[0])

    def test_a_payload_without_a_session_records_nothing(self):
        with mock.patch.object(hook.agent_store, 'run') as run:
            self.assertEqual(0, self.main('start', '--host', 'claude', stdin='{}'))
        run.assert_not_called()


if __name__ == '__main__':
    unittest.main()
