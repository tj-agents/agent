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
    monkeypatch.setattr(hook.codex_processes, 'record', lambda *_: None)
    # main() must never inspect the developer's installed native hosts in these tests.
    monkeypatch.setattr(hook.session_plugins, 'check', lambda *_args, **_kwargs: 'Fixture plugin diagnostic')


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
    def start(self, host, environ, title=None, launch=None):
        def response(*arguments: str) -> dict[str, object]:
            if arguments[:2] == ('show', '--launch-id'):
                return {'launch': {'launch_id': launch, 'host': str(host),
                                   'state': 'active', 'session': None}, 'session': None}
            if arguments[0] == 'show':
                raise hook.agent_store.AgentStoreError('not-found', 'new native session')
            return {'session': {'title': title}}

        run = mock.Mock(side_effect=response)
        adapter = mock.Mock()
        with mock.patch.object(hook.agent_store, 'run', run), \
                mock.patch.object(hook.owned_launch, 'current', return_value=launch), \
                mock.patch.object(hook, 'worktree', return_value='/repo'), \
                mock.patch('agent_cli.host_launch', return_value=adapter):
            hook.start(host, 'n1', '/repo/sub', environ=environ)
        return list(run.call_args.args), adapter

    def test_a_tj_run_session_binds_to_its_launch_and_a_codex_thread_takes_its_title(self):
        arguments, adapter = self.start(Host.CODEX, {hook.LAUNCH_ENV: 'L1', 'WT_SESSION': 'w'}, title='agent#98',
                                        launch='L1')
        self.assertEqual(['register', '--session', 'codex:n1', '--cwd', '/repo/sub', '--worktree', '/repo',
                          '--launch-id', 'L1', '--require-unowned'], arguments)
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

    def test_prompts_and_tool_uses_are_not_hook_events(self):
        for event in ('prompt', 'idle'):
            with self.subTest(event=event), self.assertRaises(SystemExit):
                self.main(event, '--host', 'claude')

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
    run.assert_called_once_with('end', '--session', 'claude:n1', '--reason', reason)


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


