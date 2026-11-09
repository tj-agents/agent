"""The Claude and Codex handoff launchers, with `agent_cli.launch` faked so no terminal tab ever opens."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / 'plugins/agent/.agents/agent'
SHOWN = {'launch': {'launch_id': 'L1', 'state': 'running'}, 'session': {'native_id': 'thread-1'}}


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class LauncherCase(unittest.TestCase):
    script = ''

    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='handoff launcher ')
        self.addCleanup(temp.cleanup)
        self.directory = Path(temp.name)
        self.prompt = self.directory / 'prompt.md'
        self.prompt.write_text('Do the work.', encoding='utf-8')
        self.cli = load(PACKAGE / 'runtime/agent_cli.py', f'agent_cli_{self.id()}')
        self.launch = mock.Mock(return_value=SHOWN)
        self.cli.launch = self.launch
        self.cli.resolve_item = mock.Mock(side_effect=lambda item, title, cwd, parent, fields: item or 'auto-item')
        session = {name: value for name, value in os.environ.items()
                   if name not in ('TJ_RUN_LAUNCH_ID', 'CLAUDE_CODE_SESSION_ID', 'CODEX_THREAD_ID')}
        patcher = mock.patch.dict(os.environ, session, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.launcher = load(PACKAGE / f'resources/handoff/{self.script}', f'{self.script}_{self.id()}')
        self.launcher._load_agent_cli = lambda: self.cli

    def run_launcher(self, *arguments):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
                mock.patch.object(self.cli.agent_store, 'run', return_value={'session': {'title': 'parent tab'}}):
            code = self.launcher.main(['--working-directory', str(self.directory), '--prompt-path', str(self.prompt),
                                       '--title', 'agent#98 review', *arguments])
        return code, out.getvalue(), err.getvalue()


class ClaudeLauncherTests(LauncherCase):
    script = 'launch_claude.py'

    def test_the_title_names_the_session_and_item_and_parent_reach_the_launch(self):
        code, out, _ = self.run_launcher('--model', 'claude-opus-5-5', '--item', '98', '--parent', 'codex:p')
        self.assertEqual(0, code)
        host, directory, title, arguments = self.launch.call_args.args
        self.assertEqual((self.cli.Host.CLAUDE, 'agent#98 review'), (host, title))
        self.assertEqual(['--name', 'agent#98 review', '--model', 'claude-opus-5-5'], arguments[:4])
        self.assertIn('"parent tab" (codex:p)', arguments[-1])
        self.assertEqual({'item': '98', 'parent': 'codex:p'}, self.launch.call_args.kwargs)
        self.assertEqual('L1', json.loads(out.splitlines()[-1])['launch_id'])

    def test_without_a_parent_the_prompt_has_no_standing_rule(self):
        self.run_launcher('--model', 'claude-opus-5-5')
        self.assertNotIn('Standing rule', self.launch.call_args.args[3][-1])
        self.assertIsNone(self.launch.call_args.kwargs['parent'])

    def test_omitting_item_uses_what_resolve_item_creates(self):
        self.run_launcher('--model', 'claude-opus-5-5')
        self.cli.resolve_item.assert_called_once_with(None, 'agent#98 review', self.directory, None, [])
        self.assertEqual('auto-item', self.launch.call_args.kwargs['item'])

    def test_item_fields_reach_the_created_work_item(self):
        self.run_launcher('--model', 'claude-opus-5-5', '--item-field', 'attribution=TechPlatform')
        self.assertEqual([('attribution', 'TechPlatform')], self.cli.resolve_item.call_args.args[4])

    def test_the_calling_session_becomes_the_parent_by_default(self):
        os.environ['CLAUDE_CODE_SESSION_ID'] = 'caller'
        self.run_launcher('--model', 'claude-opus-5-5')
        self.assertEqual('claude:caller', self.launch.call_args.kwargs['parent'])

    def test_skipping_permissions_leads_the_arguments(self):
        self.run_launcher('--model', 'claude-opus-5-5', '--dangerously-skip-permissions')
        self.assertEqual('--dangerously-skip-permissions', self.launch.call_args.args[3][0])

    def test_a_haiku_model_is_refused_before_any_launch(self):
        code, _, err = self.run_launcher('--model', 'claude-haiku-4-5-20251001')
        self.assertEqual(1, code)
        self.assertIn('Haiku', err)
        self.launch.assert_not_called()

    def test_an_unconfirmed_launch_exits_3(self):
        self.launch.side_effect = self.cli.LaunchTimeout('tj run did not record launch L1')
        code, _, _ = self.run_launcher('--model', 'claude-opus-5-5')
        self.assertEqual(3, code)


class CodexLauncherTests(LauncherCase):
    script = 'launch_codex.py'

    def setUp(self):
        super().setUp()
        self.adapter = self.cli.host_launch(self.cli.Host.CODEX)
        patcher = mock.patch.object(self.adapter, 'resolve_executable', return_value=('/bin/codex', {}))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.deliver = mock.patch.object(self.adapter, 'deliver').start()
        self.addCleanup(mock.patch.stopall)

    def test_without_an_effort_the_prompt_rides_the_daemon_attached_command_line(self):
        code, _, _ = self.run_launcher('--model', 'gpt-6', '--item', '98')
        self.assertEqual(0, code)
        arguments = self.launch.call_args.args[3]
        self.assertEqual(['--cd', str(self.directory), '--model', 'gpt-6'], arguments[:4])
        self.assertNotIn('-c', arguments)
        self.assertFalse(self.launch.call_args.kwargs['want_session'])
        self.deliver.assert_not_called()

    def test_an_effort_is_delivered_as_the_first_turn_through_the_daemon(self):
        self.run_launcher('--model', 'gpt-6', '--reasoning-effort', 'high', '--parent', 'claude:p')
        arguments = self.launch.call_args.args[3]
        self.assertEqual(['--cd', str(self.directory), '--model', 'gpt-6'], arguments)
        self.assertTrue(self.launch.call_args.kwargs['want_session'])
        session, prompt, model, effort = self.deliver.call_args.args
        self.assertEqual(('thread-1', 'gpt-6', 'high'), (session, model, effort))
        self.assertIn('"parent tab" (claude:p)', prompt)

    def test_lane_l7_is_refused(self):
        code, _, _ = self.run_launcher('--lane', 'L7')
        self.assertEqual(1, code)
        self.launch.assert_not_called()

    def test_a_blank_title_is_refused(self):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = self.launcher.main(['--working-directory', str(self.directory), '--prompt-path',
                                       str(self.prompt), '--title', ' '])
        self.assertEqual(1, code)
        self.launch.assert_not_called()


if __name__ == '__main__':
    unittest.main()
