import contextlib
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock


SCRIPTS = Path(__file__).resolve().parents[1] / "plugins/agent/.agents/agent/utility/peer/scripts"
sys.path.insert(0, str(SCRIPTS))


def load_module(name):
    path = SCRIPTS / (name + ".py")
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


close_tab = load_module("close_tab")
configurator = load_module("configure_terminal_tab_close")


def completed(stdout="", returncode=0, stderr=""):
    return mock.Mock(stdout=stdout, returncode=returncode, stderr=stderr)


def guarded_close(identity, *, before_close):
    before_close()


class CloseTabTests(unittest.TestCase):
    def test_stale_windows_fallback_requires_unique_title_and_honors_recorded_id(self):
        identity = {"kind": "windows-terminal", "wt_session": "native-session"}
        entry = {"session_id": "stale", "pid": 10, "pid_started_at": 1.0,
                 "title": "same", "terminal": identity}
        target = {"title": "same", "identity": {"kind": "windows-terminal",
                  "runtime_id": "tab-2"}}
        with mock.patch.object(close_tab, "is_windows", return_value=True), \
                mock.patch.object(close_tab, "actual_targets", return_value=[target]), \
                mock.patch.object(close_tab.session_close, "entries", return_value=[(Path("stale"), entry)]), \
                mock.patch.object(close_tab.session_close, "liveness", return_value=False), \
                mock.patch.object(close_tab, "close_actual") as closer:
            close_tab.close_stale_entry(entry)
            closer.assert_called_once_with(target, fallback_entry=entry)
        for recorded, targets in ((identity, [target, target]),
                                  ({**identity, "runtime_id": "tab-1"}, [target])):
            with self.subTest(recorded=recorded, targets=targets), \
                    mock.patch.object(close_tab, "actual_targets", return_value=targets), \
                    mock.patch.object(close_tab.session_close, "entries", return_value=[(Path("stale"), entry)]), \
                    mock.patch.object(close_tab, "close_actual") as closer:
                with self.assertRaisesRegex(close_tab.processes.Refusal, "absent or ambiguous"):
                    close_tab.close_stale_entry({"title": "same", "terminal": recorded})
                closer.assert_not_called()




    def test_stale_windows_title_fallback_refuses_native_successor_with_changed_title(self):
        stale = {"session_id": "old", "title": "old title",
                 "terminal": {"kind": "windows-terminal"}}
        successor = {"session_id": "new", "title": "new title",
                     "terminal": {"kind": "windows-terminal", "runtime_id": "tab-2"}}
        target = {"title": "old title", "identity": successor["terminal"]}
        for state in (True, None):
            with self.subTest(state=state), \
                    mock.patch.object(close_tab, "actual_targets", return_value=[target]), \
                    mock.patch.object(close_tab.session_close, "entries",
                                      return_value=[(Path("old"), stale), (Path("new"), successor)]), \
                    mock.patch.object(close_tab.session_close, "liveness",
                                      side_effect=lambda entry: False if entry is stale else state), \
                    mock.patch.object(close_tab, "close_actual") as closer:
                with self.assertRaisesRegex(close_tab.processes.Refusal, "live or unknown"):
                    close_tab.close_stale_entry(stale)
                closer.assert_not_called()

    def test_stale_windows_title_fallback_refuses_any_other_registered_peer(self):
        stale = {"session_id": "stale", "title": "same",
                 "terminal": {"kind": "windows-terminal", "wt_session": "old"}}
        live = {"session_id": "live", "title": "same",
                "terminal": {"kind": "windows-terminal", "wt_session": "current"}}
        target = {"title": "same", "identity": {"kind": "windows-terminal",
                  "runtime_id": "live-tab"}}
        for state in (True, None, False):
            with self.subTest(other_peer_liveness=state), \
                    mock.patch.object(close_tab, "actual_targets", return_value=[target]), \
                    mock.patch.object(close_tab.session_close, "entries",
                                      return_value=[(Path("stale"), stale), (Path("live"), live)]), \
                    mock.patch.object(close_tab.session_close, "liveness", return_value=state), \
                    mock.patch.object(close_tab, "close_actual") as closer:
                with self.assertRaisesRegex(close_tab.processes.Refusal, "absent or ambiguous"):
                    close_tab.close_stale_entry(stale)
                closer.assert_not_called()

    def test_stale_linux_target_requires_recorded_identity_despite_matching_title(self):
        cases = (
            ("kitty", "kitty_inventory", {"window_id": "1", "listen_on": "unix:/kitty"},
             {"window_id": "2", "listen_on": "unix:/kitty"}),
            ("tmux", "tmux_inventory", {"pane_id": "%1", "socket": "/tmp/tmux"},
             {"pane_id": "%2", "socket": "/tmp/tmux"}),
        )
        for kind, inventory, recorded, replacement in cases:
            with self.subTest(kind=kind):
                identity = {"kind": kind, **recorded}
                entry = {"title": "same", "terminal": identity}
                other = {"title": "same", "identity": {"kind": kind, **replacement}}
                exact = {"title": "same", "identity": identity}
                with mock.patch.object(close_tab, "actual_targets", return_value=[other]), \
                        mock.patch.object(close_tab, "close_actual") as closer:
                    with self.assertRaisesRegex(close_tab.processes.Refusal, "absent or ambiguous"):
                        close_tab.close_stale_entry(entry)
                    closer.assert_not_called()
                with mock.patch.object(close_tab, "actual_targets", return_value=[other, exact]), \
                        mock.patch.object(close_tab.session_close, "entries",
                                          return_value=[(Path("stale"), entry)]), \
                        mock.patch.object(close_tab.session_close, "liveness", return_value=False), \
                        mock.patch.object(close_tab, "close_actual") as closer:
                    close_tab.close_stale_entry(entry)
                    closer.assert_called_once_with(exact, fallback_entry=None)

    def test_stale_linux_target_refuses_live_or_unknown_successor_with_same_native_target(self):
        for kind, inventory, identity in (
                ("kitty", "kitty_inventory", {"window_id": "1", "listen_on": "unix:/kitty"}),
                ("tmux", "tmux_inventory", {"pane_id": "%1", "socket": "/tmp/tmux"}),
        ):
            with self.subTest(kind=kind):
                recorded = {"kind": kind, **identity}
                stale = {"session_id": "same", "title": "old title", "terminal": recorded,
                         "pid": 10, "pid_started_at": 1.0}
                successor = {"session_id": "same", "title": "new title", "terminal": recorded,
                             "pid": 20, "pid_started_at": 2.0}
                target = {"title": "old title", "identity": recorded}
                for state in (True, None):
                    with self.subTest(state=state), \
                            mock.patch.object(close_tab, "actual_targets", return_value=[target]), \
                            mock.patch.object(close_tab.session_close, "entries",
                                              return_value=[(Path("same.json"), successor)]), \
                            mock.patch.object(close_tab.session_close, "liveness", return_value=state), \
                            mock.patch.object(close_tab, "close_actual") as closer:
                        with self.assertRaisesRegex(close_tab.processes.Refusal, "live or unknown"):
                            close_tab.close_stale_entry(stale)
                        closer.assert_not_called()

    def test_stale_close_rechecks_a_successor_registered_after_the_first_guard(self):
        for kind, inventory, identity in (
                ("tmux", "tmux_inventory", {"pane_id": "%1", "socket": "/tmp/tmux"}),
                ("windows-terminal", "actual_targets", {"runtime_id": "tab-2"}),
        ):
            with self.subTest(kind=kind):
                recorded = {"kind": kind, **({} if kind == "windows-terminal" else identity)}
                stale = {"session_id": "same", "title": "old", "terminal": recorded,
                         "pid": 10, "pid_started_at": 1.0}
                successor = {**stale, "pid": 20, "pid_started_at": 2.0}
                target = {"title": "old", "identity": {"kind": kind, **identity}}
                reads = [[(Path("same"), stale)], [(Path("same"), successor)]]
                if kind == "windows-terminal":
                    reads.insert(0, [(Path("same"), stale)])
                with mock.patch.object(close_tab, "actual_targets", return_value=[target]), \
                        mock.patch.object(close_tab.session_close, "entries", side_effect=reads), \
                        mock.patch.object(close_tab.session_close, "liveness",
                                          side_effect=lambda entry: False if entry is stale else True), \
                        mock.patch.object(close_tab.terminal, "close_identity", side_effect=guarded_close) as terminal_command:
                    with self.assertRaises(close_tab.processes.Refusal):
                        close_tab.close_stale_entry(stale)
                terminal_command.assert_called_once()

    def test_missing_or_unverified_native_identity_refuses(self):
        entry = {"pid": 0, "pid_started_at": 0, "terminal": {"kind": "kitty"}}
        with mock.patch.object(close_tab.session_close, "liveness", return_value=None), mock.patch.object(close_tab.terminal, "close_identity", side_effect=guarded_close):
            with self.assertRaisesRegex(close_tab.processes.Refusal, "verified live"):
                close_tab.close_entry(entry)




    def test_windows_missing_runtime_ids_match_only_the_same_fallback_title(self):
        first = {"kind": "windows-terminal", "title": "first"}
        second = {"kind": "windows-terminal", "title": "second"}
        missing = {"kind": "windows-terminal"}
        self.assertFalse(close_tab.same_target(first, second))
        self.assertFalse(close_tab.same_target(first, missing))
        self.assertFalse(close_tab.same_target(missing, missing))
        self.assertTrue(close_tab.same_target(first, dict(first)))

    def test_windows_runtime_id_never_falls_back_to_a_matching_title(self):
        identified = {"kind": "windows-terminal", "runtime_id": "tab-1",
                      "title": "same"}
        fallback = {"kind": "windows-terminal", "title": "same"}
        self.assertFalse(close_tab.same_target(identified, fallback))



    def test_windows_direct_title_selection_refuses_duplicate_fallback_tabs(self):
        targets = [
            {"title": "same", "identity": {"kind": "windows-terminal", "title": "same"},
             "actual": True},
            {"title": "same", "identity": {"kind": "windows-terminal", "title": "same"},
             "actual": True},
        ]
        with mock.patch.object(close_tab, "merged_inventory", return_value=targets):
            with self.assertRaisesRegex(close_tab.processes.Refusal, "ambiguous"):
                close_tab.main(["--title", "same"])






    def test_exact_title_requires_one_actual_target(self):
        targets = [
            {"title": "same", "identity": {"kind": "tmux", "pane_id": "%1"}, "actual": True},
            {"title": "same", "identity": {"kind": "tmux", "pane_id": "%2"}, "actual": True},
        ]
        with mock.patch.object(close_tab, "merged_inventory", return_value=targets):
            with self.assertRaisesRegex(close_tab.processes.Refusal, "ambiguous"):
                close_tab.main(["--title", "same"])

    def test_wildcard_one_target_still_refuses_without_all(self):
        targets = [{"title": "only", "identity": {"kind": "tmux", "pane_id": "%1"},
                    "actual": True}]
        with mock.patch.object(close_tab, "merged_inventory", return_value=targets):
            with self.assertRaisesRegex(close_tab.processes.Refusal, "wildcard"):
                close_tab.main(["--title", "on*"])

    def test_live_unknown_and_force_policy_never_signals_a_process(self):
        target = {"title": "stale", "identity": {"kind": "tmux", "pane_id": "%1"},
                  "actual": True}
        for live in (True, None):
            with self.subTest(live=live), \
                    mock.patch.object(close_tab, "target_liveness", return_value=live), \
                    mock.patch.object(close_tab.terminal, "close_identity", side_effect=guarded_close) as runner, \
                    mock.patch.object(close_tab.os, "kill") as killer:
                with self.assertRaisesRegex(close_tab.processes.Refusal, "live or unknown"):
                    close_tab.close_actual(target)
                runner.assert_called_once()
                killer.assert_not_called()
        with mock.patch.object(close_tab, "target_liveness", return_value=None), \
                mock.patch.object(close_tab.terminal, "close_identity", side_effect=guarded_close) as runner, \
                mock.patch.object(close_tab.os, "kill") as killer:
            close_tab.close_actual(target, force=True)
        self.assertEqual(runner.call_args.args[0], target["identity"])
        killer.assert_not_called()

    def test_explicit_title_force_keeps_direct_actual_tab_authorization(self):
        target = {"title": "running", "identity": {"kind": "tmux", "pane_id": "%1"},
                  "actual": True}
        with mock.patch.object(close_tab, "merged_inventory", return_value=[target]), \
                mock.patch.object(close_tab, "close_actual") as closer:
            self.assertEqual(0, close_tab.main(["--title", "running", "--force"]))
        closer.assert_called_once_with(target, force=True)



    def test_list_combines_actual_stale_tabs_and_registered_live_targets(self):
        actual = {"title": "stale", "identity": {"kind": "tmux", "pane_id": "%1"},
                  "actual": True}
        live_entry = {"title": "live", "terminal": {"kind": "tmux", "pane_id": "%2"}}
        stale_entry = {"title": "stale", "terminal": {"kind": "tmux", "pane_id": "%1"}}
        with mock.patch.object(close_tab, "current_terminal_targets", return_value=[actual]), \
                mock.patch.object(close_tab.session_close, "entries",
                                  return_value=[(Path("stale"), stale_entry),
                                                (Path("live"), live_entry)]), \
                mock.patch.object(close_tab.session_close, "liveness",
                                  side_effect=lambda entry: False if entry is stale_entry else True):
            inventory = close_tab.merged_inventory()
        self.assertEqual(inventory[0]["title"], "stale")
        self.assertFalse(inventory[0]["live"])
        self.assertEqual(inventory[1]["title"], "live")
        self.assertTrue(inventory[1]["live"])