def test_an_unrecorded_launch_does_not_register_by_inherited_title(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = AgentStoreCalls(hook.agent_store.AgentStoreError('not-found', 'no launch'))
    monkeypatch.setattr(hook.agent_store, 'run', calls)
    monkeypatch.setattr(hook.owned_launch, 'current', lambda: 'L1')
    monkeypatch.setattr(hook, 'worktree', lambda cwd: '/repo')
    monkeypatch.setattr(hook, 'terminal', lambda environ: ['--terminal-kind', 'windows-terminal', '--terminal-id', 'w'])
    environ = {hook.LAUNCH_ENV: 'L1', hook.TITLE_ENV: 'agent#98'}
    with pytest.raises(hook.agent_store.AgentStoreError, match='no launch'):
        hook.start(Host.CLAUDE, 'n1', '/repo/sub', environ)
    assert calls.calls == [('show', '--launch-id', 'L1')]


def test_an_unrecorded_codex_launch_does_not_copy_its_inherited_wrapper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = AgentStoreCalls(hook.agent_store.AgentStoreError('not-found', 'no session'),
                            hook.agent_store.AgentStoreError('not-found', 'no launch'))
    monkeypatch.setattr(hook.agent_store, 'run', calls)
    monkeypatch.setattr(hook.owned_launch, 'current', lambda: 'L1')
    monkeypatch.setattr(hook, 'worktree', lambda cwd: None)
    monkeypatch.setattr(hook, 'terminal', lambda environ: [])
    environ = {hook.LAUNCH_ENV: 'L1', hook.WRAPPER_PID_ENV: '4242', hook.WRAPPER_STARTED_AT_ENV: '2026-11-09T00:00:00Z'}
    with pytest.raises(hook.agent_store.AgentStoreError, match='no launch'):
        hook.start(Host.CODEX, 'n1', '/repo/sub', environ)
    assert calls.calls == [('show', 'codex:n1'), ('show', '--launch-id', 'L1')]


def test_a_session_not_started_by_tj_run_never_carries_wrapper_arguments(monkeypatch):
    calls = AgentStoreCalls({'session': {'title': None}})
    monkeypatch.setattr(hook.agent_store, 'run', calls)
    monkeypatch.setattr(hook, 'worktree', lambda cwd: None)
    monkeypatch.setattr(hook, 'terminal', lambda environ: [])
    environ = {hook.WRAPPER_PID_ENV: '4242', hook.WRAPPER_STARTED_AT_ENV: '2026-11-09T00:00:00Z'}
    hook.start(Host.CLAUDE, 'n1', '/repo/sub', environ)
    assert calls.calls == [('register', '--session', 'claude:n1', '--cwd', '/repo/sub')]


def test_an_inherited_launch_environment_registers_an_unbound_session(monkeypatch):
    calls = AgentStoreCalls({'session': {'title': None}})
    claims = []
    monkeypatch.setattr(hook.agent_store, 'run', calls)
    monkeypatch.setattr(hook, 'worktree', lambda cwd: '/repo')
    monkeypatch.setattr(hook, 'terminal', lambda environ: [])
    monkeypatch.setattr(hook.worktrees, 'claim_started_worktree', lambda *arguments: claims.append(arguments))
    environ = {hook.LAUNCH_ENV: 'parent', hook.WRAPPER_PID_ENV: '4242',
               hook.WRAPPER_STARTED_AT_ENV: '2026-11-09T00:00:00Z'}
    hook.start(Host.CLAUDE, 'n1', '/repo/sub', environ)
    assert calls.calls == [('register', '--session', 'claude:n1', '--cwd', '/repo/sub', '--worktree', '/repo')]
    assert hook.LAUNCH_ENV not in claims[0][3]


def test_a_launch_lookup_failure_other_than_not_found_propagates(monkeypatch):
    calls = AgentStoreCalls(hook.agent_store.AgentStoreError('unexpected', 'boom'))
    monkeypatch.setattr(hook.agent_store, 'run', calls)
    monkeypatch.setattr(hook.owned_launch, 'current', lambda: 'L1')
    monkeypatch.setattr(hook, 'worktree', lambda cwd: None)
    with pytest.raises(hook.agent_store.AgentStoreError):
        hook.start(Host.CLAUDE, 'n1', '/repo/sub', {hook.LAUNCH_ENV: 'L1'})


def unanswered_probe() -> None:
    raise hook.owned_launch.OwnedLaunchError('tj launch did not answer: timed out')


def test_a_launched_session_is_never_registered_unbound_when_tj_launch_cannot_answer(monkeypatch, capsys):
    calls = AgentStoreCalls()
    monkeypatch.setattr(hook.agent_store, 'run', calls)
    monkeypatch.setattr(hook.owned_launch, 'current', unanswered_probe)
    monkeypatch.setattr(hook, 'worktree', lambda cwd: '/repo')
    monkeypatch.setenv(hook.LAUNCH_ENV, 'L1')
    monkeypatch.setenv(hook.TITLE_ENV, 'standards loading')
    monkeypatch.setattr(sys, 'stdin', io.StringIO('{"session_id": "n1", "cwd": "/repo/sub"}'))
    assert hook.main(['start', '--host', 'claude']) == 0
    assert calls.calls == []
    assert 'tj launch did not answer' in capsys.readouterr().err


def test_a_session_outside_tj_run_registers_without_asking_tj_launch(monkeypatch):
    calls = AgentStoreCalls({'session': {'title': None}})
    monkeypatch.setattr(hook.agent_store, 'run', calls)
    monkeypatch.setattr(hook.owned_launch, 'current', unanswered_probe)
    monkeypatch.setattr(hook, 'worktree', lambda cwd: None)
    monkeypatch.setattr(hook, 'terminal', lambda environ: [])
    hook.start(Host.CLAUDE, 'n1', '/repo/sub', {})
    assert calls.calls == [('register', '--session', 'claude:n1', '--cwd', '/repo/sub')]


def test_main_reads_the_process_environment_and_passes_it_to_start(monkeypatch):
    captured = {}

    def fake_start(host, session_id, cwd, environ):
        captured['environ'] = environ

    monkeypatch.setattr(hook, 'start', fake_start)
    monkeypatch.setattr(sys, 'stdin', io.StringIO('{"session_id": "n1"}'))
    hook.main(['start', '--host', 'claude'])
    assert captured['environ'] is os.environ


@pytest.mark.parametrize('host', list(Host))
def test_plugin_warning_reaches_user_and_context_even_when_registration_fails(monkeypatch, capsys, host):
    checked = mock.Mock(return_value='STALE Agent hook; reload plugins')
    monkeypatch.setattr(hook.session_plugins, 'check', checked)
    monkeypatch.setattr(hook, 'start', mock.Mock(side_effect=RuntimeError('registration unavailable')))
    monkeypatch.setattr(sys, 'stdin', io.StringIO('{"session_id": "n1", "cwd": "/repo"}'))
    monkeypatch.delenv(hook.LAUNCH_ENV, raising=False)
    assert hook.main(['start', '--host', host.value]) == 0
    output = capsys.readouterr()
    assert 'registration unavailable' in output.err
    assert json.loads(output.out) == {
        'systemMessage': 'STALE Agent hook; reload plugins',
        'hookSpecificOutput': {
            'hookEventName': 'SessionStart', 'additionalContext': 'STALE Agent hook; reload plugins',
        },
    }
    assert hook.__file__ is not None
    checked.assert_called_once_with(host, Path('/repo'), executing_file=Path(hook.__file__))


def test_unexpected_diagnostic_failure_warns_and_still_registers(monkeypatch, capsys):
    registered = mock.Mock()
    monkeypatch.setattr(hook.session_plugins, 'check', mock.Mock(side_effect=RuntimeError('broken probe')))
    monkeypatch.setattr(hook, 'start', registered)
    monkeypatch.setattr(sys, 'stdin', io.StringIO('{"session_id": "n1"}'))
    assert hook.main(['start', '--host', 'claude']) == 0
    warning = json.loads(capsys.readouterr().out)
    assert 'broken probe' in warning['systemMessage']
    assert 'loaded plugin versions remain unverified' in warning['systemMessage']
    assert set(warning) == {'systemMessage', 'hookSpecificOutput'}
    registered.assert_called_once()


def test_start_checks_plugins_even_without_a_registration_id(monkeypatch, capsys):
    checked = mock.Mock(return_value='unverified')
    monkeypatch.setattr(hook.session_plugins, 'check', checked)
    monkeypatch.setattr(sys, 'stdin', io.StringIO('{}'))
    assert hook.main(['start', '--host', 'codex']) == 0
    checked.assert_called_once()
    assert json.loads(capsys.readouterr().out)['systemMessage'] == 'unverified'


def test_end_never_checks_or_emits_plugin_inventory(monkeypatch, capsys):
    checked = mock.Mock()
    monkeypatch.setattr(hook.session_plugins, 'check', checked)
    monkeypatch.setattr(sys, 'stdin', io.StringIO('{"session_id": "n1"}'))
    assert hook.main(['end', '--host', 'codex']) == 0
    checked.assert_not_called()
    assert not capsys.readouterr().out


if __name__ == '__main__':
    unittest.main()
