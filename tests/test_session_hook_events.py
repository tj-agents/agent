"""session_hook.py: what each host event records in agent-store, with the store and hosts faked."""
import importlib
import io
import json
import os
from pathlib import Path
import sys
import unittest
from unittest import mock

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'plugins/agent/.agents/agent/runtime'))
hook = importlib.import_module('session_hook')
Host = hook.Host


@pytest.fixture(autouse=True)
def isolated_worktree_claim(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hook.worktrees, 'claim_started_worktree', lambda *_: None)


class AgentStoreCalls:
    """A fake `agent_store.run` answering each registered outcome in turn and recording every call."""

    def __init__(self, *outcomes):
        self.queue = list(outcomes)
        self.calls = []

    def __call__(self, *arguments):
        self.calls.append(arguments)
        outcome = self.queue.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


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


@pytest.mark.parametrize('reason', ['clear', 'resume', 'logout', 'prompt_input_exit'])
def test_end_closes_the_session_for_an_explicit_exit_reason(monkeypatch, reason):
    run = mock.Mock(side_effect=hook.agent_store.AgentStoreError('not-found', 'unknown'))
    monkeypatch.setattr(hook.agent_store, 'run', run)
    monkeypatch.setattr(sys, 'stdin', io.StringIO(json.dumps({'session_id': 'n1', 'reason': reason})))
    assert hook.main(['end', '--host', 'claude']) == 0
    run.assert_called_once_with('end', '--session', 'claude:n1')


@pytest.mark.parametrize('stdin', [
    json.dumps({'session_id': 'n1', 'reason': 'other'}),
    json.dumps({'session_id': 'n1'}),
    json.dumps({'session_id': 'n1', 'reason': 'something-unexpected'}),
])
def test_end_leaves_the_session_active_for_other_missing_or_unknown_reasons(monkeypatch, stdin):
    run = mock.Mock()
    monkeypatch.setattr(hook.agent_store, 'run', run)
    monkeypatch.setattr(sys, 'stdin', io.StringIO(stdin))
    assert hook.main(['end', '--host', 'claude']) == 0
    run.assert_not_called()


def test_an_unrecorded_launch_registers_by_title_and_reports_it_on_stderr(monkeypatch, capsys):
    calls = AgentStoreCalls(hook.agent_store.AgentStoreError('not-found', 'no launch'), {'session': {'title': None}})
    monkeypatch.setattr(hook.agent_store, 'run', calls)
    monkeypatch.setattr(hook, 'worktree', lambda cwd: '/repo')
    monkeypatch.setattr(hook, 'terminal', lambda environ: ['--terminal-kind', 'windows-terminal', '--terminal-id', 'w'])
    environ = {hook.LAUNCH_ENV: 'L1', hook.TITLE_ENV: 'agent#98'}
    hook.start(Host.CLAUDE, 'n1', '/repo/sub', environ)
    assert calls.calls == [
        ('register', '--session', 'claude:n1', '--cwd', '/repo/sub', '--worktree', '/repo', '--launch-id', 'L1'),
        ('register', '--session', 'claude:n1', '--cwd', '/repo/sub', '--worktree', '/repo',
         '--terminal-kind', 'windows-terminal', '--terminal-id', 'w', '--title', 'agent#98'),
    ]
    assert 'L1' in capsys.readouterr().err


def test_the_fallback_registration_carries_the_wrapper_pid_and_started_at(monkeypatch):
    calls = AgentStoreCalls(hook.agent_store.AgentStoreError('not-found', 'no session'),
                            hook.agent_store.AgentStoreError('not-found', 'no launch'), {'session': {'title': None}})
    monkeypatch.setattr(hook.agent_store, 'run', calls)
    monkeypatch.setattr(hook, 'worktree', lambda cwd: None)
    monkeypatch.setattr(hook, 'terminal', lambda environ: [])
    environ = {hook.LAUNCH_ENV: 'L1', hook.WRAPPER_PID_ENV: '4242', hook.WRAPPER_STARTED_AT_ENV: '2026-11-09T00:00:00Z'}
    hook.start(Host.CODEX, 'n1', '/repo/sub', environ)
    assert calls.calls[2][-4:] == ('--wrapper-pid', '4242', '--wrapper-started-at', '2026-11-09T00:00:00Z')


def test_a_session_not_started_by_tj_run_never_carries_wrapper_arguments(monkeypatch):
    calls = AgentStoreCalls({'session': {'title': None}})
    monkeypatch.setattr(hook.agent_store, 'run', calls)
    monkeypatch.setattr(hook, 'worktree', lambda cwd: None)
    monkeypatch.setattr(hook, 'terminal', lambda environ: [])
    environ = {hook.WRAPPER_PID_ENV: '4242', hook.WRAPPER_STARTED_AT_ENV: '2026-11-09T00:00:00Z'}
    hook.start(Host.CLAUDE, 'n1', '/repo/sub', environ)
    assert calls.calls == [('register', '--session', 'claude:n1', '--cwd', '/repo/sub')]


def test_a_launch_lookup_failure_other_than_not_found_propagates(monkeypatch):
    calls = AgentStoreCalls(hook.agent_store.AgentStoreError('unexpected', 'boom'))
    monkeypatch.setattr(hook.agent_store, 'run', calls)
    monkeypatch.setattr(hook, 'worktree', lambda cwd: None)
    with pytest.raises(hook.agent_store.AgentStoreError):
        hook.start(Host.CLAUDE, 'n1', '/repo/sub', {hook.LAUNCH_ENV: 'L1'})


def test_main_reads_the_process_environment_and_passes_it_to_start(monkeypatch):
    captured = {}

    def fake_start(host, session_id, cwd, environ):
        captured['environ'] = environ

    monkeypatch.setattr(hook, 'start', fake_start)
    monkeypatch.setattr(sys, 'stdin', io.StringIO('{"session_id": "n1"}'))
    hook.main(['start', '--host', 'claude'])
    assert captured['environ'] is os.environ


if __name__ == '__main__':
    unittest.main()
