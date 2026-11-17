from __future__ import annotations

from dataclasses import dataclass
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "plugins/agent/.agents/agent/runtime"
sys.path.insert(0, str(SCRIPTS))
import host_plugins
import install
import native_runtime
import required_plugins
from tj_agent.host import Host


SCRIPT = """import os
import sys
from pathlib import Path
state = Path(os.environ['PLUGIN_SETUP_STATE'])
if sys.argv[-1] == '--check':
    raise SystemExit(0 if state.exists() else 1)
state.write_text(sys.argv[-1], encoding='utf-8')
"""


class PluginSetupTests(unittest.TestCase):
    def setUp(self):
        reconciliation = mock.patch.object(required_plugins, "reconcile_selection", return_value=[])
        reconciliation.start()
        self.addCleanup(reconciliation.stop)
        temporary = tempfile.TemporaryDirectory(prefix="plugin setup ")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.project = self.root / "project"
        (self.project / ".agents").mkdir(parents=True)
        (self.project / ".agents/plugins.json").write_text(
            json.dumps({"schema_version": 1, "plugins": ["selected@publisher"]}), encoding="utf-8")
        self.runtime = self.root / "runtime"
        script = self.runtime / "scripts/setup.py"
        script.parent.mkdir(parents=True)
        script.write_text(SCRIPT, encoding="utf-8")
        harness = self.runtime / "harness.json"
        harness.write_text(json.dumps({"plugin": "publisher/selected", "requires": {
            "setup": {"script": "scripts/setup.py"}}}), encoding="utf-8")
        self.state = self.root / "state"
        self.records = self.root / "agent state"
        self.environment = mock.patch.dict(os.environ, {"PLUGIN_SETUP_STATE": str(self.state)})
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def roots(self):
        return mock.patch.object(required_plugins, "check", return_value={"selected@publisher": self.runtime})

    def test_check_is_read_only_and_apply_converges(self):
        with self.roots():
            self.assertEqual(["selected@publisher: setup has not succeeded with its current runtime"],
                             required_plugins.check_setup(self.project, ("codex",), {"codex": None}, self.records))
            self.assertFalse(self.state.exists())
            self.assertEqual([], required_plugins.converge_setup(self.project, ("codex",), {"codex": None}, self.records))
            self.assertEqual("setup", self.state.read_text(encoding="utf-8"))

    def test_check_reports_missing_plugins_without_installing(self):
        missing = required_plugins.MissingRequiredPluginsError(
            "missing selected", self.project, ["selected@publisher"]
        )
        with mock.patch.object(required_plugins, "check", side_effect=missing), \
                mock.patch.object(host_plugins.PLUGINS[Host.CODEX], "install") as install:
            errors = required_plugins.check_setup(
                self.project, (Host.CODEX,), {Host.CODEX: None}, self.records
            )
        self.assertEqual([f"{Host.CODEX}: missing selected"], errors)
        install.assert_not_called()
        self.assertFalse(self.state.exists())

    def test_converge_repairs_missing_plugins_after_collecting_every_host(self):
        missing = {Host.CLAUDE, Host.CODEX}
        checks = []

        def check(project, host, executable=None, *, inventory_options=()):
            self.assertEqual(self.project.resolve(), project)
            self.assertEqual((), inventory_options)
            checks.append(host)
            if host in missing:
                raise required_plugins.MissingRequiredPluginsError(
                    f"missing {host}", project, ["selected@publisher"]
                )
            return {"selected@publisher": self.runtime}

        def install(project, identities):
            self.assertEqual(self.project.resolve(), project)
            self.assertEqual(("selected@publisher",), tuple(identities))
            self.assertEqual({Host.CLAUDE, Host.CODEX}, set(checks))
            missing.remove(Host.CLAUDE)

        def codex_install(project, identities):
            self.assertEqual(self.project.resolve(), project)
            self.assertEqual(("selected@publisher",), tuple(identities))
            self.assertEqual({Host.CLAUDE, Host.CODEX}, set(checks))
            missing.remove(Host.CODEX)

        with mock.patch.object(required_plugins, "check", side_effect=check), \
                mock.patch.object(host_plugins.PLUGINS[Host.CLAUDE], "install", side_effect=install), \
                mock.patch.object(host_plugins.PLUGINS[Host.CODEX], "install", side_effect=codex_install):
            self.assertEqual([], required_plugins.converge_setup(
                self.project, (Host.CLAUDE, Host.CODEX), {Host.CLAUDE: None, Host.CODEX: None}, self.records
            ))
        self.assertEqual([Host.CLAUDE, Host.CODEX, Host.CLAUDE, Host.CODEX, Host.CLAUDE, Host.CODEX], checks)

    def test_onboarding_is_passed_only_when_explicitly_requested(self):
        with self.roots():
            self.assertEqual([], required_plugins.converge_setup(self.project, ("codex",), {"codex": None}, self.records, onboard=True))
        self.assertEqual("--onboard", self.state.read_text(encoding="utf-8"))

    def test_setup_and_checks_run_in_the_selected_project_instead_of_the_callers_checkout(self):
        script = self.runtime / "scripts/setup.py"
        script.write_text("import os\nfrom pathlib import Path\n"
                          "assert Path.cwd().resolve() == Path(os.environ['PLUGIN_SETUP_PROJECT']).resolve()\n" + SCRIPT,
                          encoding="utf-8")
        nested = self.project / "nested directory"
        nested.mkdir()
        caller = Path.cwd()
        with mock.patch.dict(os.environ, {"PLUGIN_SETUP_PROJECT": str(self.project)}), self.roots():
            for onboard in (False, True):
                with self.subTest(onboard=onboard):
                    self.state.unlink(missing_ok=True)
                    self.assertEqual([], required_plugins.converge_setup(nested, ("codex",),
                                                                        {"codex": None}, self.records, onboard=onboard))
                    self.assertEqual([], required_plugins.check_setup(nested, ("codex",), {"codex": None}, self.records))
        self.assertEqual(caller, Path.cwd())

    def test_unselected_runtime_is_not_examined_or_executed(self):
        unrelated = self.root / "unrelated"
        unrelated.mkdir()
        (unrelated / "harness.json").write_text(json.dumps({"plugin": "publisher/other", "requires": {
            "setup": {"script": "missing.py"}}}), encoding="utf-8")
        with mock.patch.object(required_plugins, "check", return_value={"selected@publisher": self.runtime}):
            self.assertEqual([], required_plugins.converge_setup(self.project, ("codex",), {"codex": None}, self.records))
        self.assertEqual("setup", self.state.read_text(encoding="utf-8"))

    def test_valid_harness_without_setup_is_not_an_error(self):
        (self.runtime / "harness.json").write_text(json.dumps({"plugin": "publisher/selected", "requires": {}}),
                                                   encoding="utf-8")
        with self.roots():
            self.assertEqual([], required_plugins.converge_setup(self.project, ("codex",), {"codex": None}, self.records))
        self.assertFalse(self.state.exists())

    def test_malformed_declaration_fails_without_success(self):
        harness = self.runtime / "harness.json"
        harness.write_text(json.dumps({"plugin": "publisher/selected", "requires": {"setup": {"script": "../bad.py"}}}),
                           encoding="utf-8")
        with self.roots():
            errors = required_plugins.converge_setup(self.project, ("codex",), {"codex": None}, self.records)
        self.assertTrue(errors)
        self.assertFalse(self.state.exists())

    def test_conflicting_host_runtime_is_rejected(self):
        other = self.root / "other"
        (other / "scripts").mkdir(parents=True)
        (other / "scripts/setup.py").write_text(SCRIPT + "\n# changed\n", encoding="utf-8")
        harness = other / "harness.json"
        harness.write_text((self.runtime / "harness.json").read_text(encoding="utf-8"),
                           encoding="utf-8")
        with mock.patch.object(required_plugins, "check", side_effect=({"selected@publisher": self.runtime}, {"selected@publisher": other})):
            errors = required_plugins.converge_setup(self.project, ("claude", "codex"), {"claude": None, "codex": None}, self.records)
        self.assertIn("runtimes disagree", errors[0])

    def test_host_adapter_differences_do_not_conflict_when_setup_runtime_matches(self):
        other = self.root / "other"
        (other / "scripts").mkdir(parents=True)
        (other / "scripts/setup.py").write_text(SCRIPT, encoding="utf-8")
        (other / "host-adapter.txt").write_text("different host payload", encoding="utf-8")
        (other / "harness.json").write_text((self.runtime / "harness.json").read_text(encoding="utf-8"),
                                              encoding="utf-8")
        with mock.patch.object(required_plugins, "check", side_effect=({"selected@publisher": self.runtime}, {"selected@publisher": other})):
            self.assertEqual([], required_plugins.converge_setup(self.project, ("claude", "codex"),
                                                       {"claude": None, "codex": None}, self.records))

    def test_same_script_with_a_different_runtime_sibling_is_rejected_before_execution(self):
        other = self.root / "other"
        (other / "scripts").mkdir(parents=True)
        (other / "scripts/setup.py").write_text(SCRIPT, encoding="utf-8")
        (other / "scripts/descriptor.json").write_text('{"different": true}', encoding="utf-8")
        (other / "harness.json").write_text((self.runtime / "harness.json").read_text(encoding="utf-8"),
                                              encoding="utf-8")
        with mock.patch.object(required_plugins, "check", side_effect=({"selected@publisher": self.runtime}, {"selected@publisher": other})), \
                mock.patch.object(required_plugins, "invoke") as invoke:
            errors = required_plugins.converge_setup(self.project, ("claude", "codex"), {"claude": None, "codex": None}, self.records)
        self.assertIn("runtimes disagree", errors[0])
        invoke.assert_not_called()

    def test_setup_declaration_must_exist_on_every_requested_host(self):
        other = self.root / "other"
        other.mkdir()
        (other / "harness.json").write_text(json.dumps({"plugin": "publisher/selected", "requires": {}}),
                                              encoding="utf-8")
        with mock.patch.object(required_plugins, "check", side_effect=({"selected@publisher": self.runtime}, {"selected@publisher": other})), \
                mock.patch.object(required_plugins, "invoke") as invoke:
            errors = required_plugins.converge_setup(self.project, ("claude", "codex"), {"claude": None, "codex": None}, self.records)
        self.assertIn("setup declarations disagree", errors[0])
        invoke.assert_not_called()

    @unittest.skipUnless(os.name == "nt", "Windows junction")
    def test_windows_junction_in_declared_path_is_rejected(self):
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "setup.py").write_text(SCRIPT, encoding="utf-8")
        junction = self.runtime / "scripts"
        for path in junction.iterdir():
            path.unlink()
        junction.rmdir()
        result = __import__("subprocess").run(["cmd", "/c", "mklink", "/J", str(junction), str(outside)],
                                                capture_output=True, text=True, check=False)
        if result.returncode:
            self.skipTest("cannot create a Windows junction")
        with self.roots():
            errors = required_plugins.converge_setup(self.project, ("codex",), {"codex": None}, self.records)
        self.assertIn("unsafe", errors[0])