class FocusEntryTests(unittest.TestCase):
    def entry(self, identity):
        return {"session_id": "s1", "pid": 10, "pid_started_at": 1.0,
                "title": "tab", "terminal": identity}

    def test_refuses_without_verified_live_host_or_terminal_identity(self):
        with self.assertRaises(close_tab.processes.Refusal):
            close_tab.focus_entry({"session_id": "s1"})
        with mock.patch.object(close_tab.session_close, "liveness", return_value=None), \
                mock.patch.object(close_tab.terminal, "focus") as focus:
            with self.assertRaises(close_tab.processes.Refusal):
                close_tab.focus_entry(self.entry({"kind": "tmux", "pane_id": "%1"}))
            focus.assert_not_called()

    def test_focus_requires_process_binding_before_the_native_mutation(self):
        identity = {"kind": "windows-terminal", "runtime_id": "tab-1"}
        events = []
        with mock.patch.object(close_tab.session_close, "liveness", return_value=True), \
                mock.patch.object(close_tab.terminal, "verify_process_binding", side_effect=lambda *args: events.append("verify")) as verify, \
                mock.patch.object(close_tab.terminal, "focus", side_effect=lambda *args: events.append("focus")) as focus:
            close_tab.focus_entry(self.entry(identity))
        verify.assert_called_once_with(identity, {"pid": 10, "started_at": 1.0})
        focus.assert_called_once_with(identity)
        self.assertEqual(events, ["verify", "focus"])

    def test_binding_refusal_preserves_the_tab(self):
        with mock.patch.object(close_tab.session_close, "liveness", return_value=True), \
                mock.patch.object(close_tab.terminal, "verify_process_binding", side_effect=close_tab.processes.Refusal("foreign tab")), \
                mock.patch.object(close_tab.terminal, "focus") as focus:
            with self.assertRaisesRegex(close_tab.processes.Refusal, "foreign tab"):
                close_tab.focus_entry(self.entry({"kind": "tmux", "pane_id": "%3"}))
        focus.assert_not_called()

    def test_close_rechecks_liveness_and_binding_inside_the_native_guard(self):
        identity = {"kind": "kitty", "listen_on": "unix:/k", "window_id": "7"}
        with mock.patch.object(close_tab.session_close, "liveness", return_value=True), \
                mock.patch.object(close_tab.terminal, "verify_process_binding") as verify, \
                mock.patch.object(close_tab.terminal, "close_identity", side_effect=guarded_close) as close:
            close_tab.close_entry(self.entry(identity))
        close.assert_called_once()
        self.assertEqual(close.call_args.args, (identity,))
        verify.assert_called_once_with(identity, {"pid": 10, "started_at": 1.0})

    def test_native_close_failure_is_reported(self):
        with mock.patch.object(close_tab.terminal, "close_identity", side_effect=close_tab.processes.Refusal("did not confirm")):
            with self.assertRaisesRegex(close_tab.processes.Refusal, "did not confirm"):
                close_tab.close_entry(self.entry({"kind": "tmux", "pane_id": "%3"}))

    def test_focus_flag_requires_a_session(self):
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            close_tab.main(["--focus"])


