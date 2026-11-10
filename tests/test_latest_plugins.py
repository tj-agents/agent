import io
import json
import os
import shlex
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import install
import jobs
import latest_plugins
import machine_state
import plugin_copies
import pytest
import retired_state
from fake_machine import (
    Started,
    agent_root,
    converge,
    environment,
    install_plugin,
    machine_context,
    pin_plugin,
    write_json,
    write_tool_receipt,
)
from tj_agent.host import Host
from tj_cli import process

RUNTIME = Path(latest_plugins.__file__).resolve().parent
PINS = json.loads((RUNTIME / "work-items.json").read_text(encoding="utf-8"))
PIN = "b354217d980a143516a3bd6029d60ffcc9ea3e7b"
OLD = "9e207f3dbe4ce5d06e1a792f6f8edcc366fc4cfd"
UPDATING_CLAUDE = (
    "Updating tj-agents plugins in the background; "
    "type /reload-plugins when it finishes."
)
UPDATING_CODEX = (
    "Updating tj-agents plugins in the background; "
    "restart this session when it finishes."
)
REFRESH = [sys.executable, "-B", str(RUNTIME / "plugins" / "refresh.py")]
INSTALL = [sys.executable, "-B", str(RUNTIME / "install.py")]
DRIFT_STEPS = [
    "Codex shared daemon",
    "agent-store CLI",
    "tj item CLI",
    "launch-check runtime",
    "tj lifecycle commands",
    "shell profiles",
    "Codex hook trust",
    "plugin copies",
]


INSTALLED_BUILD = machine_state.installed_build


@dataclass
class Machine:
    home: Path
    context: Any
    started: Started
    monkeypatch: pytest.MonkeyPatch
    capsys: pytest.CaptureFixture[str]

    @property
    def claude(self) -> Path:
        return self.home / "claude"

    @property
    def state(self) -> Path:
        return self.home / "state"

    @property
    def install(self) -> Path:
        return self.claude / "plugins/cache/tj-agents/agent/current"

    def registry(self, revision: str) -> None:
        rows = {
            "agent@tj-agents": [
                {
                    "scope": "user",
                    "gitCommitSha": revision,
                    "installPath": str(self.install),
                }
            ],
            "cli@tj-agents": [
                {"scope": "user", "gitCommitSha": OLD, "installPath": str(self.install)}
            ],
        }
        registry = self.claude / "plugins/installed_plugins.json"
        write_json(registry, {"version": 2, "plugins": rows})

    def codex_build(self, label: str) -> None:
        (self.home / "codex/plugins/cache/tj-agents/agent" / label).mkdir(parents=True)

    def converge(self) -> None:
        converge(self.context, machine_state.packaged("install.py").parents[3])

    def hook(self, host: str, event: str, root: Path | None = None) -> Any:
        running = str(root or self.install)
        self.monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", running)
        self.monkeypatch.setenv("PLUGIN_ROOT", running)
        event_input = io.StringIO(json.dumps({"hook_event_name": event}))
        self.monkeypatch.setattr(sys, "stdin", event_input)
        self.capsys.readouterr()
        assert latest_plugins.main(["--host", host]) == 0
        output = self.capsys.readouterr().out
        if not output.strip():
            return None
        document = json.loads(output)
        assert "decision" not in document
        context = document["hookSpecificOutput"]
        assert context["hookEventName"] == event
        assert document["systemMessage"] == context["additionalContext"]
        return context["additionalContext"]

    def reason(self) -> str | None:
        return latest_plugins.reason(Host.CLAUDE, self.state, full=True)