RECORDED_SETUP = """import sys
from pathlib import Path
runs = Path.cwd() / "setup-runs.log"
if sys.argv[-1] == "--check":
    raise SystemExit(0 if runs.exists() else 1)
with runs.open("a", encoding="utf-8") as stream:
    stream.write(sys.argv[-1] + "\\n")
"""


@dataclass(frozen=True, slots=True)
class SetupMachine:
    project: Path
    runtime: Path
    state: Path

    @property
    def runs(self) -> list[str]:
        log = self.project / "setup-runs.log"
        return log.read_text(encoding="utf-8").splitlines() if log.exists() else []

    @property
    def record(self) -> Path:
        return self.state / required_plugins.SETUP_RECORD

    def converge(self) -> list[str]:
        return required_plugins.converge_setup(
            self.project, (Host.CODEX,), {Host.CODEX: None}, self.state
        )

    def check(self) -> list[str]:
        return required_plugins.check_setup(
            self.project, (Host.CODEX,), {Host.CODEX: None}, self.state
        )


@pytest.fixture
def setup_machine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SetupMachine:
    project = tmp_path / "project ü"
    (project / ".agents").mkdir(parents=True)
    (project / ".agents/plugins.json").write_text(
        json.dumps({"schema_version": 1, "plugins": ["selected@publisher"]}),
        encoding="utf-8",
    )
    runtime = tmp_path / "runtime"
    (runtime / "scripts").mkdir(parents=True)
    (runtime / "scripts/setup.py").write_text(RECORDED_SETUP, encoding="utf-8")
    harness = {
        "plugin": "publisher/selected",
        "requires": {"setup": {"script": "scripts/setup.py"}},
    }
    (runtime / "harness.json").write_text(json.dumps(harness), encoding="utf-8")
    monkeypatch.setattr(required_plugins, "reconcile_selection", lambda *_, **__: [])
    monkeypatch.setattr(
        required_plugins,
        "check",
        lambda *_, **__: {"selected@publisher": runtime},
    )
    return SetupMachine(project, runtime, tmp_path / "state")