class ConfigureTerminalTabCloseTests(unittest.TestCase):
    def test_write_preserves_windows_line_endings_and_backup_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            original = b'{\r\n  "profiles": {"defaults": {}}\r\n}\r\n'
            path.write_bytes(original)
            _message, backup = configurator.configure(path, "always")
            self.assertEqual(backup.read_bytes(), original)
            updated = path.read_bytes()
            self.assertIn(b'"closeOnExit": "always"', updated)
            self.assertNotIn(b"\n", updated.replace(b"\r\n", b""))

    def test_preview_preserves_jsonc_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            original = '{\n  // keep this comment\n  "profiles": { "defaults": { } }\n}\n'
            path.write_text(original, encoding="utf-8")
            message, backup = configurator.configure(path, "always", preview=True)
            self.assertIn("Would set", message)
            self.assertIsNone(backup)
            self.assertEqual(path.read_text(encoding="utf-8"), original)

    def test_write_makes_timestamped_backup_and_preserves_jsonc(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            original = '{\n  // keep\n  "profiles": {\n    "defaults": {\n      "fontFace": "Cascadia"\n    }\n  }\n}\n'
            path.write_text(original, encoding="utf-8")
            moment = configurator.datetime.datetime(2026, 10, 10, 12, 0, 1)
            _message, backup = configurator.configure(path, "always", now=moment)
            self.assertEqual(backup.name, "settings.json.bak-20261010120001")
            self.assertEqual(backup.read_text(encoding="utf-8"), original)
            updated = path.read_text(encoding="utf-8")
            self.assertIn("// keep", updated)
            self.assertIn('"closeOnExit": "always",', updated)

    def test_empty_defaults_does_not_receive_invalid_comma(self):
        updated, changed = configurator.update_close_on_exit('{"profiles":{"defaults":{}}}', "always")
        self.assertTrue(changed)
        self.assertIn('"closeOnExit": "always"}', updated)
        self.assertNotIn('"always",}', updated)

    def test_existing_value_is_unchanged_without_backup(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            path.write_text('{"profiles":{"defaults":{"closeOnExit":"always"}}}', encoding="utf-8")
            message, backup = configurator.configure(path, "always")
            self.assertEqual(message, "closeOnExit is already 'always'.")
            self.assertIsNone(backup)
            self.assertEqual(list(Path(directory).glob("*.bak-*")), [])

    def test_linux_noop_and_explicit_disposable_settings_path(self):
        output = io.StringIO()
        with mock.patch.object(configurator, "is_windows", return_value=False), \
                contextlib.redirect_stdout(output):
            self.assertEqual(configurator.main([]), 0)
        self.assertIn("Linux is a no-op", output.getvalue())
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            path.write_text('{"profiles":{"defaults":{}}}', encoding="utf-8")
            with mock.patch.object(configurator, "is_windows", return_value=False):
                self.assertEqual(configurator.main(["--settings-path", str(path), "--preview"]), 0)

    def test_default_windows_settings_path_uses_local_app_data(self):
        path = configurator.default_settings_path({"LOCALAPPDATA": "C:/Users/agent/AppData/Local"})
        self.assertEqual(
            str(path).replace("\\", "/"),
            "C:/Users/agent/AppData/Local/Packages/"
            "Microsoft.WindowsTerminal_8wekyb3d8bbwe/LocalState/settings.json",
        )
