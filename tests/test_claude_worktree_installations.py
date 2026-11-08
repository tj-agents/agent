import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from git_isolation import isolate_git_config, without_git_config


SCRIPTS = Path(__file__).resolve().parents[1] / 'plugins/agent/.agents/agent/runtime'
sys.path.insert(0, str(SCRIPTS))
from claude import plugins as CLAUDE  # noqa: E402
import required_plugins  # noqa: E402


IDENTITY = 'agent@tj-agents'


class ClaudeWorktreeInstallations(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(prefix='Claude worktrees with spaces ')
        cls.addClassCleanup(cls.temporary.cleanup)
        cls.root = Path(cls.temporary.name).resolve()
        cls.primary = cls.root / 'primary'
        cls.primary.mkdir()
        cls.nested = cls.primary / '.worktrees/feature'
        cls.sibling = cls.root / 'sibling'
        cls.foreign = cls.primary / 'different-repository'
        environment = without_git_config(os.environ)
        for name in ('GIT_DIR', 'GIT_WORK_TREE', 'GIT_COMMON_DIR', 'GIT_INDEX_FILE'):
            environment.pop(name, None)
        commands = [
            ['init', '--quiet'],
            ['-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid',
             '-c', 'commit.gpgsign=false', '-c', 'core.hooksPath=' + str(cls.root / 'no-hooks'),
             'commit', '--quiet', '--allow-empty', '-m', 'fixture'],
            ['worktree', 'add', '--quiet', '--detach', str(cls.nested), 'HEAD'],
            ['worktree', 'add', '--quiet', '--detach', str(cls.sibling), 'HEAD'],
            ['init', '--quiet', str(cls.foreign)],
        ]
        for arguments in commands:
            subprocess.run(['git', *arguments], cwd=cls.primary, env=environment, check=True,
                           capture_output=True, text=True, timeout=15)
        for project in (cls.primary, cls.nested, cls.sibling):
            settings = project / '.claude/settings.json'
            settings.parent.mkdir()
            settings.write_text(json.dumps({'enabledPlugins': {IDENTITY: True}}), encoding='utf-8')

    def setUp(self):
        isolate_git_config()

    def entry(self, owner, version='old', **extra):
        return {'id': IDENTITY, 'scope': 'project', 'projectPath': str(owner), 'version': version,
                'installPath': str(self.root / version), 'enabled': True, **extra}

    def test_enabled_roots_matches_the_winning_native_registration(self):
        for first, current in ((self.primary, self.nested), (self.nested, self.primary),
                               (self.sibling, self.nested)):
            with self.subTest(first=first, current=current):
                winner = self.entry(first, 'new')
                rows = [self.entry(self.foreign), winner, self.entry(current)]
                with mock.patch.object(required_plugins, 'readable_payload', return_value=True):
                    self.assertEqual({IDENTITY: self.root / 'new'},
                                     required_plugins.enabled_roots(CLAUDE, current, rows))

    def test_a_broken_first_installation_does_not_fall_back_to_a_shadowed_installation(self):
        for broken in ({'enabled': False}, {'errors': ['broken hook']}):
            with self.subTest(broken=broken), mock.patch.object(required_plugins, 'readable_payload', return_value=True):
                rows = [self.entry(self.primary, **broken), self.entry(self.nested, 'new')]
                self.assertEqual({}, required_plugins.enabled_roots(CLAUDE, self.nested, rows))

    def test_dangling_foreign_worktree_does_not_block_a_healthy_repository(self):
        with tempfile.TemporaryDirectory(dir=self.root) as temporary:
            dangling = Path(temporary)
            (dangling / '.git').write_text('gitdir: ' + str(dangling / 'deleted-metadata'), encoding='utf-8')
            rows = [self.entry(dangling), self.entry(self.primary, 'new'), self.entry(self.nested)]
            with mock.patch.object(required_plugins, 'readable_payload', return_value=True):
                self.assertEqual({IDENTITY: self.root / 'new'},
                                 required_plugins.enabled_roots(CLAUDE, self.nested, rows))
            with self.assertRaises(required_plugins.RequiredPluginsError):
                required_plugins.enabled_roots(CLAUDE, dangling, rows)

    def test_empty_inventory_does_not_hide_missing_plugins_behind_a_git_error(self):
        with mock.patch.object(CLAUDE, 'git_common_directory', side_effect=AssertionError('unneeded Git query')):
            self.assertEqual({}, required_plugins.enabled_roots(CLAUDE, self.nested, []))


if __name__ == '__main__':
    unittest.main()
