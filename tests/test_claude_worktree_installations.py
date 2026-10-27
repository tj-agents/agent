import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


SCRIPTS = Path(__file__).resolve().parents[1] / 'plugins/agent/.agents/agent/runtime'
sys.path.insert(0, str(SCRIPTS))
from claude import plugins as CLAUDE  # noqa: E402
import plugin_refresh  # noqa: E402
import required_plugins  # noqa: E402
from tj_agent.plugins import sources


def load(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


REFRESH = load('reload_plugins')
IDENTITY = 'agent@tj-agents'
DEPENDENCY = 'agent-store@tj-agents'


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
        environment = dict(os.environ, GIT_CONFIG_COUNT='0')
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
        environment = mock.patch.dict(os.environ, {'GIT_CONFIG_COUNT': '0'})
        environment.start()
        self.addCleanup(environment.stop)
        self.commands = []

    def entry(self, owner, version='old', **extra):
        return {'id': IDENTITY, 'scope': 'project', 'projectPath': str(owner), 'version': version,
                'installPath': str(self.root / version), 'enabled': True, **extra}

    def test_repository_discovery_finds_nested_and_sibling_worktrees_only(self):
        self.assertEqual({self.primary, self.nested, self.sibling}, set(CLAUDE.repository_projects([self.sibling])))

    def native(self, command, cwd):
        self.commands.append((command, Path(cwd)))
        if command[1:3] == ['plugin', 'list']:
            return json.dumps(self.rows)
        if command[1:3] == ['plugin', 'install']:
            self.fail('An existing repository installation must not be installed again per worktree')
        if command[1:3] == ['plugin', 'update']:
            first = next(row for row in self.rows if Path(row['projectPath']) != self.foreign)
            first['version'] = 'new'
            return json.dumps({'updateOutcome': 'updated', 'newVersion': 'new'})
        return ''

    def test_refresh_batches_the_current_repository_without_a_native_update_per_worktree(self):
        for first, current in ((self.primary, self.nested), (self.nested, self.primary),
                               (self.sibling, self.nested)):
            with self.subTest(first=first, current=current):
                self.commands = []
                other = self.nested if first != self.nested else self.primary
                self.rows = [self.entry(self.foreign), self.entry(first), self.entry(other)]
                with mock.patch.object(CLAUDE, 'synchronize', return_value=[IDENTITY]) as synchronize, \
                        mock.patch.object(CLAUDE, 'refresh_heads', return_value=set()), \
                        mock.patch.object(plugin_refresh, 'run', side_effect=self.native):
                    self.assertEqual([IDENTITY], REFRESH.refresh(CLAUDE, 'claude', current, {}, [IDENTITY]))
                synchronize.assert_called_once_with([current], identities={IDENTITY}, force=True)
                self.assertEqual([], self.commands)

    def test_admission_and_relaunch_use_the_same_registration_as_native_update(self):
        for first, current in ((self.primary, self.nested), (self.nested, self.primary),
                               (self.sibling, self.nested)):
            with self.subTest(first=first, current=current):
                winner = self.entry(first, 'new')
                rows = [self.entry(self.foreign), winner, self.entry(current)]
                with mock.patch.object(required_plugins, 'readable_payload', return_value=True):
                    self.assertEqual({IDENTITY: self.root / 'new'},
                                     required_plugins.enabled_roots(CLAUDE, current, rows))
                self.assertIs(winner, CLAUDE.effective_installation(rows, current))

    def test_a_broken_first_installation_does_not_fall_back_to_a_shadowed_installation(self):
        for broken in ({'enabled': False}, {'errors': ['broken hook']}):
            with self.subTest(broken=broken), mock.patch.object(required_plugins, 'readable_payload', return_value=True):
                rows = [self.entry(self.primary, **broken), self.entry(self.nested, 'new')]
                self.assertEqual({}, required_plugins.enabled_roots(CLAUDE, self.nested, rows))

    def test_local_and_project_scopes_keep_separate_first_registrations(self):
        rows = [self.entry(self.primary), self.entry(self.sibling, 'new', scope='local'),
                self.entry(self.nested), self.entry(self.nested, scope='local')]
        self.assertEqual([('local', self.sibling), ('project', self.primary)],
                         CLAUDE.update_targets(IDENTITY, self.nested, rows))

    def test_dangling_foreign_worktree_does_not_block_a_healthy_repository(self):
        with tempfile.TemporaryDirectory(dir=self.root) as temporary:
            dangling = Path(temporary)
            (dangling / '.git').write_text('gitdir: ' + str(dangling / 'deleted-metadata'), encoding='utf-8')
            rows = [self.entry(dangling), self.entry(self.primary, 'new'), self.entry(self.nested)]
            self.assertEqual([('project', self.primary)], CLAUDE.update_targets(IDENTITY, self.nested, rows))
            with mock.patch.object(required_plugins, 'readable_payload', return_value=True):
                self.assertEqual({IDENTITY: self.root / 'new'},
                                 required_plugins.enabled_roots(CLAUDE, self.nested, rows))
            self.assertEqual('new', CLAUDE.effective_installation(rows, self.nested)['version'])
            with self.assertRaises(required_plugins.RequiredPluginsError):
                required_plugins.enabled_roots(CLAUDE, dangling, rows)

    def test_empty_inventory_does_not_hide_missing_plugins_behind_a_git_error(self):
        with mock.patch.object(CLAUDE, 'git_common_directory', side_effect=AssertionError('unneeded Git query')):
            self.assertEqual({}, required_plugins.enabled_roots(CLAUDE, self.nested, []))

    def test_update_first_installs_newly_declared_dependencies(self):
        dependency = ('base@tj-agents', 'project', self.primary)
        after = [
            {'id': 'base@tj-agents', 'scope': 'project', 'projectPath': str(self.primary),
             'version': 'dependency', 'enabled': True},
            self.entry(self.primary, 'new'),
        ]
        calls = []

        def run(command, cwd):
            calls.append((command, Path(cwd)))
            if command[1:3] == ['plugin', 'update']:
                return json.dumps({'updateOutcome': 'updated', 'newVersion': 'new'})
            if command[1:3] == ['plugin', 'list']:
                return json.dumps(after)
            return ''

        with mock.patch.object(CLAUDE, 'dependency_installs', return_value=[dependency]), \
                mock.patch.object(plugin_refresh, 'run', side_effect=run):
            self.assertEqual(['base@tj-agents', IDENTITY], CLAUDE.apply(
                'claude', self.primary, [], [(IDENTITY, 'project', self.primary)]))
        self.assertEqual([
            ['plugin', 'install', 'base@tj-agents', '--scope', 'project'],
            ['plugin', 'update', IDENTITY, '--scope', 'project', '--json'],
        ], [command[1:] for command, _ in calls[:2]])
        self.assertEqual([self.primary], [cwd for command, cwd in calls if command[2] == 'install'])

    def test_user_dependencies_cannot_borrow_project_or_local_installations(self):
        dependencies = {IDENTITY: ['base@tj-agents']}
        for scope in ('project', 'local'):
            rows = [{'id': 'base@tj-agents', 'scope': scope, 'projectPath': str(self.primary), 'enabled': True}]
            with self.subTest(scope=scope):
                self.assertEqual([], CLAUDE.missing_dependencies(IDENTITY, 'project', self.nested, dependencies, rows))
                self.assertEqual(['base@tj-agents'], CLAUDE.missing_dependencies(
                    IDENTITY, 'user', self.nested, dependencies, rows))
        with mock.patch.object(CLAUDE, 'git_common_directory', side_effect=AssertionError('unneeded query')):
            self.assertEqual([], CLAUDE.missing_dependencies(IDENTITY, 'user', self.root, {}, rows))

    def test_dependency_persistence_uses_the_first_native_repository_registration(self):
        valid = {'id': 'base@tj-agents', 'scope': 'project', 'projectPath': str(self.primary), 'enabled': True}
        dependency = ('base@tj-agents', 'project', self.nested)
        for rows in ([valid], [{**valid, 'enabled': False}, {**valid, 'projectPath': str(self.nested)}],
                     [{**valid, 'projectPath': str(self.foreign)}], [{**valid, 'scope': 'local'}]):
            with self.subTest(rows=rows), mock.patch.object(plugin_refresh, 'run', side_effect=lambda command, cwd:
                    json.dumps(rows) if command[1:3] == ['plugin', 'list'] else ''):
                if rows == [valid]:
                    self.assertEqual(['base@tj-agents'], CLAUDE.apply(
                        'claude', self.nested, [], [], dependencies=[dependency]))
                else:
                    with self.assertRaisesRegex(REFRESH.RefreshError, 'did not persist enabled'):
                        CLAUDE.apply('claude', self.nested, [], [], dependencies=[dependency])

    def test_dependency_install_requires_an_enabled_effective_owner_scope(self):
        dependency = ('base@tj-agents', 'project', self.primary)
        for row in (None, {'id': 'base@tj-agents', 'scope': 'project', 'projectPath': str(self.primary),
                           'version': 'dependency', 'enabled': False}):
            with self.subTest(row=row):
                rows = [] if row is None else [row]

                def run(command, cwd):
                    if command[1:3] == ['plugin', 'list']:
                        return json.dumps(rows)
                    return ''

                with mock.patch.object(plugin_refresh, 'run', side_effect=run):
                    with self.assertRaisesRegex(REFRESH.RefreshError,
                                                r'base@tj-agents \(project\): native installation did not persist enabled'):
                        CLAUDE.apply('claude', self.primary, [], [], dependencies=[dependency])

    def test_current_scoped_plugin_uses_the_batch_dependency_closure(self):
        with mock.patch.object(CLAUDE, 'selected', return_value=({}, [IDENTITY])), \
                mock.patch.object(CLAUDE, 'installed_entries', return_value=[self.entry(self.primary)]), \
                mock.patch.object(CLAUDE.plugin_autoupdate, 'read_contents', return_value={'heads': {}, 'attempts': {}}), \
                mock.patch.object(CLAUDE, 'tj_agents_marketplaces', return_value={}), \
                mock.patch.object(CLAUDE, 'catalog_publishers', return_value={}), \
                mock.patch.object(CLAUDE, 'synchronize', return_value=['base@tj-agents']) as synchronize, \
                mock.patch.object(CLAUDE.agent_cli, 'resolve_claude_executable', return_value='claude'), \
                mock.patch.object(CLAUDE, 'refresh_marketplaces'), \
                mock.patch.object(CLAUDE, 'apply') as apply:
            self.assertEqual(['base@tj-agents'], CLAUDE.reconcile(self.primary))
        synchronize.assert_called_once_with([self.primary], identities={IDENTITY})
        apply.assert_not_called()

    def test_dependency_identity_keeps_explicit_marketplaces(self):
        self.assertEqual('service@other', CLAUDE.dependency_identity({'name': 'service', 'marketplace': 'other'},
                                                                       'tj-agents'))
        self.assertEqual('base@tj-agents', CLAUDE.dependency_identity('base', 'tj-agents'))
        self.assertIsNone(CLAUDE.dependency_identity({'marketplace': 'other'}, 'tj-agents'))

    def test_package_verification_ignores_only_shadowed_same_repository_rows(self):
        self.rows = [self.entry(self.primary, 'new'), self.entry(self.nested)]
        catalog = self.root / 'catalog'
        catalog.mkdir(exist_ok=True)
        (catalog / '.claude-plugin').mkdir(exist_ok=True)
        (catalog / '.claude-plugin/marketplace.json').write_text(
            json.dumps({'plugins': [{'name': 'agent', 'source': './agent'}]}), encoding='utf-8')
        known = {'tj-agents': {'installLocation': str(catalog)}}

        def run(command, cwd):
            if command[1:3] == ['plugin', 'list']:
                return json.dumps(self.rows)
            return 'a' * 40

        with mock.patch.object(plugin_refresh, 'run', side_effect=run), \
                mock.patch.dict(os.environ, {'CLAUDE_CONFIG_DIR': str(self.root)}), \
                mock.patch.object(plugin_refresh, 'read_json', side_effect=lambda path: known if path.name == 'known_marketplaces.json'
                                  else json.loads(path.read_text(encoding='utf-8'))), \
                mock.patch.object(sources, 'tree', return_value={'marker': b'new'}), \
                mock.patch.object(plugin_refresh, 'package_tree', side_effect=lambda path: {'marker': path.name.encode()}):
            report = REFRESH.verify_packages(CLAUDE, 'claude', self.nested,
                                            {'tj-agents': 'tj-agents/marketplace'}, [IDENTITY], self.root)
            self.assertEqual([], report['problems'])
            self.rows[0]['installPath'] = str(self.root / 'old')
            report = REFRESH.verify_packages(CLAUDE, 'claude', self.nested,
                                            {'tj-agents': 'tj-agents/marketplace'}, [IDENTITY], self.root)
            self.assertTrue(any('bytes differ' in problem for problem in report['problems']))

    def test_dependency_cycle_is_rejected(self):
        dependencies = {IDENTITY: [DEPENDENCY], DEPENDENCY: [IDENTITY]}
        with self.assertRaisesRegex(REFRESH.RefreshError, 'Cyclic plugin dependency'):
            CLAUDE.missing_dependencies(IDENTITY, 'project', self.primary, dependencies, [])


if __name__ == '__main__':
    unittest.main()
