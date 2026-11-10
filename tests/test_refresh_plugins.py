import importlib.util
import io
import json
from pathlib import Path
import os
import shutil
import subprocess
import sys
import tempfile
from typing import Any
import unittest
from unittest import mock

from fake_machine import (
    Started,
    agent_root,
    converge,
    environment,
    install_plugin,
    machine_context,
    pin_plugin,
)
import install
import latest_plugins
import machine_state
import plugin_copies
import pytest
from tj_agent.host import Host


ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "plugins/agent/.agents/agent/runtime"
sys.path.insert(0, str(RUNTIME))


def load(name):
    spec = importlib.util.spec_from_file_location(name, RUNTIME / "tj_plugins.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


REFRESH = load("refresh")

from codex import hook_trust as codex_hook_trust  # noqa: E402
from codex import launch as codex_launch  # noqa: E402


@pytest.fixture(autouse=True)
def no_codex_daemon(monkeypatch: pytest.MonkeyPatch) -> list[tuple[object, ...]]:
    trusted: list[tuple[object, ...]] = []
    monkeypatch.setattr(codex_launch, "liveness", dict)
    monkeypatch.setattr(codex_launch, "executable", lambda: "codex")
    monkeypatch.setattr(
        codex_hook_trust,
        "trust_installed",
        lambda *arguments: trusted.append(arguments) or {},
    )
    return trusted


def completed(
    returncode: int = 0, stdout: str = "", stderr: str = ""
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


class RefreshPluginsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.claude_home = Path(temporary.name) / "claude"
        self.codex_home = Path(temporary.name) / "codex"
        self.state = Path(temporary.name) / "state"
        homes = _patched_homes(Path(temporary.name))
        homes.start()
        self.addCleanup(homes.stop)
        self.started = Started()
        starts = mock.patch.object(REFRESH.jobs.process, "start", self.started)
        starts.start()
        self.addCleanup(starts.stop)
        machine = mock.patch.object(machine_state, "drift", return_value={})
        machine.start()
        self.addCleanup(machine.stop)

    def install(self, plugin, revision, pin, label=None, codex_label=None):
        marketplace = (
            self.claude_home
            / "plugins"
            / "marketplaces"
            / "tj-agents"
            / ".claude-plugin"
        )
        marketplace.mkdir(parents=True, exist_ok=True)
        catalog = marketplace / "marketplace.json"
        entries = (
            json.loads(catalog.read_text(encoding="utf-8"))["plugins"]
            if catalog.exists()
            else []
        )
        catalog.write_text(
            json.dumps(
                {"plugins": [*entries, {"name": plugin, "source": {"sha": pin}}]}
            ),
            encoding="utf-8",
        )
        root = (
            self.claude_home
            / "plugins"
            / "cache"
            / "tj-agents"
            / plugin
            / revision[:12]
        )
        (root / ".codex-plugin").mkdir(parents=True)
        harness = {"plugin": f"tj-agents/{plugin}", "requires": {}}
        (root / "harness.json").write_text(json.dumps(harness), encoding="utf-8")
        if label:
            (root / ".codex-plugin" / "plugin.json").write_text(
                json.dumps({"version": label}), encoding="utf-8"
            )
        registry = self.claude_home / "plugins" / "installed_plugins.json"
        rows = (
            json.loads(registry.read_text(encoding="utf-8"))["plugins"]
            if registry.exists()
            else {}
        )
        rows[f"{plugin}@tj-agents"] = [
            {"scope": "user", "gitCommitSha": revision, "installPath": str(root)}
        ]
        registry.write_text(json.dumps({"plugins": rows}), encoding="utf-8")
        if codex_label:
            build = (
                self.codex_home
                / "plugins"
                / "cache"
                / "tj-agents"
                / plugin
                / codex_label
            )
            shutil.copytree(root, build)
            (build / ".codex-plugin" / "plugin.json").write_text(
                json.dumps({"version": codex_label}), encoding="utf-8"
            )

    def test_one_failing_update_is_reported_and_the_rest_still_run(self):
        listing = json.dumps(
            [
                {"id": "agent@tj-agents", "scope": "user"},
                {"id": "base@tj-agents", "scope": "user"},
            ]
        )

        def run(command, **_kwargs):
            if command[:3] == ["claude", "plugin", "list"]:
                return completed(0, listing)
            if (
                command[:3] == ["claude", "plugin", "update"]
                and "agent@tj-agents" in command
            ):
                return completed(1, "", "update failed")
            if command[:2] == ["claude", "agents"]:
                return completed(
                    0,
                    json.dumps(
                        [
                            {"kind": "background", "id": "live1", "pid": 7},
                            {"kind": "background", "id": "dead1"},
                            {"kind": "interactive", "pid": 8, "name": "tab"},
                        ]
                    ),
                )
            return completed(0)

        with mock.patch.object(REFRESH.process, "run", side_effect=run) as spawned:
            self.assertEqual(1, REFRESH.main(["update", "--host", "claude"]))
        commands = [call.args[0] for call in spawned.call_args_list]
        self.assertIn(
            [
                "claude",
                "plugin",
                "update",
                "agent@tj-agents",
                "--scope",
                "user",
                "--json",
                "-y",
            ],
            commands,
        )
        self.assertIn(
            [
                "claude",
                "plugin",
                "update",
                "base@tj-agents",
                "--scope",
                "user",
                "--json",
                "-y",
            ],
            commands,
        )
        self.assertEqual(
            [],
            [c for c in commands if c[:2] == ["claude", "respawn"]],
        )

    def test_claude_updates_use_scope_user_only_for_user_scope_rows_of_selected_marketplaces(
        self,
    ):
        listing = json.dumps(
            [
                {"id": "agent@tj-agents", "scope": "user"},
                {"id": "agent@tj-agents", "scope": "project"},
                {"id": "other@elsewhere", "scope": "user"},
                {"id": "base@agents", "scope": "user"},
            ]
        )

        def run(command, **_kwargs):
            if command[:3] == ["claude", "plugin", "list"]:
                return completed(0, listing)
            return completed(0)

        with mock.patch.object(REFRESH.process, "run", side_effect=run) as spawned:
            self.assertEqual(0, REFRESH.main(["update", "--host", "claude"]))
        commands = [call.args[0] for call in spawned.call_args_list]
        updates = [
            command
            for command in commands
            if command[:3] == ["claude", "plugin", "update"]
        ]
        self.assertEqual(
            [
                [
                    "claude",
                    "plugin",
                    "update",
                    "agent@tj-agents",
                    "--scope",
                    "user",
                    "--json",
                    "-y",
                ],
                [
                    "claude",
                    "plugin",
                    "update",
                    "base@agents",
                    "--scope",
                    "user",
                    "--json",
                    "-y",
                ],
            ],
            updates,
        )

    def test_codex_upgrades_each_marketplace_then_adds_each_installed_plugin(self):
        listing = json.dumps(
            {
                "installed": [
                    {"pluginId": "agent@tj-agents", "marketplaceName": "tj-agents"},
                    {"pluginId": "base@agents", "marketplaceName": "agents"},
                    {"pluginId": "other@elsewhere", "marketplaceName": "elsewhere"},
                ]
            }
        )

        def run(command, **_kwargs):
            if command[:3] == ["codex", "plugin", "list"]:
                return completed(0, listing)
            return completed(0)

        with mock.patch.object(REFRESH.process, "run", side_effect=run) as spawned:
            self.assertEqual(0, REFRESH.main(["update", "--host", "codex"]))
        commands = [call.args[0] for call in spawned.call_args_list]
        command_fn = REFRESH.codex_installed.codex_command
        self.assertEqual(
            [
                command_fn("plugin", "marketplace", "upgrade", "agents"),
                command_fn("plugin", "marketplace", "upgrade", "tj-agents"),
            ],
            [
                command
                for command in commands
                if command[:3] == ["codex", "plugin", "marketplace"]
            ],
        )
        self.assertEqual(
            [
                command_fn("plugin", "add", "agent@tj-agents"),
                command_fn("plugin", "add", "base@agents"),
            ],
            [
                command
                for command in commands
                if command[:3] == ["codex", "plugin", "add"]
            ],
        )

    def test_claude_skips_plugins_already_at_their_pin(self):
        self.install("agent", "a" * 40, "a" * 40)
        self.install("base", "b" * 40, "c" * 40)
        listing = json.dumps(
            [
                {"id": "agent@tj-agents", "scope": "user"},
                {"id": "base@tj-agents", "scope": "user"},
            ]
        )

        def run(command, **_kwargs):
            return (
                completed(0, listing)
                if command[:3] == ["claude", "plugin", "list"]
                else completed(0, "[]")
            )

        with mock.patch.object(REFRESH.process, "run", side_effect=run) as spawned:
            self.assertEqual(0, REFRESH.main(["update", "--host", "claude"]))
        updates = [
            call.args[0]
            for call in spawned.call_args_list
            if call.args[0][:3] == ["claude", "plugin", "update"]
        ]
        self.assertEqual(
            [
                [
                    "claude",
                    "plugin",
                    "update",
                    "base@tj-agents",
                    "--scope",
                    "user",
                    "--json",
                    "-y",
                ]
            ],
            updates,
        )

    def test_codex_skips_only_plugins_whose_cached_content_matches_the_pinned_install(
        self,
    ):
        self.install("agent", "a" * 40, "a" * 40, label="label1", codex_label="label1")
        self.install("base", "b" * 40, "b" * 40, label="label2", codex_label="old")
        self.install("cli", "c" * 40, "c" * 40)
        listing = json.dumps(
            {
                "installed": [
                    {"pluginId": f"{name}@tj-agents", "marketplaceName": "tj-agents"}
                    for name in ("agent", "base", "cli")
                ]
            }
        )

        def run(command, **_kwargs):
            return (
                completed(0, listing)
                if command[:3] == ["codex", "plugin", "list"]
                else completed(0)
            )

        with mock.patch.object(REFRESH.process, "run", side_effect=run) as spawned:
            self.assertEqual(0, REFRESH.main(["update", "--host", "codex"]))
        adds = [
            call.args[0]
            for call in spawned.call_args_list
            if call.args[0][:3] == ["codex", "plugin", "add"]
        ]
        command_fn = REFRESH.codex_installed.codex_command
        self.assertEqual(
            [
                command_fn("plugin", "add", "base@tj-agents"),
                command_fn("plugin", "add", "cli@tj-agents"),
            ],
            adds,
        )

    def test_a_held_refresh_lock_makes_a_second_refresh_exit_without_running(self):
        job = REFRESH.jobs.Job.UPDATE
        with (
            REFRESH.jobs.held(self.state, job) as first,
            mock.patch.object(REFRESH.process, "run") as spawned,
        ):
            self.assertTrue(first)
            self.assertEqual(0, REFRESH.main(["update", "--host", "claude"]))
        spawned.assert_not_called()
        self.assertFalse(REFRESH.jobs.lock_path(self.state, job).exists())

    def test_a_refresh_records_its_failures_for_the_stale_check(self):
        def run(command, **_kwargs):
            return (
                completed(1, "", "boom")
                if command[:3] == ["claude", "plugin", "list"]
                else completed(0, "[]")
            )

        with mock.patch.object(REFRESH.process, "run", side_effect=run):
            self.assertEqual(1, REFRESH.main(["update", "--host", "claude"]))
        job = REFRESH.jobs.Job.UPDATE
        self.assertEqual(
            "claude: list: boom", REFRESH.jobs.recent_failure(self.state, job)
        )
        self.assertFalse(REFRESH.jobs.running(self.state, job))


def _patched_homes(home: Path) -> Any:
    return mock.patch.dict(os.environ, environment(home))


def _current_machine(home: Path) -> Any:
    claude = home / "claude"
    pin_plugin(claude, "agent", AGENT_PIN)
    agent_root(install_plugin(claude, "agent", AGENT_PIN), "agent build")
    context = machine_context(home)
    converge(context, machine_state.packaged("install.py").parents[3])
    return mock.patch.object(install, "machine_context", return_value=context)


def test_codex_commands_carry_marketplace_overrides_and_no_cwd(tmp_path: Path) -> None:
    listing = json.dumps(
        {"installed": [{"pluginId": "agent@tj-agents", "marketplaceName": "tj-agents"}]}
    )

    def run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        if command[:3] == ["codex", "plugin", "list"]:
            return completed(0, listing)
        return completed(0)

    with (
        _patched_homes(tmp_path),
        mock.patch.object(REFRESH.process, "run", side_effect=run) as spawned,
        mock.patch.object(REFRESH.jobs.process, "start", Started()),
        mock.patch.object(machine_state, "drift", return_value={}),
    ):
        assert REFRESH.main(["update", "--host", "codex"]) == 0
    overrides = REFRESH.codex_installed.MARKETPLACE_OVERRIDES
    codex_calls = [
        call for call in spawned.call_args_list if call.args[0][0] == "codex"
    ]
    assert codex_calls
    for call in codex_calls:
        assert tuple(call.args[0][-len(overrides) :]) == overrides
        assert call.kwargs.get("cwd") is None


def test_codex_refresh_fails_with_no_tj_agents_rows_after_listing(
    tmp_path: Path,
) -> None:
    listing = json.dumps(
        {"installed": [{"pluginId": "other@elsewhere", "marketplaceName": "elsewhere"}]}
    )

    def run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        if command[:3] == ["codex", "plugin", "list"]:
            return completed(0, listing)
        return completed(0)

    with (
        _patched_homes(tmp_path),
        mock.patch.object(REFRESH.process, "run", side_effect=run),
        mock.patch.object(REFRESH.jobs.process, "start", Started()),
        mock.patch.object(machine_state, "drift", return_value={}),
    ):
        assert REFRESH.main(["update", "--host", "codex"]) == 1
        failure = REFRESH.jobs.recent_failure(
            tmp_path / "state", REFRESH.jobs.Job.UPDATE
        )
        assert failure == "codex: list: no tj-agents plugins installed"


OLD_PIN = "a" * 40
NEW_PIN = "b" * 40
AGENT_PIN = "c" * 40


def test_refresh_runs_only_plugin_commands_on_both_hosts(tmp_path: Path) -> None:
    claude = tmp_path / "claude"
    pin_plugin(claude, "personal", OLD_PIN)
    install_plugin(claude, "personal", NEW_PIN, converge=True)
    claude_listing = json.dumps([{"id": "personal@tj-agents", "scope": "user"}])
    codex_listing = json.dumps(
        {
            "installed": [
                {"pluginId": "personal@tj-agents", "marketplaceName": "tj-agents"}
            ]
        }
    )

    def run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        if command[:3] == ["claude", "plugin", "list"]:
            return completed(0, claude_listing)
        if command[:3] == ["codex", "plugin", "list"]:
            return completed(0, codex_listing)
        return completed(0, "[]")

    started = Started()
    with (
        _patched_homes(tmp_path),
        _current_machine(tmp_path),
        mock.patch.object(REFRESH.process, "run", side_effect=run) as spawned,
        mock.patch.object(REFRESH.jobs.process, "start", started),
    ):
        assert REFRESH.main(["update", "--host", "claude"]) == 0
        assert REFRESH.main(["update", "--host", "codex"]) == 0
    commands = [call.args[0] for call in spawned.call_args_list]
    assert {command[0] for command in commands if command[0] != "tj"} == {
        "claude",
        "codex",
    }
    assert all(
        command[:2] == ["tj", "restart"] for command in commands if command[0] == "tj"
    )
    assert not [command for command in commands if "converge" in command]
    assert started.commands == []


def test_a_downloaded_pin_is_repaired_by_install_on_the_next_prompt(
    tmp_path: Path,
) -> None:
    claude = tmp_path / "claude"
    state = tmp_path / "state"
    pin_plugin(claude, "personal", OLD_PIN)
    install_plugin(claude, "personal", OLD_PIN, converge=True)
    listing = json.dumps([{"id": "personal@tj-agents", "scope": "user"}])

    def run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        if command == ["claude", "plugin", "marketplace", "update", "tj-agents"]:
            pin_plugin(claude, "personal", NEW_PIN)
        elif command[:3] == ["claude", "plugin", "list"]:
            return completed(0, listing)
        elif command[:4] == ["claude", "plugin", "update", "personal@tj-agents"]:
            install_plugin(claude, "personal", NEW_PIN, converge=True)
        return completed(0, "[]")

    converged: list[tuple[list[str], Path]] = []

    def converge_copies(
        command: list[str], **options: Any
    ) -> subprocess.CompletedProcess[str]:
        converged.append((command, options["cwd"]))
        return completed(0)

    started = Started()
    step = next(item for item in install.STEPS if item.name == "plugin copies")
    with (
        _patched_homes(tmp_path),
        _current_machine(tmp_path) as current,
        mock.patch.object(REFRESH.process, "run", side_effect=run),
        mock.patch.object(REFRESH.jobs.process, "start", started),
    ):
        context = current.return_value
        context.run = converge_copies
        installer = machine_state.packaged("install.py")
        copies = [sys.executable, "-B", str(installer), "--step", "plugin copies"]
        plugin_copies.record(state, plugin_copies.sources())
        assert latest_plugins.reason(Host.CLAUDE, state, full=True) is None

        pin_plugin(claude, "personal", NEW_PIN)
        assert REFRESH.main(["update", "--host", "claude"]) == 0
        assert started.commands == []

        assert latest_plugins.reason(Host.CLAUDE, state, full=True) is not None
        with REFRESH.jobs.held(state, REFRESH.jobs.Job.INSTALL):
            refusal = latest_plugins.reason(Host.CLAUDE, state, full=True)
        assert refusal is not None
        assert refusal.startswith("Updating tj-agents plugin copies")
        assert started.commands == [copies]

        assert (
            install.run_steps([step], context, False, io.StringIO(), io.StringIO()) == 0
        )
        assert latest_plugins.reason(Host.CLAUDE, state, full=True) is None
    root = (claude / "plugins/cache/tj-agents/personal" / NEW_PIN[:12]).resolve()
    script = [sys.executable, "-B", str(root / "scripts/setup.py"), "converge"]
    assert [(command, cwd.resolve()) for command, cwd in converged] == [(script, root)]
    assert started.commands == [copies]


if __name__ == "__main__":
    unittest.main()


class FakeDaemon:
    def __init__(self, failing: frozenset[str] = frozenset()) -> None:
        self.requests: list[tuple[str, dict[str, object]]] = []
        self.failing = failing

    def __enter__(self) -> "FakeDaemon":
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def request(self, method: str, params: dict[str, object]) -> dict[str, object]:
        self.requests.append((method, params))
        if params.get("pluginName") in self.failing:
            raise RuntimeError(f"Codex {method} failed: boom")
        return {}


def daemon_refresh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, daemon: FakeDaemon
) -> tuple[int, list[list[str]], list[tuple[object, ...]]]:
    listing = json.dumps(
        {
            "installed": [
                {"pluginId": "agent@tj-agents", "marketplaceName": "tj-agents"},
                {"pluginId": "base@tj-agents", "marketplaceName": "tj-agents"},
            ]
        }
    )
    roots = json.dumps(
        {"marketplaces": [{"name": "tj-agents", "root": str(tmp_path / "market")}]}
    )

    def run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        if command[:3] == ["codex", "plugin", "list"]:
            return completed(0, listing)
        if command[:4] == ["codex", "plugin", "marketplace", "list"]:
            return completed(0, roots)
        return completed(0)

    trusted: list[tuple[object, ...]] = []
    monkeypatch.setattr(codex_launch, "liveness", lambda: {"thread": object()})
    monkeypatch.setattr(codex_launch, "daemon", lambda _timeout: daemon)
    monkeypatch.setattr(codex_hook_trust, "pending", lambda _state: ["trust"])
    monkeypatch.setattr(
        codex_hook_trust,
        "trust_installed",
        lambda *arguments: trusted.append(arguments) or {},
    )
    with (
        _patched_homes(tmp_path),
        mock.patch.object(REFRESH.process, "run", side_effect=run) as spawned,
        mock.patch.object(REFRESH.jobs.process, "start", Started()),
        mock.patch.object(machine_state, "drift", return_value={}),
    ):
        code = REFRESH.main(["update", "--host", "codex"])
    return code, [call.args[0] for call in spawned.call_args_list], trusted


def test_open_codex_sessions_reload_through_the_daemon_instead_of_a_cli_add(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    daemon = FakeDaemon()
    code, commands, trusted = daemon_refresh(tmp_path, monkeypatch, daemon)
    assert code == 0
    assert not [
        command for command in commands if command[:3] == ["codex", "plugin", "add"]
    ]
    manifest = str(tmp_path / "market" / ".agents" / "plugins" / "marketplace.json")
    assert daemon.requests == [
        ("plugin/install", {"marketplacePath": manifest, "pluginName": "agent"}),
        ("plugin/install", {"marketplacePath": manifest, "pluginName": "base"}),
        ("config/batchWrite", {"edits": [], "reloadUserConfig": True}),
    ]
    assert trusted == [
        ("codex", tmp_path / "state", REFRESH.codex_installed.MARKETPLACE_OVERRIDES)
    ]


def test_a_failed_daemon_install_is_reported_and_the_rest_still_reload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    daemon = FakeDaemon(failing=frozenset({"agent"}))
    code, _commands, _trusted = daemon_refresh(tmp_path, monkeypatch, daemon)
    assert code == 1
    assert [
        params.get("pluginName")
        for method, params in daemon.requests
        if method == "plugin/install"
    ] == [
        "agent",
        "base",
    ]
    assert daemon.requests[-1] == (
        "config/batchWrite",
        {"edits": [], "reloadUserConfig": True},
    )
    failure = REFRESH.jobs.recent_failure(tmp_path / "state", REFRESH.jobs.Job.UPDATE)
    assert failure is not None and failure.startswith(
        "codex: agent@tj-agents: Codex plugin/install failed"
    )


def test_the_cli_path_trusts_new_hooks_once_after_adding_plugins(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    no_codex_daemon: list[tuple[object, ...]],
) -> None:
    listing = json.dumps(
        {"installed": [{"pluginId": "agent@tj-agents", "marketplaceName": "tj-agents"}]}
    )
    order: list[str] = []

    def run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        order.append(" ".join(command[:3]))
        if command[:3] == ["codex", "plugin", "list"]:
            return completed(0, listing)
        return completed(0)

    monkeypatch.setattr(codex_hook_trust, "pending", lambda _state: ["trust"])
    monkeypatch.setattr(
        codex_hook_trust,
        "trust_installed",
        lambda *arguments: (
            order.append("trust") or no_codex_daemon.append(arguments) or {}
        ),
    )
    with (
        _patched_homes(tmp_path),
        mock.patch.object(REFRESH.process, "run", side_effect=run),
        mock.patch.object(REFRESH.jobs.process, "start", Started()),
        mock.patch.object(machine_state, "drift", return_value={}),
    ):
        assert REFRESH.main(["update", "--host", "codex"]) == 0
    assert order[-2:] == ["codex plugin add", "trust"]
    assert no_codex_daemon == [
        ("codex", tmp_path / "state", REFRESH.codex_installed.MARKETPLACE_OVERRIDES)
    ]


def test_nothing_is_trusted_when_every_installed_build_is_already_trusted(
    tmp_path: Path, no_codex_daemon: list[tuple[object, ...]]
) -> None:
    listing = json.dumps(
        {"installed": [{"pluginId": "agent@tj-agents", "marketplaceName": "tj-agents"}]}
    )

    def run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return (
            completed(0, listing)
            if command[:3] == ["codex", "plugin", "list"]
            else completed(0)
        )

    with (
        _patched_homes(tmp_path),
        mock.patch.object(REFRESH.process, "run", side_effect=run),
        mock.patch.object(REFRESH.jobs.process, "start", Started()),
        mock.patch.object(machine_state, "drift", return_value={}),
    ):
        assert REFRESH.main(["update", "--host", "codex"]) == 0
    assert no_codex_daemon == []


def test_the_install_step_reports_untrusted_builds_and_trusts_them(
    tmp_path: Path, no_codex_daemon: list[tuple[object, ...]]
) -> None:
    context = install.Context(
        home=tmp_path, environ={}, windows=False, documents=tmp_path / "Documents"
    )
    build = context.codex_home / "plugins/cache/tj-agents/agent/build-3"
    (build / ".codex-plugin").mkdir(parents=True)
    (build / ".codex-plugin/plugin.json").write_text(
        '{"name": "agent", "hooks": "./hooks.json"}', encoding="utf-8"
    )
    (build / "hooks.json").write_text(
        '{"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "x"}]}]}}',
        encoding="utf-8",
    )
    with mock.patch.dict(os.environ, {"CODEX_HOME": str(context.codex_home)}):
        assert install.check_codex_hook_trust(context) == [
            "trust agent@tj-agents hooks"
        ]
        assert install.apply_codex_hook_trust(context) == [
            "trusted 0 tj-agents Codex hook(s)"
        ]
    assert no_codex_daemon == [
        (
            "codex",
            context.state_directory,
            REFRESH.codex_installed.MARKETPLACE_OVERRIDES,
        )
    ]


def test_a_project_scoped_codex_plugin_is_updated_and_stays_project_scoped(
    tmp_path: Path,
) -> None:
    listing = json.dumps(
        {"installed": [{"pluginId": "base@tj-agents", "marketplaceName": "tj-agents"}]}
    )
    codex_home = tmp_path / "codex"
    build = codex_home / "plugins/cache/tj-agents/agent/old-build/.codex-plugin"
    build.mkdir(parents=True)
    (build / "plugin.json").write_text('{"name": "agent"}', encoding="utf-8")
    staging = codex_home / "plugins/cache/tj-agents/plugin-install-AbC123/agent"
    staging.mkdir(parents=True)
    retired = codex_home / "plugins/cache/tj-agents/machine/old-build/.codex-plugin"
    retired.mkdir(parents=True)
    (retired / "plugin.json").write_text('{"name": "machine"}', encoding="utf-8")
    market = tmp_path / "market"
    (market / ".agents/plugins").mkdir(parents=True)
    (market / ".agents/plugins/marketplace.json").write_text(
        json.dumps({"plugins": [{"name": "agent"}, {"name": "base"}]}), encoding="utf-8"
    )
    roots = json.dumps({"marketplaces": [{"name": "tj-agents", "root": str(market)}]})
    config = codex_home / "config.toml"
    config.write_text('[plugins."base@tj-agents"]\nenabled = true\n', encoding="utf-8")

    def run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        if command[:3] == ["codex", "plugin", "list"]:
            return completed(0, listing)
        if command[:4] == ["codex", "plugin", "marketplace", "list"]:
            return completed(0, roots)
        added = command[:3] == ["codex", "plugin", "add"]
        if added and command[3] not in config.read_text(encoding="utf-8"):
            with config.open("a", encoding="utf-8") as stream:
                stream.write(f'\n[plugins."{command[3]}"]\nenabled = true\n')
        return completed(0)

    with (
        _patched_homes(tmp_path),
        mock.patch.object(REFRESH.process, "run", side_effect=run) as spawned,
        mock.patch.object(REFRESH.jobs.process, "start", Started()),
        mock.patch.object(machine_state, "drift", return_value={}),
    ):
        assert REFRESH.main(["update", "--host", "codex"]) == 0
    adds = [
        call.args[0][3]
        for call in spawned.call_args_list
        if call.args[0][:3] == ["codex", "plugin", "add"]
    ]
    assert adds == ["agent@tj-agents", "base@tj-agents"]
    text = config.read_text(encoding="utf-8")
    assert 'plugins."agent@tj-agents"' not in text
    assert 'plugins."base@tj-agents"' in text


@pytest.mark.parametrize("host", list(Host))
def test_refresh_selects_the_calling_host(
    host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    for candidate in Host:
        monkeypatch.delenv(candidate.session_variable, raising=False)
    monkeypatch.setenv(host.session_variable, "session")
    assert REFRESH.selected_host(None, os.environ) is host


def test_refresh_requires_a_host_when_session_context_is_ambiguous(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for host in Host:
        monkeypatch.setenv(host.session_variable, "session")
    with pytest.raises(ValueError, match="--host"):
        REFRESH.selected_host(None, os.environ)
    assert REFRESH.selected_host(Host.CLAUDE, os.environ) is Host.CLAUDE


def test_hook_refresh_pins_the_requested_host() -> None:
    assert latest_plugins.update_command(Host.CODEX)[-2:] == ["--host", "codex"]


def test_claude_requests_only_the_calling_wrapper_to_resume(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TJ_RUN_LAUNCH_ID", "this-tab")
    report = mock.Mock()
    REFRESH.claude_installed.restart(report, True)
    command = report.step.call_args.args[2]
    assert command[:3] == ["tj", "restart", "--reason"]
    report.run.assert_not_called()


@pytest.mark.parametrize("host", list(Host))
def test_current_plugins_spawn_no_subprocess(host: Host, tmp_path: Path) -> None:
    claude = tmp_path / "claude"
    pin_plugin(claude, "agent", AGENT_PIN)
    root = install_plugin(claude, "agent", AGENT_PIN)
    (root / ".codex-plugin").mkdir(parents=True, exist_ok=True)
    (root / ".codex-plugin/plugin.json").write_text(
        json.dumps({"name": "agent", "version": "current"}), encoding="utf-8"
    )
    shutil.copytree(root, tmp_path / "codex/plugins/cache/tj-agents/agent/current")
    with _patched_homes(tmp_path), mock.patch.object(REFRESH.process, "run") as spawned:
        assert REFRESH.main(["update", "--host", host.value]) == 0
    spawned.assert_not_called()


def test_immutable_codex_builds_need_only_published_version_metadata(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    version = "a" * 12
    (source / ".codex-plugin").mkdir(parents=True)
    (source / ".codex-plugin/plugin.json").write_text(
        json.dumps({"version": version}), encoding="utf-8"
    )
    with _patched_homes(tmp_path):
        installed = tmp_path / "codex/plugins/cache/tj-agents/agent" / version
        shutil.copytree(source, installed)
        digests = mock.Mock()
        assert REFRESH.codex_installed.build_current(
            "tj-agents", "agent", source, digests
        )
        digests.tree.assert_not_called()
        (installed / ".codex-plugin/plugin.json").write_text(
            '{"version":"local"}', encoding="utf-8"
        )
        assert not REFRESH.codex_installed.build_current(
            "tj-agents", "agent", source, digests
        )


def test_local_codex_builds_keep_the_standards_branch_content_check(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "skill.md").write_text("current", encoding="utf-8")
    with _patched_homes(tmp_path):
        installed = tmp_path / "codex/plugins/cache/tj-agents/agent/local"
        shutil.copytree(source, installed)
        digests = REFRESH.Digests(tmp_path / "state")
        assert REFRESH.codex_installed.build_current(
            "tj-agents", "agent", source, digests
        )
        (installed / "skill.md").write_text("outdated", encoding="utf-8")
        assert not REFRESH.codex_installed.build_current(
            "tj-agents", "agent", source, digests
        )


def test_current_downloads_activate_an_older_managed_claude_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    claude = tmp_path / "claude"
    pin_plugin(claude, "agent", AGENT_PIN)
    install_plugin(claude, "agent", AGENT_PIN)
    monkeypatch.setenv("TJ_RUN_LAUNCH_ID", "fixture")
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(tmp_path / "older-agent"))
    with (
        _patched_homes(tmp_path),
        mock.patch.object(REFRESH.process, "run", return_value=completed()) as run,
    ):
        assert REFRESH.main(["update", "--host", "claude"]) == 0
    assert len(run.call_args_list) == 1
    assert run.call_args.args[0][:3] == ["tj", "restart", "--reason"]


def test_a_missing_claude_install_cannot_be_current(tmp_path: Path) -> None:
    claude = tmp_path / "claude"
    pin_plugin(claude, "agent", AGENT_PIN)
    root = install_plugin(claude, "agent", AGENT_PIN)
    shutil.rmtree(root)
    with _patched_homes(tmp_path):
        assert not REFRESH.already_current(
            Host.CLAUDE, REFRESH.Digests(tmp_path / "state")
        )


def test_an_unknown_host_is_an_argument_error() -> None:
    with pytest.raises(SystemExit, match="2"):
        REFRESH.main(["update", "--host", "other"])