def test_converge_setup_runs_setup_once_while_its_runtime_is_unchanged(
    setup_machine: SetupMachine,
) -> None:
    assert setup_machine.converge() == []
    assert setup_machine.converge() == []
    assert setup_machine.check() == []
    assert setup_machine.runs == ["setup"]
    recorded = json.loads(setup_machine.record.read_text(encoding="utf-8"))
    runtime = required_plugins.runtime_bytes(setup_machine.runtime / "scripts")
    assert recorded == {"selected@publisher": runtime.hex()}


def test_converge_setup_reruns_setup_when_its_runtime_changes(
    setup_machine: SetupMachine,
) -> None:
    assert setup_machine.converge() == []
    (setup_machine.runtime / "scripts/descriptor.json").write_text(
        "{}", encoding="utf-8"
    )
    assert setup_machine.check() == [
        "selected@publisher: setup has not succeeded with its current runtime"
    ]
    assert setup_machine.runs == ["setup"]
    assert setup_machine.converge() == []
    assert setup_machine.runs == ["setup", "setup"]
    assert setup_machine.check() == []


def test_converge_setup_reruns_setup_when_its_check_fails(
    setup_machine: SetupMachine,
) -> None:
    assert setup_machine.converge() == []
    (setup_machine.project / "setup-runs.log").unlink()
    assert setup_machine.check() == ["selected@publisher: setup --check failed: exit 1"]
    assert setup_machine.converge() == []
    assert setup_machine.runs == ["setup"]


