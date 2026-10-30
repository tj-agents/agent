import base64
import importlib.util
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock
import xml.etree.ElementTree as ET


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("event_service", ROOT / ".agents/agent/runtime/event_service.py")
SERVICE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SERVICE)


class Supervisor:
    def __init__(self, context):
        self.context = context
        self.calls = []
        self.sid = "S-1-5-21-1234"
        self.account = r"DESKTOP\test-user"
        self.xml = None
        self.running = False
        self.enabled = False
        self.loaded = False
        self.overrides = ""
        self.fragment = None
        self.failure = None
        self.missing_exit = 0

    def __call__(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        if self.failure:
            return subprocess.CompletedProcess(argv, 1, "", self.failure)
        if self.context.windows:
            script = base64.b64decode(argv[-1]).decode("utf-16-le")
            if "ConvertTo-Json" in script:
                import json
                stdout = json.dumps({"sid": self.sid, "account": self.account, "xml": self.xml,
                                     "state": "Running" if self.running else "Ready"}, ensure_ascii=False)
            else:
                self.xml = SERVICE.task_xml(self.context, self.sid)
                self.running = True
                stdout = ""
            return subprocess.CompletedProcess(argv, 0, stdout, "")
        operation = argv[2]
        if operation == "show":
            path = SERVICE.unit_path(self.context)
            fragment = self.fragment or (str(path) if path.exists() else "")
            stdout = (f"LoadState={'loaded' if self.loaded else 'not-found'}\n"
                      f"ActiveState={'active' if self.running else 'inactive'}\n"
                      f"UnitFileState={'enabled' if self.enabled else 'disabled'}\n"
                      f"FragmentPath={fragment}\nDropInPaths={self.overrides}\nNeedDaemonReload=no\n")
        else:
            stdout = ""
            if operation == "daemon-reload":
                self.loaded = True
            elif operation == "enable":
                self.enabled = True
            elif operation == "restart":
                self.running = True
            else:
                raise AssertionError(argv)
        exit_code = self.missing_exit if operation == "show" and not self.loaded else 0
        return subprocess.CompletedProcess(argv, exit_code, stdout, "")


class ServiceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="events ü % $ space ")
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name)

    def context(self, windows=False):
        context = SimpleNamespace(home=self.home, environ={}, windows=windows, state_directory=self.home / "state % $ ü",
                                  claude_home=self.home / "Claude's home", codex_home=self.home / "codex home",
                                  which=lambda name: "/fake/bin/" + name)
        script = Path(SERVICE.arguments(context)[0])
        script.parent.mkdir(parents=True, exist_ok=True)
        script.write_text("pass\n", encoding="utf-8")
        context.run = Supervisor(context)
        return context

    def test_windows_registration_converges_hidden_limited_logon_task(self):
        context = self.context(windows=True)
        self.assertTrue(SERVICE.check(context))
        self.assertTrue(SERVICE.apply(context))
        self.assertEqual([], SERVICE.check(context))
        self.assertEqual([], SERVICE.apply(context))
        values, counts = SERVICE.xml_values(context.run.xml)
        self.assertEqual((1, 1, 1), counts)
        self.assertEqual("PT0S", values["/Task/Settings/ExecutionTimeLimit"])
        self.assertEqual("LeastPrivilege", values["/Task/Principals/Principal/RunLevel"])
        self.assertEqual("InteractiveToken", values["/Task/Principals/Principal/LogonType"])
        self.assertEqual("IgnoreNew", values["/Task/Settings/MultipleInstancesPolicy"])
        self.assertEqual("true", values["/Task/Settings/Hidden"])
        self.assertEqual("999", values["/Task/Settings/RestartOnFailure/Count"])
        self.assertEqual(context.run.sid, values["/Task/Triggers/LogonTrigger/UserId"])
        self.assertIn("--watch", values["/Task/Actions/Exec/Arguments"])
        self.assertIn("ü", values["/Task/Actions/Exec/Arguments"])
        for argv, kwargs in context.run.calls:
            self.assertIn("Hidden", argv)
            self.assertEqual("utf-8", kwargs["encoding"])
            self.assertNotIn("topic", base64.b64decode(argv[-1]).decode("utf-16-le"))

    def test_windows_detects_command_drift_and_stopped_task(self):
        context = self.context(windows=True)
        SERVICE.apply(context)
        context.run.xml = context.run.xml.replace("--watch", "--wrong")
        context.run.running = False
        drift = SERVICE.check(context)
        self.assertEqual(2, len(drift))
        SERVICE.apply(context)
        self.assertEqual([], SERVICE.check(context))

    def test_windows_exported_defaults_and_current_account_name_are_equivalent(self):
        context = self.context(windows=True)
        SERVICE.apply(context)
        xml = ET.fromstring(context.run.xml)
        for parent, name in (("Triggers/LogonTrigger", "Enabled"),
                             ("Principals/Principal", "RunLevel"), ("Settings", "Enabled")):
            node = xml.find("/".join(f"{{{SERVICE.NAMESPACE}}}{part}" for part in parent.split("/")))
            node.remove(node.find(f"{{{SERVICE.NAMESPACE}}}{name}"))
        for node in xml.iter(f"{{{SERVICE.NAMESPACE}}}UserId"):
            node.text = context.run.account
        context.run.xml = ET.tostring(xml, encoding="unicode")
        before = context.run.xml
        self.assertEqual([], SERVICE.check(context))
        self.assertEqual([], SERVICE.apply(context))
        self.assertEqual(before, context.run.xml)

    def test_windows_explicit_disabled_elevated_or_other_user_values_still_drift(self):
        context = self.context(windows=True)
        SERVICE.apply(context)
        original = context.run.xml
        for before, after in (("<Enabled>true</Enabled>", "<Enabled>false</Enabled>"),
                              ("LeastPrivilege", "HighestAvailable"),
                              (context.run.sid, "S-1-5-21-other")):
            with self.subTest(after=after):
                context.run.xml = original.replace(before, after)
                self.assertIn("machine-event scheduled task command or settings differ", SERVICE.check(context))

    def test_windows_preserves_task_owned_by_someone_else(self):
        context = self.context(windows=True)
        context.run.xml = SERVICE.task_xml(context, context.run.sid).replace(SERVICE.marker(context), "Someone else's task")
        before = context.run.xml
        for operation in (SERVICE.check, SERVICE.apply):
            with self.assertRaisesRegex(SERVICE.ServiceError, "unowned scheduled task"):
                operation(context)
        self.assertEqual(before, context.run.xml)
        self.assertTrue(all("ConvertTo-Json" in base64.b64decode(argv[-1]).decode("utf-16-le")
                            for argv, _ in context.run.calls))

    def test_windows_fallback_launches_console_python_through_hidden_powershell(self):
        context = self.context(windows=True)
        context.which = lambda name: None if name == "pythonw.exe" else "/fake/powershell.exe"
        with mock.patch.object(Path, "is_file", return_value=False):
            executable, argv = SERVICE.windows_action(context)
        self.assertEqual("/fake/powershell.exe", executable)
        self.assertIn("-WindowStyle Hidden", argv)
        encoded = argv.split()[-1]
        script = base64.b64decode(encoded).decode("utf-16-le")
        self.assertIn("--watch", script)
        self.assertIn("Claude''s home", script)
        self.assertIn("exit $LASTEXITCODE", script)

    def test_windows_rejects_additional_action(self):
        context = self.context(windows=True)
        SERVICE.apply(context)
        root = ET.fromstring(context.run.xml)
        actions = root.find(f"{{{SERVICE.NAMESPACE}}}Actions")
        ET.SubElement(actions, f"{{{SERVICE.NAMESPACE}}}Exec")
        context.run.xml = ET.tostring(root, encoding="unicode")
        self.assertTrue(SERVICE.check(context))

    def test_linux_registration_converges_and_restarts_stale_unit(self):
        context = self.context()
        self.assertTrue(SERVICE.check(context))
        self.assertTrue(SERVICE.apply(context))
        self.assertEqual([], SERVICE.check(context))
        self.assertEqual([], SERVICE.apply(context))
        path = SERVICE.unit_path(context)
        body = path.read_text(encoding="utf-8")
        self.assertIn("Restart=on-failure", body)
        self.assertIn("--watch", body)
        self.assertIn("%%", body)
        self.assertIn("$$", body)
        operations = [argv[2] for argv, _ in context.run.calls]
        self.assertEqual(1, operations.count("daemon-reload"))
        self.assertEqual(1, operations.count("enable"))
        self.assertEqual(1, operations.count("restart"))
        path.write_text(body.replace("Restart=on-failure", "Restart=no"), encoding="utf-8")
        self.assertTrue(SERVICE.apply(context))
        self.assertEqual(body, path.read_text(encoding="utf-8"))

    def test_linux_preserves_unowned_unit(self):
        context = self.context()
        path = SERVICE.unit_path(context)
        path.parent.mkdir(parents=True)
        path.write_text("[Service]\nExecStart=/someone/else\n", encoding="utf-8")
        with self.assertRaisesRegex(SERVICE.ServiceError, "unowned service unit"):
            SERVICE.apply(context)
        self.assertEqual("[Service]\nExecStart=/someone/else\n", path.read_text(encoding="utf-8"))
        self.assertEqual(["show"], [argv[2] for argv, _ in context.run.calls])

    def test_linux_rejects_unmanaged_dropins_and_foreign_fragment(self):
        context = self.context()
        context.run.overrides = "/foreign/override.conf"
        with self.assertRaisesRegex(SERVICE.ServiceError, "unmanaged overrides"):
            SERVICE.apply(context)
        context.run.overrides = ""
        context.run.fragment = "/etc/systemd/user/foreign.service"
        with self.assertRaisesRegex(SERVICE.ServiceError, "supplied by"):
            SERVICE.apply(context)

    def test_unreachable_user_supervisor_fails_without_writing(self):
        context = self.context()
        context.run.failure = "Failed to connect to bus"
        with self.assertRaisesRegex(SERVICE.ServiceError, "connect to bus"):
            SERVICE.apply(context)
        self.assertFalse(SERVICE.unit_path(context).exists())

    def test_missing_linux_unit_nonzero_exit_is_installable(self):
        context = self.context()
        context.run.missing_exit = 1
        self.assertTrue(SERVICE.apply(context))
        self.assertEqual([], SERVICE.check(context))

    def test_native_task_state_transition_is_polled_before_success(self):
        context = self.context(windows=True)
        original = SERVICE.windows_snapshot
        queries = []

        def delayed_snapshot(value):
            snapshot = original(value)
            queries.append(snapshot)
            if len(queries) == 2:
                snapshot["state"] = "Ready"
            return snapshot

        with mock.patch.object(SERVICE, "windows_snapshot", side_effect=delayed_snapshot), \
                mock.patch.object(SERVICE.time, "sleep") as sleep:
            self.assertTrue(SERVICE.apply(context))
        sleep.assert_called_once_with(0.2)

    def test_linux_native_concurrent_registration_is_preserved(self):
        context = self.context()
        path = SERVICE.unit_path(context)
        original = SERVICE.unit_contents
        calls = []

        def contents(value):
            calls.append(value)
            if len(calls) == 2:
                path.write_text("foreign registration", encoding="utf-8")
            return original(value)

        with mock.patch.object(SERVICE, "unit_contents", side_effect=contents):
            with self.assertRaisesRegex(SERVICE.ServiceError, "changed during registration"):
                SERVICE.apply(context)
        self.assertEqual("foreign registration", path.read_text(encoding="utf-8"))
        self.assertFalse(path.with_suffix(".service.next").exists())

    def test_service_name_is_stable_and_state_scoped(self):
        context = self.context()
        first = SERVICE.identity(context)
        self.assertEqual(first, SERVICE.identity(context))
        context.state_directory = context.state_directory / "other"
        self.assertNotEqual(first, SERVICE.identity(context))

    def test_escaping_rejects_multiline_paths(self):
        with self.assertRaisesRegex(SERVICE.ServiceError, "control characters"):
            SERVICE.systemd_escape("/a\nb")
        self.assertEqual('"a\\\\b\\"c%%$$"', SERVICE.systemd_escape('a\\b"c%$'))

    def test_shell_tool_search_path_survives_service_delivery(self):
        context = self.context()
        context.environ["PATH"] = "/home/user/.nvm/v22/bin:/home/user/tools % $ ü:/usr/bin"
        expected = "--search-path=" + context.environ["PATH"]
        self.assertIn(expected, SERVICE.arguments(context))
        self.assertIn(SERVICE.systemd_escape(expected), SERVICE.unit_contents(context))
        self.assertEqual(context.environ["PATH"], SERVICE.receipt(context.state_directory, context.claude_home,
                         context.codex_home, context.environ["PATH"])["search_path"])

    def test_missing_runtime_does_not_register_anything(self):
        context = self.context()
        Path(SERVICE.arguments(context)[0]).unlink()
        with self.assertRaisesRegex(SERVICE.ServiceError, "runtime is missing"):
            SERVICE.apply(context)
        self.assertEqual([], context.run.calls)


if __name__ == "__main__":
    unittest.main()