@pytest.fixture
def machine(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> Machine:
    for name, value in environment(tmp_path).items():
        monkeypatch.setenv(name, value)
    for name in ("CLAUDE_PLUGIN_ROOT", "PLUGIN_ROOT"):
        monkeypatch.delenv(name, raising=False)
    context = machine_context(tmp_path)
    monkeypatch.setattr(install, "machine_context", lambda: context)
    monkeypatch.setattr(retired_state, "remove", lambda _state: None)
    monkeypatch.setattr(machine_state, "installed_build", lambda: True)
    started = Started()
    monkeypatch.setattr(process, "start", started)
    catalog = tmp_path / "claude/plugins/marketplaces/tj-agents/.claude-plugin"
    write_json(
        catalog / "marketplace.json",
        {
            "plugins": [
                {"name": "agent", "source": {"source": "git-subdir", "sha": PIN}},
                {"name": "cli", "source": {"source": "git-subdir", "ref": "main"}},
            ]
        },
    )
    agent = Machine(tmp_path, context, started, monkeypatch, capsys)
    write_json(
        agent.install / ".codex-plugin/plugin.json",
        {"name": "agent", "version": "aaaaaaaaaaaa"},
    )
    agent_root(agent.install, "current build")
    return agent


def install_step(name: str) -> list[str]:
    script = machine_state.packaged("install.py")
    return [sys.executable, "-B", str(script), "--step", name]


def no_files(_context: object) -> list[Path]:
    return []


def missing_tj_hook(_command: list[str], **_options: object) -> None:
    raise FileNotFoundError("tj_hook")


def test_a_current_session_runs_unchanged(machine: Machine) -> None:
    machine.registry(PIN)
    machine.codex_build("aaaaaaaaaaaa")
    machine.converge()
    assert machine.hook("claude", "UserPromptSubmit") is None
    assert machine.hook("codex", "UserPromptSubmit") is None
    assert machine.started.commands == []


def test_a_stale_install_without_tj_hook_says_how_to_refresh(machine: Machine) -> None:
    machine.registry(OLD)
    machine.converge()
    machine.monkeypatch.setattr(process, "start", missing_tj_hook)
    assert machine.hook("claude", "UserPromptSubmit") == (
        "tj-agents plugins are behind and tj_hook is missing; "
        f"run {shlex.join(latest_plugins.refresh_command())}."
    )


def test_a_running_refresh_is_not_started_again(machine: Machine) -> None:
    machine.registry(OLD)
    machine.converge()
    with jobs.held(machine.state, jobs.Job.REFRESH):
        assert machine.hook("claude", "UserPromptSubmit") == UPDATING_CLAUDE
    assert machine.started.commands == []


def test_a_recent_failed_refresh_is_reported_instead_of_retried(
    machine: Machine,
) -> None:
    machine.registry(OLD)
    machine.converge()
    write_json(
        machine.state / "plugin-refresh.json",
        {"finished": time.time(), "failures": ["claude: list: boom"]},
    )
    assert machine.hook("claude", "UserPromptSubmit") == (
        "tj-agents plugin refresh failed (claude: list: boom); "
        f"run {shlex.join(latest_plugins.refresh_command())}."
    )
    assert machine.started.commands == []


def test_a_stale_codex_build_is_reported_at_prompt_and_session_start(
    machine: Machine,
) -> None:
    machine.registry(PIN)
    machine.codex_build("bbbbbbbbbbbb")
    machine.converge()
    with jobs.held(machine.state, jobs.Job.REFRESH):
        assert machine.hook("codex", "UserPromptSubmit") == UPDATING_CODEX
        assert machine.hook("codex", "SessionStart") == UPDATING_CODEX


def test_only_the_running_session_behind_asks_for_a_reload(machine: Machine) -> None:
    machine.registry(PIN)
    machine.converge()
    agent = machine.claude / "plugins/cache/tj-agents/agent"
    running = agent / f"{OLD[:12]}-d086935e"
    running.mkdir(parents=True)
    assert machine.hook("claude", "UserPromptSubmit", running) == (
        "Plugins updated: type /reload-plugins."
    )
    current = agent / f"{PIN[:12]}-d086935e"
    current.mkdir(parents=True)
    assert machine.hook("claude", "UserPromptSubmit", current) is None


def test_only_the_running_codex_session_behind_asks_for_a_restart(
    machine: Machine,
) -> None:
    machine.registry(PIN)
    machine.codex_build("aaaaaaaaaaaa")
    machine.converge()
    older = machine.home / "running-codex"
    write_json(
        older / ".codex-plugin/plugin.json",
        {"name": "agent", "version": "cccccccccccc"},
    )
    expected = "Plugins updated: restart this session."
    assert machine.hook("codex", "UserPromptSubmit", older) == expected
    deleted = machine.home / "deleted-build"
    assert machine.hook("codex", "UserPromptSubmit", deleted) == expected


def test_a_drifted_tj_item_receipt_is_reported_by_name(machine: Machine) -> None:
    machine.registry(PIN)
    machine.converge()
    write_tool_receipt(machine.home / "uv-tools", [{**PINS["tool"], "revision": OLD}])
    tool = PINS["tool"]
    assert machine.hook("claude", "UserPromptSubmit") == (
        "Updating tj-agents tj item CLI "
        f"(tj-work-items is {tool['repository']}@{OLD[:12]}"
        f", pinned {tool['repository']}@{tool['revision'][:12]}) in the background."
    )
    assert machine.started.commands == [install_step("tj item CLI")]


def test_a_selected_profile_missing_from_the_receipt_is_reported(
    machine: Machine,
) -> None:
    machine.registry(PIN)
    machine.converge()
    plugin, pin = next(iter(PINS["profiles"].items()))
    name, _, marketplace = plugin.partition("@")
    (machine.home / "codex/plugins/cache" / marketplace / name / "local").mkdir(
        parents=True
    )
    reason = machine.hook("claude", "UserPromptSubmit")
    assert f"{pin['package']} is not installed" in reason


def plugins_behind(home: Path) -> None:
    pin_plugin(home / "claude", "base", PIN)
    install_plugin(home / "claude", "base", OLD)


def tj_item_drifted(home: Path) -> None:
    write_tool_receipt(home / "uv-tools", [{**PINS["tool"], "revision": OLD}])


def copies_drifted(home: Path) -> None:
    pin_plugin(home / "claude", "personal", PIN)
    install_plugin(home / "claude", "personal", PIN, converge=True)
    copies = [plugin_copies.Source("personal@tj-agents", OLD, home)]
    plugin_copies.record(home / "state", copies)


def converging_plugin_behind(home: Path) -> None:
    pin_plugin(home / "claude", "personal", PIN)
    install_plugin(home / "claude", "personal", OLD, converge=True)
    copies = [plugin_copies.Source("personal@tj-agents", OLD, home)]
    plugin_copies.record(home / "state", copies)


def current_machine(home: Path) -> None:
    pin_plugin(home / "claude", "personal", PIN)
    install_plugin(home / "claude", "personal", PIN, converge=True)
    copies = [plugin_copies.Source("personal@tj-agents", PIN, home)]
    plugin_copies.record(home / "state", copies)


@pytest.mark.parametrize(
    ("cause", "fixes"),
    [
        (plugins_behind, ["refresh"]),
        (tj_item_drifted, ["tj item CLI"]),
        (copies_drifted, ["plugin copies"]),
        (converging_plugin_behind, ["refresh"]),
        (current_machine, []),
    ],
)
def test_the_stale_check_starts_only_the_fix_for_its_cause(
    machine: Machine,
    cause: Callable[[Path], None],
    fixes: list[str],
) -> None:
    machine.registry(PIN)
    cause(machine.home)
    machine.converge()
    refusal = machine.reason()
    expected = [REFRESH if fix == "refresh" else install_step(fix) for fix in fixes]
    assert machine.started.commands == expected
    assert (refusal is None) == (fixes == [])


def test_an_agent_update_starts_only_the_launch_runtime_step(machine: Machine) -> None:
    claude = machine.claude
    old, new = "c" * 40, "d" * 40
    pin_plugin(claude, "agent", old)
    agent_root(install_plugin(claude, "agent", old), "first build")
    machine.converge()
    assert machine_state.drift(machine.state) == {}
    pin_plugin(claude, "agent", new)
    updated = agent_root(install_plugin(claude, "agent", new), "second build")
    assert machine.reason() is not None
    assert machine_state.packaged("install.py").parents[3] == updated
    assert machine.started.commands == [install_step("launch-check runtime")]


def test_only_cheap_steps_take_part_in_the_stale_check() -> None:
    watched = [step.name for step in install.STEPS if step.watch is not None]
    assert watched == DRIFT_STEPS


def test_the_stale_check_never_runs_a_subprocess(machine: Machine) -> None:
    def forbidden(*_arguments: object, **_options: object) -> Any:
        raise AssertionError("the stale check ran a subprocess")

    machine.registry(PIN)
    machine.converge()
    machine.context.run = forbidden
    machine.monkeypatch.setattr(subprocess, "Popen", forbidden)
    machine.monkeypatch.setattr(install.process, "run", forbidden)
    machine.monkeypatch.setattr(install.agent_store.process, "run", forbidden)
    copies_drifted(machine.home)
    tj_item_drifted(machine.home)
    assert set(machine_state.drift(machine.state)) == {"tj item CLI", "plugin copies"}


def test_a_slow_step_never_runs_from_the_hook(
    machine: Machine, monkeypatch: pytest.MonkeyPatch
) -> None:
    def slow(_context: object) -> list[str]:
        raise AssertionError("a subprocess step ran from the hook")

    steps = (
        install.Step("slow", slow, slow),
        install.Step("cheap", lambda _context: ["drifted"], slow, watch=no_files),
    )
    monkeypatch.setattr(install, "STEPS", steps)
    assert machine_state.drift(machine.state) == {"cheap": ["drifted"]}


def test_plugin_copies_drift_names_the_converged_and_pinned_builds(
    machine: Machine,
) -> None:
    machine.registry(PIN)
    machine.converge()
    copies_drifted(machine.home)
    assert machine.reason() == (
        "Updating tj-agents plugin copies (personal@tj-agents copies are from "
        f"{OLD[:12]}, pinned {PIN[:12]}) in the background."
    )


def test_a_running_install_is_not_started_again(machine: Machine) -> None:
    machine.registry(PIN)
    machine.converge()
    copies_drifted(machine.home)
    with jobs.held(machine.state, jobs.Job.INSTALL):
        assert machine.reason() is not None
    assert machine.started.commands == []


def test_a_recent_failed_install_is_reported_instead_of_retried(
    machine: Machine,
) -> None:
    machine.registry(PIN)
    machine.converge()
    copies_drifted(machine.home)
    jobs.record(machine.state, jobs.Job.INSTALL, ["plugin copies: boom"])
    refusal = machine.reason()
    assert machine.started.commands == []
    assert refusal is not None
    assert refusal.startswith(
        "tj-agents machine install failed (plugin copies: boom); run "
    )
    assert refusal.endswith("--step 'plugin copies'.")


def test_jobs_start_detached_and_hidden_through_the_shared_process_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict[str, object]] = []

    def start(command: list[str], **options: object) -> None:
        calls.append({"command": command, **options})

    monkeypatch.setattr(process, "start", start)
    assert jobs.start(tmp_path, jobs.Job.REFRESH, REFRESH)
    (call,) = calls
    assert call["command"] == REFRESH
    assert call["stdin"] == subprocess.DEVNULL
    if sys.platform == "win32":
        assert call["creationflags"] == subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        assert call["start_new_session"] is True
    assert (tmp_path / "plugin-refresh.log").is_file()


