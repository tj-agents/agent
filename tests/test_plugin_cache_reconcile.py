"""Synthetic profiles only; no normal-profile plugin cache is read or removed."""
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    'prune_plugin_cache', ROOT / '.agents/agent/runtime/prune_plugin_cache.py'
)
PRUNE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PRUNE)


class ReconcileHarness(unittest.TestCase):
    """Synthetic config root, cache and pin registry. Holds no tests of its own."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='synthetic plugin cache ')
        self.addCleanup(self.temp.cleanup)
        self.config = Path(self.temp.name) / '.claude'
        self.cache = self.config / 'plugins' / 'cache'
        self.state = Path(self.temp.name) / 'state'
        self.environ = {PRUNE.STATE_DIRECTORY_ENV: str(self.state)}

    def version(self, marketplace, plugin, version):
        path = self.cache / marketplace / plugin / version
        path.mkdir(parents=True, exist_ok=True)
        (path / 'payload.txt').write_text('x' * 16, encoding='utf-8')
        return path

    def registry(self, *install_paths, raw=None):
        path = self.config / 'plugins' / 'installed_plugins.json'
        path.parent.mkdir(parents=True, exist_ok=True)
        if raw is not None:
            path.write_text(raw, encoding='utf-8')
            return path
        plugins = {
            f'plugin{index}@market': [{'scope': 'user', 'installPath': str(entry)}]
            for index, entry in enumerate(install_paths)
        }
        path.write_text(json.dumps({'version': 2, 'plugins': plugins}), encoding='utf-8')
        return path

    def run_cli(self, *arguments):
        stream = io.StringIO()
        code = PRUNE.run_reconcile(
            self.config,
            keep_previous=0,
            apply_mode='--apply' in arguments,
            stream=stream,
            environ=self.environ,
        )
        return code, stream.getvalue()

    def pin(self, *install_paths, pid=None, started_at=0.0):
        """A pin file as `--pin` writes one. `pid=None` means this process, which is certainly alive."""
        import os

        entry = {
            'pid': os.getpid() if pid is None else pid,
            'started_at': started_at,
            'paths': [str(path) for path in install_paths],
        }
        directory = self.state / PRUNE.PIN_DIRECTORY
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{entry['pid']}.json"
        path.write_text(json.dumps(entry), encoding='utf-8')
        return path

class PluginCacheReconcileTests(ReconcileHarness):
    def test_report_is_the_default_and_removes_nothing(self):
        live = self.version('market', 'kept', 'aaaa')
        stale = self.version('market', 'kept', 'bbbb')
        orphan = self.version('gone', 'renamed', 'cccc')
        self.registry(live)

        code, output = self.run_cli()

        self.assertEqual(PRUNE.EXIT_OK, code)
        self.assertTrue(live.is_dir())
        self.assertTrue(stale.is_dir())
        self.assertTrue(orphan.is_dir())
        self.assertIn('would remove 2 directories', output)
        self.assertIn(str(stale), output)
        self.assertIn(str(orphan), output)

    def test_apply_removes_only_what_the_registry_does_not_name(self):
        live = self.version('market', 'kept', 'aaaa')
        stale = self.version('market', 'kept', 'bbbb')
        orphan = self.version('gone', 'renamed', 'cccc')
        self.registry(live)

        code, output = self.run_cli('--apply')

        self.assertEqual(PRUNE.EXIT_OK, code)
        self.assertTrue(live.is_dir())
        self.assertFalse(stale.exists())
        self.assertFalse(orphan.exists())
        self.assertIn('removed 2 directories', output)

    def test_apply_is_idempotent(self):
        live = self.version('market', 'kept', 'aaaa')
        self.version('market', 'kept', 'bbbb')
        self.registry(live)

        first, _ = self.run_cli('--apply')
        second, output = self.run_cli('--apply')

        self.assertEqual(PRUNE.EXIT_OK, first)
        self.assertEqual(PRUNE.EXIT_OK, second)
        self.assertIn('nothing to prune', output)
        self.assertTrue(live.is_dir())

    def test_a_clean_cache_succeeds_with_nothing_to_do(self):
        live = self.version('market', 'kept', 'aaaa')
        self.registry(live)

        code, output = self.run_cli('--apply')

        self.assertEqual(PRUNE.EXIT_OK, code)
        self.assertIn('nothing to prune', output)

    def test_an_unreadable_registry_removes_nothing(self):
        orphan = self.version('gone', 'renamed', 'cccc')

        code, output = self.run_cli('--apply')

        self.assertEqual(PRUNE.EXIT_REGISTRY, code)
        self.assertTrue(orphan.is_dir())
        self.assertEqual('', output)

    def test_a_malformed_registry_removes_nothing(self):
        orphan = self.version('gone', 'renamed', 'cccc')
        self.registry(raw='{ not json')

        code, _ = self.run_cli('--apply')

        self.assertEqual(PRUNE.EXIT_REGISTRY, code)
        self.assertTrue(orphan.is_dir())

    def test_a_registry_naming_no_install_paths_removes_nothing(self):
        orphan = self.version('gone', 'renamed', 'cccc')
        self.registry(raw=json.dumps({'version': 2, 'plugins': {}}))

        code, _ = self.run_cli('--apply')

        self.assertEqual(PRUNE.EXIT_REGISTRY, code)
        self.assertTrue(orphan.is_dir())

    def test_a_registry_entry_for_a_missing_directory_is_a_warning(self):
        live = self.version('market', 'kept', 'aaaa')
        self.registry(live, self.cache / 'market' / 'kept' / 'never-fetched')

        code, output = self.run_cli()

        self.assertEqual(PRUNE.EXIT_OK, code)
        self.assertIn('missing install path', output)
        self.assertIn('nothing to prune', output)

    def test_every_version_of_an_unregistered_plugin_is_an_orphan(self):
        live = self.version('market', 'kept', 'aaaa')
        first = self.version('gone', 'renamed', 'bbbb')
        second = self.version('gone', 'renamed', 'cccc')
        self.registry(live)

        states = dict(
            PRUNE.classify(PRUNE.cached_versions(self.cache), {PRUNE.normalize(live)})
        )

        self.assertEqual(PRUNE.LIVE, states[live])
        self.assertEqual(PRUNE.ORPHAN, states[first])
        self.assertEqual(PRUNE.ORPHAN, states[second])

    def test_retention_applies_only_to_a_still_installed_plugin(self):
        live = self.version('market', 'kept', 'aaaa')
        stale = self.version('market', 'kept', 'bbbb')
        orphan = self.version('gone', 'renamed', 'cccc')

        states = dict(
            PRUNE.classify(
                PRUNE.cached_versions(self.cache), {PRUNE.normalize(live)}, keep_previous=1
            )
        )

        self.assertEqual(PRUNE.LIVE, states[live])
        self.assertEqual(PRUNE.RETAINED, states[stale])
        self.assertEqual(PRUNE.ORPHAN, states[orphan])

    def test_apply_keeps_what_claude_orphaned_inside_its_window(self):
        import time

        live = self.version('market', 'kept', 'aaaa')
        recent = self.version('market', 'kept', 'bbbb')
        expired = self.version('market', 'kept', 'cccc')
        uninstalled = self.version('gone', 'renamed', 'dddd')
        now_ms = int(time.time() * 1000)
        (recent / PRUNE.HOST_ORPHAN_MARKER).write_text(str(now_ms), encoding='utf-8')
        (uninstalled / PRUNE.HOST_ORPHAN_MARKER).write_text(str(now_ms), encoding='utf-8')
        expired_ms = now_ms - (PRUNE.HOST_ORPHAN_GRACE_SECONDS + 60) * 1000
        (expired / PRUNE.HOST_ORPHAN_MARKER).write_text(str(expired_ms), encoding='utf-8')
        self.registry(live)

        code, output = self.run_cli('--apply')

        self.assertEqual(PRUNE.EXIT_OK, code)
        self.assertTrue(recent.is_dir())
        self.assertTrue(uninstalled.is_dir())
        self.assertFalse(expired.exists())
        self.assertIn("2 directory(s) inside Claude's 14-day orphan window", output)

    def test_a_removal_target_must_be_a_cached_version_directory(self):
        PRUNE.assert_within(self.cache, self.cache / 'market' / 'plugin' / 'version')
        with self.assertRaises(ValueError):
            PRUNE.assert_within(self.cache, self.cache / 'market' / 'plugin')
        with self.assertRaises(ValueError):
            PRUNE.assert_within(self.cache, self.cache.parent / 'installed_plugins.json')

    def test_the_notice_is_throttled_and_never_removes(self):
        live = self.version('market', 'kept', 'aaaa')
        stale = self.version('market', 'kept', 'bbbb')
        self.registry(live)
        state = Path(self.temp.name) / 'state'
        environ = {PRUNE.STATE_DIRECTORY_ENV: str(state)}

        first = io.StringIO()
        PRUNE.run_notice(self.config, 0, 1000.0, environ=environ, stream=first)
        second = io.StringIO()
        PRUNE.run_notice(self.config, 0, 1001.0, environ=environ, stream=second)

        self.assertIn('1 version directories', first.getvalue())
        self.assertEqual('', second.getvalue())
        self.assertTrue(stale.is_dir())

    def test_the_notice_stays_silent_on_an_unusable_registry(self):
        self.version('gone', 'renamed', 'cccc')
        stream = io.StringIO()

        code = PRUNE.run_notice(
            self.config,
            0,
            1000.0,
            environ={PRUNE.STATE_DIRECTORY_ENV: str(Path(self.temp.name) / 'state')},
            stream=stream,
        )

        self.assertEqual(PRUNE.EXIT_OK, code)
        self.assertEqual('', stream.getvalue())


class SessionPinTests(ReconcileHarness):
    """A version directory is never removed while a running session is bound to it."""

    def test_a_live_session_holds_a_directory_the_registry_no_longer_names(self):
        live = self.version('market', 'kept', 'aaaa')
        bound = self.version('market', 'kept', 'bbbb')
        self.registry(live)
        self.pin(bound)

        code, output = self.run_cli('--apply')

        self.assertEqual(PRUNE.EXIT_OK, code)
        self.assertTrue(bound.is_dir())
        self.assertIn('pinned', output)
        self.assertIn('held by a running session', output)

    def test_a_pin_holds_a_wholly_orphaned_plugin_too(self):
        live = self.version('market', 'kept', 'aaaa')
        renamed = self.version('gone', 'renamed', 'cccc')
        self.registry(live)
        self.pin(renamed)

        self.run_cli('--apply')

        self.assertTrue(renamed.is_dir())

    def test_a_pin_whose_process_is_gone_stops_holding_and_is_swept(self):
        live = self.version('market', 'kept', 'aaaa')
        stale = self.version('market', 'kept', 'bbbb')
        self.registry(live)
        # Pid 2**22 is above every Windows and Linux pid ceiling, so it names no process anywhere.
        pin = self.pin(stale, pid=2 ** 22, started_at=1.0)

        code, output = self.run_cli('--apply')

        self.assertEqual(PRUNE.EXIT_OK, code)
        self.assertFalse(stale.is_dir())
        self.assertFalse(pin.exists())
        self.assertNotIn('pinned', output)

    def test_a_young_pin_is_honoured_whatever_its_pid_says(self):
        bound = self.version('market', 'kept', 'bbbb')
        self.pin(bound, pid=2 ** 22, started_at=1000.0)

        held, expired = PRUNE.read_pins(self.environ, now=1000.0)

        self.assertEqual({PRUNE.normalize(bound)}, held)
        self.assertEqual([], expired)

    def test_an_unreadable_pin_holds_nothing_and_is_swept(self):
        directory = self.state / PRUNE.PIN_DIRECTORY
        directory.mkdir(parents=True, exist_ok=True)
        broken = directory / 'broken.json'
        broken.write_text('{not json', encoding='utf-8')

        held, expired = PRUNE.read_pins(self.environ, now=10.0 ** 9)

        self.assertEqual(set(), held)
        self.assertEqual([broken], expired)

    def test_a_dry_run_leaves_the_pin_registry_exactly_as_it_found_it(self):
        live = self.version('market', 'kept', 'aaaa')
        stale = self.version('market', 'kept', 'bbbb')
        self.registry(live)
        pin = self.pin(stale, pid=2 ** 22, started_at=1.0)

        self.run_cli()

        self.assertTrue(pin.exists())
        self.assertTrue(stale.is_dir())

    def test_pin_records_the_registry_and_never_fails_a_session(self):
        live = self.version('market', 'kept', 'aaaa')
        self.registry(live)

        self.assertEqual(PRUNE.EXIT_OK, PRUNE.run_pin(self.config, 1000.0, self.environ))

        written = json.loads(
            next((self.state / PRUNE.PIN_DIRECTORY).glob('*.json')).read_text(encoding='utf-8')
        )
        self.assertEqual([PRUNE.normalize(live)], written['paths'])

    def test_pin_exits_zero_when_the_registry_is_unusable_and_records_nothing(self):
        self.registry(raw='{not json')

        self.assertEqual(PRUNE.EXIT_OK, PRUNE.run_pin(self.config, 1000.0, self.environ))
        self.assertFalse((self.state / PRUNE.PIN_DIRECTORY).exists())

    def test_an_undecidable_pid_reads_as_alive(self):
        self.assertTrue(PRUNE.process_is_alive(None))
        self.assertTrue(PRUNE.process_is_alive(0))
        self.assertTrue(PRUNE.process_is_alive(-1))

    def test_this_process_is_alive_and_an_impossible_pid_is_not(self):
        import os

        self.assertTrue(PRUNE.process_is_alive(os.getpid()))
        self.assertFalse(PRUNE.process_is_alive(2 ** 22))


    def test_a_second_pin_under_one_parent_does_not_drop_the_first(self):
        first = self.version('market', 'kept', 'aaaa')
        second = self.version('market', 'kept', 'bbbb')
        self.registry(first)
        PRUNE.run_pin(self.config, 1000.0, self.environ)
        self.registry(second)
        PRUNE.run_pin(self.config, 1000.0, self.environ)

        held, _ = PRUNE.read_pins(self.environ, now=1000.0)

        self.assertEqual({PRUNE.normalize(first), PRUNE.normalize(second)}, held)

    def test_a_pin_that_holds_nothing_readable_contributes_nothing_to_the_union(self):
        live = self.version('market', 'kept', 'aaaa')
        self.registry(live)
        directory = self.state / PRUNE.PIN_DIRECTORY
        directory.mkdir(parents=True, exist_ok=True)
        import os

        (directory / f'{os.getppid()}.json').write_text('{not json', encoding='utf-8')

        entry = PRUNE.record_pin(self.config, os.getppid(), 1000.0, self.environ)

        self.assertEqual([PRUNE.normalize(live)], entry['paths'])



if __name__ == '__main__':
    unittest.main()