@pytest.mark.parametrize("damage", ["deleted", "corrupt"])
def test_converge_setup_rebuilds_a_lost_record_by_running_setup_once(
    setup_machine: SetupMachine, damage: str
) -> None:
    assert setup_machine.converge() == []
    if damage == "deleted":
        setup_machine.record.unlink()
    else:
        setup_machine.record.write_text("not json", encoding="utf-8")
    assert setup_machine.converge() == []
    assert setup_machine.converge() == []
    assert setup_machine.runs == ["setup", "setup"]


def test_converge_setup_does_not_record_a_setup_that_did_not_converge(
    setup_machine: SetupMachine,
) -> None:
    (setup_machine.runtime / "scripts/setup.py").write_text(
        "import sys\nraise SystemExit(1 if sys.argv[-1] == '--check' else 0)\n",
        encoding="utf-8",
    )
    assert setup_machine.converge() == [
        "selected@publisher: setup did not converge: exit 1"
    ]
    assert not setup_machine.record.exists()


def test_install_plugin_setup_step_is_current_after_one_run(
    setup_machine: SetupMachine, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(native_runtime, "pending", lambda: False)
    context = install.Context(
        home=setup_machine.state.parent,
        environ={"AGENT_STATE_DIRECTORY": str(setup_machine.state)},
        windows=False,
        project=setup_machine.project,
        which=lambda _name: None,
    )
    step = next(item for item in install.STEPS if item.name == "plugin setup")
    outputs = []
    for _ in range(2):
        output = io.StringIO()
        assert install.run_steps([step], context, False, output, io.StringIO()) == 0
        outputs.append(output.getvalue().splitlines()[-1])
    assert outputs == [
        "apply  plugin setup: selected@publisher: setup has not succeeded with its "
        "current runtime",
        "ok     plugin setup",
    ]
    assert setup_machine.runs == ["setup"]


if __name__ == "__main__":
    unittest.main()