def test_jobs_report_a_missing_tj_hook_instead_of_raising(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(process, "start", missing_tj_hook)
    assert not jobs.start(tmp_path, jobs.Job.INSTALL, INSTALL)


def clean_session(machine: Machine) -> None:
    machine.registry(PIN)
    machine.converge()
    assert machine.hook("claude", "SessionStart") is None
    assert (machine.state / machine_state.FINGERPRINT).is_file()


def test_a_prompt_with_an_unchanged_fingerprint_skips_install_and_its_checks(
    machine: Machine,
) -> None:
    clean_session(machine)
    machine.monkeypatch.setitem(sys.modules, "install", None)
    assert machine.hook("claude", "UserPromptSubmit") is None
    assert machine.started.commands == []


def test_a_plugin_update_makes_the_next_prompt_run_the_machine_check(
    machine: Machine,
) -> None:
    clean_session(machine)
    new = "e" * 40
    pin_plugin(machine.claude, "agent", new)
    agent_root(install_plugin(machine.claude, "agent", new), "updated build")
    assert machine.hook("claude", "UserPromptSubmit") is not None
    assert machine.started.commands == [install_step("launch-check runtime")]


def test_a_touched_receipt_makes_the_next_prompt_run_the_machine_check(
    machine: Machine,
) -> None:
    clean_session(machine)
    receipt = machine.home / "uv-tools/tj-work-items/uv-receipt.toml"
    before = receipt.stat().st_mtime_ns
    write_tool_receipt(machine.home / "uv-tools", [{**PINS["tool"], "revision": OLD}])
    later = before + 2_000_000_000
    os.utime(receipt, ns=(later, later))
    assert machine.hook("claude", "UserPromptSubmit") is not None
    assert machine.started.commands == [install_step("tj item CLI")]


def test_session_start_always_runs_the_machine_check(machine: Machine) -> None:
    clean_session(machine)
    probe = install.Step(
        "probe", lambda _context: ["drifted"], lambda _context: [], watch=no_files
    )
    machine.monkeypatch.setattr(install, "STEPS", (probe,))
    assert machine.hook("claude", "UserPromptSubmit") is None
    assert machine.started.commands == []
    assert machine.hook("claude", "SessionStart") is not None
    assert machine.started.commands == [install_step("probe")]


def test_a_step_whose_watch_fails_is_reported_as_drift(machine: Machine) -> None:
    clean_session(machine)

    def broken(_context: install.Context) -> list[Path]:
        raise OSError("pins unreadable")

    probe = install.Step(
        "probe", lambda _context: [], lambda _context: [], watch=broken
    )
    machine.monkeypatch.setattr(install, "STEPS", (probe,))
    assert machine.hook("claude", "SessionStart") is not None
    assert machine.started.commands == [install_step("probe")]
    assert not (machine.state / machine_state.FINGERPRINT).exists()


def test_a_session_on_an_older_build_leaves_machine_repair_to_the_installed_build(
    machine: Machine,
) -> None:
    machine.registry(PIN)
    machine.converge()
    write_json(machine.install / ".codex-plugin/plugin.json", {"version": "new-build"})
    running = machine.home / "older/plugin"
    write_json(running / ".codex-plugin/plugin.json", {"version": "old-build"})
    machine.monkeypatch.setattr(
        machine_state, "RUNTIME", running / ".agents/agent/runtime"
    )
    machine.monkeypatch.setattr(machine_state, "installed_build", INSTALLED_BUILD)
    write_tool_receipt(machine.home / "uv-tools", [{**PINS["tool"], "revision": OLD}])
    assert not machine_state.installed_build()
    assert machine.reason() is None
    assert machine.started.commands == []
