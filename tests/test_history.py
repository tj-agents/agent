"""Synthetic transcripts only; no normal-profile history access."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('history', ROOT / 'plugins/agent/.agents/agent/runtime/history.py')
HISTORY = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(HISTORY)


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='synthetic history ')
        self.addCleanup(self.temp.cleanup)
        # Resolved so it matches a --worktree argument after query() resolves it too (Windows
        # short/long-name spelling, e.g. TOMMYS~1 vs TommySeery, would otherwise never line up).
        self.root = Path(self.temp.name).resolve()
        self.cwd = self.root / 'project with spaces'
        self.cwd.mkdir()

    def write(self, relative, events):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('\n'.join(json.dumps(e) for e in events) + '\npartial{', encoding='utf-8')
        return path

    def test_codex_metadata_messages_and_resume(self):
        self.write('codex/2026/rollout.jsonl', [
            dict(type='session_meta', payload=dict(id='codex-id', cwd=str(self.cwd), source='cli', git={'branch':'feature/test'})),
            dict(type='response_item', payload=dict(type='message', role='user', content=[dict(type='input_text', text='Discuss a.b')]))])
        self.write('codex/2026/worker.jsonl', [
            dict(type='session_meta', payload=dict(id='worker', cwd=str(self.cwd), source={'subagent':{}})),
            dict(type='response_item', payload=dict(type='message', role='user', content='worker data'))])
        result = HISTORY.query(self.root / 'codex', 'codex', cwd=self.cwd)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]['resume'], 'codex resume codex-id')
        self.assertEqual(result[0]['branch'], 'feature/test')
        self.assertEqual(HISTORY.query(self.root / 'codex', 'codex', pattern='a.b')[0]['hits'], 1)
        self.assertEqual(HISTORY.query(self.root / 'codex', 'codex', pattern='a.b', cwd=self.root), [])

    def test_claude_format_and_literal_search(self):
        self.write('claude/project/session-id.jsonl', [
            dict(type='user', sessionId='session-id', cwd=str(self.cwd), message={'content':'Discuss axb'}),
            dict(type='assistant', message={'content':[dict(type='text', text='answer')]}),
            dict(type='user', isSidechain=True, message={'content':'a.b hidden'})])
        self.write('claude/project/subagents/ignored.jsonl', [dict(type='user', message={'content':'a.b'})])
        self.assertEqual(HISTORY.query(self.root / 'claude', 'claude', pattern='a.b'), [])
        result = HISTORY.query(self.root / 'claude', 'claude', pattern='a.b', regex=True, show_lines=True)
        self.assertEqual(result[0]['resume'], 'claude --resume session-id')
        self.assertEqual(result[0]['matches'][0]['text'], 'Discuss axb')

    def test_packaged_cli_missing_root_and_invalid_pattern_are_clear(self):
        package = self.root / 'isolated package'
        shutil.copytree(ROOT / 'plugins/agent/.agents/agent/runtime', package / '.agents/agent/runtime')
        script = package / '.agents/agent/runtime/history.py'
        result = subprocess.run([sys.executable, '-B', str(script), 'recent', '--host', 'codex',
                                 '--history-root', str(self.root / 'missing')], cwd=self.cwd,
                                text=True, capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('No codex history directory', result.stderr)
        with self.assertRaisesRegex(ValueError, 'positive'):
            HISTORY.query(self.root, 'codex', count=0)

    def test_last_activity_uses_record_timestamp_not_mtime(self):
        self.write('claude/project/old-ts.jsonl', [
            dict(type='user', sessionId='old-ts', cwd=str(self.cwd), timestamp='2020-01-01T00:00:00Z',
                 message={'content': 'first'}),
            dict(type='assistant', timestamp='2020-01-01T00:05:00Z', message={'content': [dict(type='text', text='answer')]})])
        result = HISTORY.query(self.root / 'claude', 'claude', cwd=self.cwd)
        self.assertEqual(result[0]['last_activity'], '2020-01-01T00:05:00Z')
        self.assertNotEqual(result[0]['last_activity'], result[0]['modified'])

    def test_worktree_matches_recorded_cwd(self):
        worktree = self.root / 'repo' / '.worktrees' / 'Feature-X'
        self.write('claude/project/cwd-match.jsonl', [
            dict(type='user', sessionId='cwd-match', cwd=str(worktree / 'nested'),
                 timestamp='2026-01-01T00:00:00Z', message={'content': 'working here'})])
        result = HISTORY.query(self.root / 'claude', 'claude', worktree=worktree)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]['session'], 'cwd-match')
        self.assertEqual(result[0]['matched_by'], 'cwd')

    def test_worktree_matches_claude_tool_call_from_other_cwd(self):
        worktree = self.root / 'repo' / '.worktrees' / 'Feature-Y'
        elsewhere = self.root / 'main-checkout'
        self.write('claude/project/tool-match.jsonl', [
            dict(type='user', sessionId='tool-match', cwd=str(elsewhere),
                 timestamp='2026-01-02T00:00:00Z', message={'content': 'please check the worktree'}),
            dict(type='assistant', cwd=str(elsewhere), timestamp='2026-01-02T00:01:00Z',
                 message={'content': [dict(type='tool_use', name='Bash', id='t1',
                                            input={'command': f'git -C "{worktree}" status'})]})])
        result = HISTORY.query(self.root / 'claude', 'claude', worktree=worktree)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]['session'], 'tool-match')
        self.assertEqual(result[0]['matched_by'], 'tool')

    def test_worktree_matches_codex_tool_call_workdir_from_other_cwd(self):
        worktree = self.root / 'repo' / '.worktrees' / 'Feature-Z'
        elsewhere = self.root / 'other-checkout'
        self.write('codex/2026/tool-match.jsonl', [
            dict(type='session_meta', payload=dict(id='codex-tool-match', cwd=str(elsewhere), source='cli')),
            dict(type='response_item', payload=dict(type='function_call', name='shell', call_id='c1',
                                                      arguments=json.dumps({'command': ['bash', '-lc', 'ls'],
                                                                            'workdir': str(worktree)}))),
            dict(type='response_item', payload=dict(type='message', role='user',
                                                      timestamp='2026-01-03T00:00:00Z',
                                                      content=[dict(type='input_text', text='look in that worktree')]))])
        result = HISTORY.query(self.root / 'codex', 'codex', worktree=worktree)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]['session'], 'codex-tool-match')
        self.assertEqual(result[0]['matched_by'], 'tool')

    def test_worktree_boundary_does_not_match_longer_sibling_name(self):
        worktree = self.root / 'repo' / '.worktrees' / 'Fix-A'
        sibling = self.root / 'repo' / '.worktrees' / 'Fix-AB'
        elsewhere = self.root / 'main-checkout'
        self.write('claude/project/sibling.jsonl', [
            dict(type='user', sessionId='sibling', cwd=str(elsewhere), timestamp='2026-01-04T00:00:00Z',
                 message={'content': 'checking the other worktree'}),
            dict(type='assistant', cwd=str(elsewhere), timestamp='2026-01-04T00:01:00Z',
                 message={'content': [dict(type='tool_use', name='Bash', id='t1',
                                            input={'command': f'git -C "{sibling}" status'})]})])
        result = HISTORY.query(self.root / 'claude', 'claude', worktree=worktree)
        self.assertEqual(result, [])

    def test_worktree_normalizes_case_and_separators_on_windows(self):
        if os.name != 'nt':
            self.skipTest('case/separator folding only applies on Windows')
        worktree = self.root / 'Repo' / 'WT' / 'MixedCase'
        recorded_cwd = str(worktree).upper().replace('\\', '/')
        self.write('claude/project/mixed-case.jsonl', [
            dict(type='user', sessionId='mixed-case', cwd=recorded_cwd, timestamp='2026-01-05T00:00:00Z',
                 message={'content': 'hello'})])
        result = HISTORY.query(self.root / 'claude', 'claude', worktree=str(worktree).lower())
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]['session'], 'mixed-case')

    def test_worktree_results_sort_by_last_activity_descending(self):
        worktree = self.root / 'repo' / '.worktrees' / 'Feature-Order'
        older = self.write('claude/project/older.jsonl', [
            dict(type='user', sessionId='older', cwd=str(worktree), timestamp='2026-01-01T00:00:00Z',
                 message={'content': 'first session'})])
        newer = self.write('claude/project/newer.jsonl', [
            dict(type='user', sessionId='newer', cwd=str(worktree), timestamp='2026-01-10T00:00:00Z',
                 message={'content': 'second session'})])
        # Make the older session's file the most recently modified, to prove sorting uses
        # last_activity from the records, not file mtime.
        os.utime(newer, (1, 1))
        os.utime(older, (2000000000, 2000000000))
        result = HISTORY.query(self.root / 'claude', 'claude', worktree=worktree)
        self.assertEqual([item['session'] for item in result], ['newer', 'older'])

    def test_worktree_resolves_relative_path(self):
        worktree_abs = self.root / 'repo' / '.worktrees' / 'Feature-Rel'
        worktree_abs.mkdir(parents=True)
        self.write('claude/project/rel-match.jsonl', [
            dict(type='user', sessionId='rel-match', cwd=str(worktree_abs), timestamp='2026-01-06T00:00:00Z',
                 message={'content': 'hi'})])
        original = Path.cwd()
        self.addCleanup(os.chdir, original)
        os.chdir(worktree_abs)
        result = HISTORY.query(self.root / 'claude', 'claude', worktree=Path('.'))
        self.assertEqual([item['session'] for item in result], ['rel-match'])

    def test_worktree_keeps_the_given_spelling_when_resolve_changes_it(self):
        worktree = self.root / 'mapped' / '.worktrees' / 'Feature-Map'
        self.write('claude/project/mapped.jsonl', [
            dict(type='user', sessionId='mapped', cwd=str(worktree), timestamp='2026-01-08T00:00:00Z',
                 message={'content': 'on the mapped drive'})])
        with mock.patch.object(HISTORY.Path, 'resolve', return_value=Path('//server/share/elsewhere')):
            result = HISTORY.query(self.root / 'claude', 'claude', worktree=worktree)
        self.assertEqual([item['session'] for item in result], ['mapped'])

    def test_worktree_given_spelling_collapses_parent_segments(self):
        worktree = self.root / 'mapped' / 'Feature-Up'
        start = self.root / 'mapped' / 'sub'
        start.mkdir(parents=True)
        self.write('claude/project/up.jsonl', [
            dict(type='user', sessionId='up', cwd=str(worktree), timestamp='2026-01-09T00:00:00Z',
                 message={'content': 'one level up'})])
        self.addCleanup(os.chdir, Path.cwd())
        os.chdir(start)
        with mock.patch.object(HISTORY.Path, 'resolve', return_value=Path('//server/share/elsewhere')):
            result = HISTORY.query(self.root / 'claude', 'claude', worktree=Path('..') / 'Feature-Up')
        self.assertEqual([item['session'] for item in result], ['up'])

    def test_worktree_matches_git_bash_drive_spelling_on_windows(self):
        if os.name != 'nt':
            self.skipTest('MSYS path folding only applies on Windows')
        worktree = self.root / 'repo' / '.worktrees' / 'Feature-Bash'
        elsewhere = self.root / 'main-checkout'
        drive = worktree.drive.rstrip(':').lower()
        msys_path = f'/{drive}' + str(worktree)[len(worktree.drive):].replace('\\', '/')
        self.write('claude/project/bash-match.jsonl', [
            dict(type='user', sessionId='bash-match', cwd=str(elsewhere), timestamp='2026-01-07T00:00:00Z',
                 message={'content': 'checking via bash'}),
            dict(type='assistant', cwd=str(elsewhere), timestamp='2026-01-07T00:01:00Z',
                 message={'content': [dict(type='tool_use', name='Bash', id='t1',
                                            input={'command': f'git -C "{msys_path}" status'})]})])
        result = HISTORY.query(self.root / 'claude', 'claude', worktree=worktree)
        self.assertEqual([item['session'] for item in result], ['bash-match'])

    def test_worktree_boundary_matches_shell_punctuation(self):
        worktree = self.root / 'repo' / '.worktrees' / 'Fix-B'
        elsewhere = self.root / 'main-checkout'
        commands = [
            f'cd {worktree}; git status',
            f'(Get-Item {worktree})',
            f'--git-dir={worktree}',
        ]
        for index, command in enumerate(commands):
            self.write(f'claude/project/punct-{index}.jsonl', [
                dict(type='user', sessionId=f'punct-{index}', cwd=str(elsewhere),
                     timestamp='2026-01-08T00:00:00Z', message={'content': 'poke'}),
                dict(type='assistant', cwd=str(elsewhere), timestamp='2026-01-08T00:01:00Z',
                     message={'content': [dict(type='tool_use', name='Bash', id='t1',
                                                input={'command': command})]})])
        result = HISTORY.query(self.root / 'claude', 'claude', worktree=worktree)
        self.assertEqual({item['session'] for item in result}, {'punct-0', 'punct-1', 'punct-2'})

    def test_worktree_boundary_excludes_dotted_sibling_file(self):
        worktree = self.root / 'repo' / '.worktrees' / 'Fix-A'
        elsewhere = self.root / 'main-checkout'
        backup = str(worktree) + '.bak'
        self.write('claude/project/dotted-sibling.jsonl', [
            dict(type='user', sessionId='dotted-sibling', cwd=str(elsewhere),
                 timestamp='2026-01-09T00:00:00Z', message={'content': 'restoring a backup'}),
            dict(type='assistant', cwd=str(elsewhere), timestamp='2026-01-09T00:01:00Z',
                 message={'content': [dict(type='tool_use', name='Bash', id='t1',
                                            input={'command': f'cp "{backup}" /tmp'})]})])
        result = HISTORY.query(self.root / 'claude', 'claude', worktree=worktree)
        self.assertEqual(result, [])

    def test_worktree_sort_tolerates_naive_timestamp(self):
        worktree = self.root / 'repo' / '.worktrees' / 'Feature-Naive'
        self.write('claude/project/naive.jsonl', [
            dict(type='user', sessionId='naive', cwd=str(worktree), timestamp='2026-01-02T00:00:00',
                 message={'content': 'no offset here'})])
        self.write('claude/project/aware.jsonl', [
            dict(type='user', sessionId='aware', cwd=str(worktree), timestamp='2026-01-01T00:00:00Z',
                 message={'content': 'has an offset'})])
        result = HISTORY.query(self.root / 'claude', 'claude', worktree=worktree)
        self.assertEqual([item['session'] for item in result], ['naive', 'aware'])

    def test_existing_selection_modes_unaffected_by_worktree_option(self):
        self.write('claude/project/plain.jsonl', [
            dict(type='user', sessionId='plain', cwd=str(self.cwd), message={'content': 'hi'})])
        result = HISTORY.query(self.root / 'claude', 'claude', cwd=self.cwd)
        self.assertEqual(len(result), 1)
        self.assertNotIn('matched_by', result[0])
        self.assertIn('last_activity', result[0])


    def test_search_skill_doc_does_not_contradict_worktree_tool_matching(self):
        text = (ROOT / 'plugins/agent/.agents/agent/utility/search/SKILL.md').read_text(encoding='utf-8')
        self.assertNotIn('does not search tool payloads', text)
        self.assertIn('tool-call input', text)


if __name__ == '__main__':
    unittest.main()
