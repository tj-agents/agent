import io
import subprocess
import sys
from pathlib import Path

import install
import plugin_copies
import pytest
from fake_machine import install_plugin, pin_plugin

PIN = "b" * 40
OLD = "a" * 40


class FakeConverge:
    def __init__(self, failing: frozenset[Path] = frozenset()) -> None:
        self.failing = failing
        self.runs: list[tuple[list[str], Path]] = []

    def __call__(
        self, command: list[str], **options: object
    ) -> subprocess.CompletedProcess[str]:
        cwd = Path(str(options["cwd"]))
        self.runs.append((command, cwd))
        if cwd in self.failing:
            return subprocess.CompletedProcess(command, 1, "", "copies refused")
        return subprocess.CompletedProcess(command, 0, "", "")


@pytest.fixture
def machine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    return tmp_path


def copies_step(machine: Path, converge: FakeConverge) -> tuple[int, str]:
    context = install.Context(home=machine, environ={}, windows=False, run=converge)
    step = next(item for item in install.STEPS if item.name == "plugin copies")
    errors = io.StringIO()
    code = install.run_steps([step], context, False, io.StringIO(), errors)
    return code, errors.getvalue()


def state(machine: Path) -> Path:
    return machine / ".agents-state"


def converge_command(root: Path) -> list[str]:
    return [
        sys.executable,
        "-B",
        str((root / "scripts/setup.py").resolve()),
        "converge",
    ]


def test_plugin_copies_converge_only_pinned_declaring_installs(machine: Path) -> None:
    claude = machine / "claude"
    for plugin, revision, declares in (
        ("personal", PIN, True),
        ("behind", OLD, True),
        ("plain", PIN, False),
    ):
        pin_plugin(claude, plugin, PIN)
        install_plugin(claude, plugin, revision, converge=declares)
    personal = claude / "plugins/cache/tj-agents/personal" / PIN[:12]
    converge = FakeConverge()
    assert copies_step(machine, converge) == (0, "")
    assert converge.runs == [(converge_command(personal), personal)]
    assert plugin_copies.converged(state(machine)) == {"personal@tj-agents": PIN}
    assert plugin_copies.drift(state(machine)) == []


def test_converged_plugin_copies_are_not_converged_again(machine: Path) -> None:
    pin_plugin(machine / "claude", "personal", PIN)
    install_plugin(machine / "claude", "personal", PIN, converge=True)
    copies_step(machine, FakeConverge())
    converge = FakeConverge()
    assert copies_step(machine, converge) == (0, "")
    assert converge.runs == []


def test_a_new_pin_is_plugin_copies_drift(machine: Path) -> None:
    claude = machine / "claude"
    pin_plugin(claude, "personal", OLD)
    install_plugin(claude, "personal", OLD, converge=True)
    copies_step(machine, FakeConverge())
    pin_plugin(claude, "personal", PIN)
    install_plugin(claude, "personal", PIN, converge=True)
    assert plugin_copies.drift(state(machine)) == [
        f"personal@tj-agents copies are from {OLD[:12]}, pinned {PIN[:12]}"
    ]


def test_a_failed_converge_stays_drift_and_fails_the_step(machine: Path) -> None:
    claude = machine / "claude"
    for plugin in ("personal", "work"):
        pin_plugin(claude, plugin, PIN)
        install_plugin(claude, plugin, PIN, converge=True)
    work = claude / "plugins/cache/tj-agents/work" / PIN[:12]
    code, errors = copies_step(machine, FakeConverge(frozenset({work})))
    assert code == 2
    assert "work@tj-agents: converge failed: copies refused" in errors
    assert plugin_copies.converged(state(machine)) == {"personal@tj-agents": PIN}
    assert plugin_copies.drift(state(machine)) == [
        f"work@tj-agents copies are from no build, pinned {PIN[:12]}"
    ]


def test_plugin_copies_check_writes_nothing(machine: Path) -> None:
    pin_plugin(machine / "claude", "personal", PIN)
    install_plugin(machine / "claude", "personal", PIN, converge=True)
    context = install.Context(
        home=machine, environ={}, windows=False, run=FakeConverge()
    )
    step = next(item for item in install.STEPS if item.name == "plugin copies")
    output = io.StringIO()
    assert install.run_steps([step], context, True, output, io.StringIO()) == 1
    assert "drift  plugin copies: personal@tj-agents" in output.getvalue()
    assert not (state(machine) / plugin_copies.MARKER).exists()
