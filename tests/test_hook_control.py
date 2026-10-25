import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / '.agents/codex/utility/hooks/scripts/hook_control.py'
SPEC = importlib.util.spec_from_file_location('hook_control', SCRIPT)
CONTROL = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = CONTROL
SPEC.loader.exec_module(CONTROL)


class HookControlTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='hook-control-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def config(self, name='profile'):
        path = self.root / name / 'config.toml'
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def test_true_round_trip_preserves_comments_legacy_trust_and_plugins(self):
        config = self.config()
        original = (
            '# top-level note\n'
            '[features]\n'
            'hooks = true # retain\n'
            'codex_hooks = false\n\n'
            '[plugins]\n'
            'trusted_hash = "unchanged"\n'
        )
        config.write_text(original, encoding='utf-8')
        result = CONTROL.disable('global', config)
        self.assertTrue(result['changed'])
        self.assertIn('hooks = false # retain', config.read_text(encoding='utf-8'))
        snapshot = json.loads(CONTROL.sidecar_path(config).read_text(encoding='utf-8'))
        self.assertIs(snapshot['original'], True)
        result = CONTROL.restore('global', config)
        self.assertTrue(result['changed'])
        self.assertEqual(config.read_text(encoding='utf-8'), original)
        self.assertFalse(CONTROL.sidecar_path(config).exists())

    def test_false_is_saved_and_restored_without_enabling(self):
        config = self.config()
        original = '[features]\nhooks = false\n'
        config.write_text(original, encoding='utf-8')
        self.assertFalse(CONTROL.disable('global', config)['changed'])
        self.assertFalse(CONTROL.restore('global', config)['changed'])
        self.assertEqual(config.read_text(encoding='utf-8'), original)

    def test_absent_value_and_introduced_table_are_removed(self):
        config = self.config()
        original = '[plugins]\ntrusted_hash = "kept"\n'
        config.write_text(original, encoding='utf-8')
        CONTROL.disable('global', config)
        self.assertIn('[features]', config.read_text(encoding='utf-8'))
        CONTROL.restore('global', config)
        self.assertEqual(config.read_text(encoding='utf-8'), original)

    def test_missing_config_is_removed_after_absent_restore(self):
        config = self.config()
        config.unlink(missing_ok=True)
        CONTROL.disable('global', config)
        self.assertTrue(config.exists())
        CONTROL.restore('global', config)
        self.assertFalse(config.exists())

    def test_existing_empty_config_survives_absent_restore(self):
        config = self.config()
        config.write_text('', encoding='utf-8')
        CONTROL.disable('global', config)
        CONTROL.restore('global', config)
        self.assertTrue(config.exists())
        self.assertEqual(config.read_text(encoding='utf-8'), '')

    def test_existing_empty_features_table_survives_absent_restore(self):
        config = self.config()
        original = '[features]\n'
        config.write_text(original, encoding='utf-8')
        CONTROL.disable('global', config)
        CONTROL.restore('global', config)
        self.assertEqual(config.read_text(encoding='utf-8'), original)

    def test_no_final_newline_round_trips(self):
        config = self.config()
        original = '[features]\nhooks = true'
        config.write_text(original, encoding='utf-8')
        CONTROL.disable('global', config)
        CONTROL.restore('global', config)
        self.assertEqual(config.read_text(encoding='utf-8'), original)

    def test_absent_hook_no_final_newline_round_trips_exactly(self):
        config = self.config()
        original = 'model = "x"'
        config.write_text(original, encoding='utf-8')
        CONTROL.disable('global', config)
        CONTROL.restore('global', config)
        self.assertEqual(config.read_bytes(), original.encode('utf-8'))

    def test_crlf_round_trip_preserves_bytes(self):
        config = self.config()
        original = b'[features]\r\nhooks = true # keep\r\n[plugins]\r\nenabled = true\r\n'
        config.write_bytes(original)
        CONTROL.disable('global', config)
        CONTROL.restore('global', config)
        self.assertEqual(config.read_bytes(), original)

    def test_project_and_global_snapshots_are_isolated(self):
        global_config = self.root / 'home' / 'config.toml'
        global_config.parent.mkdir(parents=True)
        global_config.write_text('[features]\nhooks = true\n', encoding='utf-8')
        project = self.root / 'project'
        project_config = project / '.codex' / 'config.toml'
        project_config.parent.mkdir(parents=True)
        project_config.write_text('[features]\nhooks = true\n', encoding='utf-8')
        CONTROL.disable('global', global_config)
        CONTROL.disable('project', project_config)
        CONTROL.restore('global', global_config)
        self.assertIn('hooks = true', global_config.read_text(encoding='utf-8'))
        self.assertIn('hooks = false', project_config.read_text(encoding='utf-8'))

    def test_repeated_off_keeps_original_restoration_value(self):
        config = self.config()
        config.write_text('[features]\nhooks = true\n', encoding='utf-8')
        CONTROL.disable('global', config)
        CONTROL.disable('global', config)
        snapshot = json.loads(CONTROL.sidecar_path(config).read_text(encoding='utf-8'))
        self.assertIs(snapshot['original'], True)
        CONTROL.restore('global', config)
        self.assertIn('hooks = true', config.read_text(encoding='utf-8'))

    def test_restore_rejects_user_change_and_retains_snapshot(self):
        config = self.config()
        config.write_text('[features]\nhooks = true\n', encoding='utf-8')
        CONTROL.disable('global', config)
        config.write_text('[features]\nhooks = true\n', encoding='utf-8')
        with self.assertRaisesRegex(CONTROL.ControlError, 'changed while disabled'):
            CONTROL.restore('global', config)
        self.assertTrue(CONTROL.sidecar_path(config).exists())

    def test_corrupted_original_values_do_not_edit_disabled_config(self):
        for original in (0, 1, 'true'):
            with self.subTest(original=original):
                config = self.config(f'corrupt-original-{original}')
                config.write_text('[features]\nhooks = true\n', encoding='utf-8')
                CONTROL.disable('global', config)
                sidecar = CONTROL.sidecar_path(config)
                snapshot = json.loads(sidecar.read_text(encoding='utf-8'))
                snapshot['original'] = original
                sidecar.write_text(json.dumps(snapshot), encoding='utf-8')
                disabled = config.read_bytes()
                corrupted = sidecar.read_bytes()
                with self.assertRaisesRegex(CONTROL.ControlError, 'invalid original'):
                    CONTROL.restore('global', config)
                self.assertEqual(config.read_bytes(), disabled)
                self.assertEqual(sidecar.read_bytes(), corrupted)

    def test_corrupted_boolean_ownership_does_not_edit_disabled_config(self):
        for field in ('config_existed', 'features_table_existed'):
            for value in (0, 1, 'true'):
                with self.subTest(field=field, value=value):
                    config = self.config(f'corrupt-ownership-{field}-{value}')
                    config.write_text('[features]\nhooks = true\n', encoding='utf-8')
                    CONTROL.disable('global', config)
                    sidecar = CONTROL.sidecar_path(config)
                    snapshot = json.loads(sidecar.read_text(encoding='utf-8'))
                    snapshot[field] = value
                    sidecar.write_text(json.dumps(snapshot), encoding='utf-8')
                    disabled = config.read_bytes()
                    corrupted = sidecar.read_bytes()
                    with self.assertRaisesRegex(CONTROL.ControlError, f'invalid {field}'):
                        CONTROL.restore('global', config)
                    self.assertEqual(config.read_bytes(), disabled)
                    self.assertEqual(sidecar.read_bytes(), corrupted)

    def test_unknown_separator_does_not_edit_disabled_config(self):
        config = self.config()
        config.write_text('[features]\nhooks = true\n', encoding='utf-8')
        CONTROL.disable('global', config)
        sidecar = CONTROL.sidecar_path(config)
        snapshot = json.loads(sidecar.read_text(encoding='utf-8'))
        snapshot['owned_separator'] = '\x0b'
        sidecar.write_text(json.dumps(snapshot), encoding='utf-8')
        disabled = config.read_bytes()
        corrupted = sidecar.read_bytes()
        with self.assertRaisesRegex(CONTROL.ControlError, 'invalid owned_separator'):
            CONTROL.restore('global', config)
        self.assertEqual(config.read_bytes(), disabled)
        self.assertEqual(sidecar.read_bytes(), corrupted)

    def test_dotted_form_is_preserved(self):
        config = self.config()
        original = 'features.hooks = true # canonical\n[plugins]\ntrusted_hash = "kept"\n'
        config.write_text(original, encoding='utf-8')
        CONTROL.disable('global', config)
        self.assertIn('features.hooks = false # canonical', config.read_text(encoding='utf-8'))
        CONTROL.restore('global', config)
        self.assertEqual(config.read_text(encoding='utf-8'), original)

    def test_restore_preserves_hook_and_plugin_changes_made_while_off(self):
        config = self.config()
        config.write_text(
            '[features]\n'
            'hooks = true\n'
            '[hooks]\n'
            'async = false\n'
            '[hooks.state.existing]\n'
            'enabled = true\n'
            'trusted_hash = "old"\n'
            '[plugins.example]\n'
            'enabled = true\n',
            encoding='utf-8')
        CONTROL.disable('global', config)
        changed = config.read_text(encoding='utf-8').replace('trusted_hash = "old"', 'trusted_hash = "updated"').replace(
            '[plugins.example]\nenabled = true', '[plugins.example]\nenabled = false')
        changed += '[hooks.state.new_identity]\nenabled = true\ntrusted_hash = "new"\n'
        config.write_text(changed, encoding='utf-8')
        CONTROL.restore('global', config)
        restored = config.read_text(encoding='utf-8')
        self.assertIn('hooks = true', restored)
        self.assertIn('async = false', restored)
        self.assertIn('trusted_hash = "updated"', restored)
        self.assertIn('[hooks.state.new_identity]', restored)
        self.assertIn('trusted_hash = "new"', restored)
        self.assertIn('[plugins.example]\nenabled = false', restored)

    def test_status_counts_only_disabled_hook_states(self):
        config = self.config()
        config.write_text(
            '[features]\nhooks = true\n[hooks]\nasync = false\n'
            '[hooks.state.one]\nenabled = false\n'
            '[hooks.state.two]\nenabled = true\n', encoding='utf-8')
        result = CONTROL.status('global', config)
        self.assertEqual(result['individually_disabled_entries'], 1)

    def test_unfamiliar_value_is_rejected_before_edit(self):
        config = self.config()
        original = '[features]\nhooks = "false"\n'
        config.write_text(original, encoding='utf-8')
        with self.assertRaisesRegex(CONTROL.ControlError, 'true or false'):
            CONTROL.disable('global', config)
        self.assertEqual(config.read_text(encoding='utf-8'), original)
        self.assertFalse(CONTROL.sidecar_path(config).exists())

    def test_inline_table_is_rejected_before_snapshot(self):
        config = self.config()
        config.write_text('features = { hooks = true }\n', encoding='utf-8')
        with self.assertRaisesRegex(CONTROL.ControlError, 'unfamiliar TOML syntax'):
            CONTROL.disable('global', config)
        self.assertFalse(CONTROL.sidecar_path(config).exists())

    def test_concurrent_off_preserves_single_original_for_restore(self):
        config = self.config()
        config.write_text('[features]\nhooks = true\n', encoding='utf-8')
        commands = [[sys.executable, '-B', str(SCRIPT), 'off', '--scope', 'global', '--codex-home', str(config.parent)]] * 2
        processes = [subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for command in commands]
        outputs = [process.communicate() for process in processes]
        self.assertEqual([process.returncode for process in processes], [0, 0], outputs)
        snapshot = json.loads(CONTROL.sidecar_path(config).read_text(encoding='utf-8'))
        self.assertIs(snapshot['original'], True)
        restored = subprocess.run(
            [sys.executable, '-B', str(SCRIPT), 'on', '--scope', 'global', '--codex-home', str(config.parent)],
            capture_output=True, text=True, check=False)
        self.assertEqual(restored.returncode, 0, restored.stderr)
        self.assertIn('hooks = true', config.read_text(encoding='utf-8'))

    @unittest.skipUnless(sys.platform == 'win32', 'Windows sharing violations are Windows-only')
    def test_snapshot_read_and_write_tolerate_an_open_reader(self):
        config = self.config()
        config.write_text('[features]\nhooks = true\n', encoding='utf-8')
        CONTROL.disable('global', config)
        sidecar = CONTROL.sidecar_path(config)
        original = CONTROL.load_snapshot(sidecar)
        replacement = dict(original, disabled_sha256='a' * 64)
        attempted = threading.Event()
        errors = []
        original_replace = CONTROL.os.replace
        original_read_text = Path.read_text
        read_attempts = 0
        transient_winerrors = (5, 32)

        def replace(source, destination):
            attempted.set()
            return original_replace(source, destination)

        def write():
            try:
                CONTROL.atomic_json(sidecar, replacement)
            except BaseException as error:
                errors.append(error)

        def read_text(path, *args, **kwargs):
            nonlocal read_attempts
            if path == sidecar:
                read_attempts += 1
                if read_attempts <= len(transient_winerrors):
                    error = PermissionError(13, 'sharing violation')
                    error.winerror = transient_winerrors[read_attempts - 1]
                    raise error
            return original_read_text(path, *args, **kwargs)

        with mock.patch.object(CONTROL.os, 'replace', side_effect=replace):
            with sidecar.open(encoding='utf-8') as reader:
                reader.read()
                writer = threading.Thread(target=write)
                writer.start()
                self.assertTrue(attempted.wait(timeout=5))
                self.assertTrue(writer.is_alive())
                with mock.patch.object(Path, 'read_text', new=read_text):
                    self.assertEqual(CONTROL.load_snapshot(sidecar)['disabled_sha256'], original['disabled_sha256'])
            writer.join(timeout=5)

        self.assertFalse(writer.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(read_attempts, 3)
        self.assertEqual(CONTROL.load_snapshot(sidecar)['disabled_sha256'], 'a' * 64)

    @unittest.skipUnless(sys.platform == 'win32', 'Windows sharing violations are Windows-only')
    def test_snapshot_restore_tolerates_an_open_reader(self):
        config = self.config()
        config.write_text('[features]\nhooks = false\n', encoding='utf-8')
        CONTROL.disable('global', config)
        sidecar = CONTROL.sidecar_path(config)
        attempted = threading.Event()
        errors = []
        restored = []
        original_unlink = Path.unlink

        def unlink(path, *args, **kwargs):
            if path == sidecar:
                attempted.set()
            return original_unlink(path, *args, **kwargs)

        def restore():
            try:
                restored.append(CONTROL.restore('global', config))
            except BaseException as error:
                errors.append(error)

        with mock.patch.object(Path, 'unlink', new=unlink):
            with sidecar.open(encoding='utf-8') as reader:
                reader.read()
                writer = threading.Thread(target=restore)
                writer.start()
                self.assertTrue(attempted.wait(timeout=5))
                self.assertTrue(writer.is_alive())
                self.assertEqual(CONTROL.load_snapshot(sidecar)['original'], False)
            writer.join(timeout=5)

        self.assertFalse(writer.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(restored[0]['restored'], False)
        self.assertFalse(sidecar.exists())

    @unittest.skipUnless(sys.platform == 'win32', 'Windows lock contention is Windows-only')
    def test_windows_lock_retries_a_transient_open_permission_error(self):
        lock = CONTROL.lock_path(self.config())
        original_open = open
        attempts = 0

        def open_after_transient_permission_error(*args, **kwargs):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise PermissionError(13, 'Permission denied')
            return original_open(*args, **kwargs)

        with mock.patch('builtins.open', side_effect=open_after_transient_permission_error):
            with CONTROL.ExclusiveLock(lock):
                self.assertTrue(lock.exists())

        self.assertEqual(attempts, 2)

    @unittest.skipUnless(sys.platform == 'win32', 'Windows lock contention is Windows-only')
    def test_windows_lock_closes_and_reopens_after_a_transient_initialization_permission_error(self):
        lock = CONTROL.lock_path(self.config())
        original_open = open
        failed_handle = mock.Mock()
        failed_handle.read.side_effect = PermissionError(13, 'Permission denied')
        attempts = 0

        def open_after_transient_permission_error(*args, **kwargs):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                return failed_handle
            return original_open(*args, **kwargs)

        with mock.patch('builtins.open', side_effect=open_after_transient_permission_error):
            with CONTROL.ExclusiveLock(lock):
                self.assertTrue(lock.exists())

        failed_handle.close.assert_called_once_with()
        self.assertEqual(attempts, 2)

if __name__ == '__main__':
    unittest.main()
