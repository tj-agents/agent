from collections.abc import Callable
import importlib.util
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

from git_isolation import without_git_config
from fake_machine import (
    claude_payload,
    MARKETPLACE_REVISION,
    CodexPlugins,
    environment,
    install_plugin,
    pin_plugin,
    record_codex_plugin,
    recorded_failures,
    update_claude_plugin,
)
import install
import pytest
import required_plugins
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
READ_MARKETPLACE_REVISION = REFRESH.marketplace_revision

from codex import hook_trust as codex_hook_trust  # noqa: E402
from codex import launch as codex_launch  # noqa: E402


@pytest.fixture(autouse=True)
def no_codex_daemon(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> list[tuple[object, ...]]:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        REFRESH, "marketplace_revision", lambda *_arguments: MARKETPLACE_REVISION
    )
    trusted: list[tuple[object, ...]] = []
    monkeypatch.setattr(codex_launch, "liveness", lambda **_kwargs: {})
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
        claude_payload(root, plugin)
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
        self.install("agent", "a" * 40, "b" * 40)
        self.install("base", "a" * 40, "c" * 40)

        def run(command, **_kwargs):
            if command[:3] == ["claude", "plugin", "update"]:
                if "agent@tj-agents" in command:
                    return completed(1, "", "update failed")
                update_claude_plugin(self.claude_home, command)
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
        registry = self.claude_home / "plugins" / "installed_plugins.json"
        registry.parent.mkdir(parents=True)
        registry.write_text(
            json.dumps(
                {
                    "plugins": {
                        "agent@tj-agents": [{"scope": "user"}, {"scope": "project"}],
                        "other@elsewhere": [{"scope": "user"}],
                        "base@agents": [{"scope": "user"}],
                    }
                }
            ),
            encoding="utf-8",
        )

        def run(command, **_kwargs):
            return completed(0)

        with mock.patch.object(REFRESH.process, "run", side_effect=run) as spawned:
            self.assertEqual(0, REFRESH.main(["update", "--host", "claude"]))
        commands = [call.args[0] for call in spawned.call_args_list]
        updates = sorted(
            command
            for command in commands
            if command[:3] == ["claude", "plugin", "update"]
        )
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
        self.codex_home.mkdir(parents=True)
        (self.codex_home / "config.toml").write_text(
            '[marketplaces.agents]\nsource_type = "git"\n', encoding="utf-8"
        )
        inventory = CodexPlugins(
            self.codex_home,
            dict.fromkeys(
                ("agent@tj-agents", "base@agents", "other@elsewhere"), "local"
            ),
        )

        def run(command, **_kwargs):
            if command[:3] == ["codex", "plugin", "list"]:
                return completed(0, inventory.listing())
            if command[:3] == ["codex", "plugin", "add"]:
                inventory.install(command[3])
            return completed(0)

        with mock.patch.object(REFRESH.process, "run", side_effect=run) as spawned:
            self.assertEqual(0, REFRESH.main(["update", "--host", "codex"]))
        commands = [call.args[0] for call in spawned.call_args_list]
        command_fn = REFRESH.codex_installed.codex_command
        self.assertEqual(
            [
                command_fn("plugin", "marketplace", "upgrade", "tj-agents"),
                command_fn("plugin", "marketplace", "upgrade", "agents"),
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

        def run(command, **_kwargs):
            if command[:3] == ["claude", "plugin", "update"]:
                update_claude_plugin(self.claude_home, command)
            return completed(0, "[]")

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
        record_codex_plugin(self.codex_home, "agent", "a" * 40, "label1")
        inventory = CodexPlugins(
            self.codex_home,
            {
                "agent@tj-agents": "label1",
                "base@tj-agents": "label2",
                "cli@tj-agents": "label3",
            },
        )

        def run(command, **_kwargs):
            if command[:3] == ["codex", "plugin", "list"]:
                return completed(0, inventory.listing())
            if command[:3] == ["codex", "plugin", "add"]:
                inventory.install(command[3])
            return completed(0)

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

    def test_a_refresh_records_its_failures(self):
        self.install("agent", "a" * 40, "b" * 40)

        def run(command, **_kwargs):
            return (
                completed(1, "", "boom")
                if command[:3] == ["claude", "plugin", "update"]
                else completed(0, "[]")
            )

        with mock.patch.object(REFRESH.process, "run", side_effect=run):
            self.assertEqual(1, REFRESH.main(["update", "--host", "claude"]))
        self.assertEqual(
            ["claude: agent@tj-agents: boom"], recorded_failures(self.state)
        )
        self.assertFalse(REFRESH.jobs.running(self.state, REFRESH.jobs.Job.UPDATE))


def _patched_homes(home: Path) -> Any:
    return mock.patch.dict(os.environ, environment(home))


def test_codex_commands_carry_marketplace_overrides_and_no_cwd(tmp_path: Path) -> None:
    inventory = CodexPlugins(tmp_path / "codex", {"agent@tj-agents": "local"})

    def run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        if command[:3] == ["codex", "plugin", "list"]:
            return completed(0, inventory.listing())
        if command[:3] == ["codex", "plugin", "add"]:
            inventory.install(command[3])
        return completed(0)

    with (
        _patched_homes(tmp_path),
        mock.patch.object(REFRESH.process, "run", side_effect=run) as spawned,
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
    ):
        assert REFRESH.main(["update", "--host", "codex"]) == 1
    assert recorded_failures(tmp_path / "state") == [
        "codex: list: no tj-agents plugins installed"
    ]


OLD_PIN = "a" * 40
NEW_PIN = "b" * 40
AGENT_PIN = "c" * 40


def test_refresh_runs_only_plugin_commands_on_both_hosts(tmp_path: Path) -> None:
    claude = tmp_path / "claude"
    pin_plugin(claude, "personal", OLD_PIN)
    install_plugin(claude, "personal", NEW_PIN, converge=True)
    inventory = CodexPlugins(tmp_path / "codex", {"personal@tj-agents": "local"})

    def run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        if command[:3] == ["claude", "plugin", "update"]:
            update_claude_plugin(claude, command)
        if command[:3] == ["codex", "plugin", "list"]:
            return completed(0, inventory.listing())
        if command[:3] == ["codex", "plugin", "add"]:
            inventory.install(command[3])
        return completed(0, "[]")

    with (
        _patched_homes(tmp_path),
        mock.patch.object(REFRESH.process, "run", side_effect=run) as spawned,
    ):
        assert REFRESH.main(["update", "--host", "claude"]) == 0
        assert REFRESH.main(["update", "--host", "codex"]) == 0
    commands = [call.args[0] for call in spawned.call_args_list]
    assert {command[0] for command in commands if command[0] != "tj"} == {
        "claude",
        "codex",
    }
    assert all(
        command[:2] == ["tj", "update"] for command in commands if command[0] == "tj"
    )
    assert not [command for command in commands if "converge" in command]


if __name__ == "__main__":
    unittest.main()


class FakeDaemon:
    def __init__(self, failing: frozenset[str] = frozenset()) -> None:
        self.requests: list[tuple[str, dict[str, object]]] = []
        self.failing = failing
        self.inventory: CodexPlugins | None = None

    def __enter__(self) -> "FakeDaemon":
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def request(self, method: str, params: dict[str, object]) -> dict[str, object]:
        self.requests.append((method, params))
        if params.get("pluginName") in self.failing:
            raise RuntimeError(f"Codex {method} failed: boom")
        if method == "plugin/install" and self.inventory is not None:
            self.inventory.install(f"{params['pluginName']}@tj-agents")
        if method == "thread/loaded/list":
            return {"data": [], "nextCursor": None}
        return {}


def daemon_refresh(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    daemon: FakeDaemon,
    published: Callable[[], None] = lambda: None,
) -> tuple[int, list[list[str]], list[tuple[object, ...]]]:
    inventory = CodexPlugins(
        tmp_path / "codex", {"agent@tj-agents": "local", "base@tj-agents": "local"}
    )
    daemon.inventory = inventory
    roots = json.dumps(
        {"marketplaces": [{"name": "tj-agents", "root": str(tmp_path / "market")}]}
    )

    def run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        if command[:3] == ["codex", "plugin", "list"]:
            return completed(0, inventory.listing())
        if command[:4] == ["codex", "plugin", "marketplace", "list"]:
            return completed(0, roots)
        if command[:2] == ["tj", "update"]:
            published()
        return completed(0)

    trusted: list[tuple[object, ...]] = []
    monkeypatch.setattr(
        codex_launch, "liveness", lambda **_kwargs: {"thread": object()}
    )
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
    ):
        code = REFRESH.main(["update", "--host", "codex"])
    return code, [call.args[0] for call in spawned.call_args_list], trusted


def test_open_codex_sessions_install_through_the_daemon_and_publish_the_update(
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
        ("thread/loaded/list", {"cursor": None}),
    ]
    assert commands[-1] == [
        "tj",
        "update",
        "--host",
        "codex",
        "--revision",
        MARKETPLACE_REVISION,
    ]
    assert trusted == [
        ("codex", tmp_path / "state", REFRESH.codex_installed.MARKETPLACE_OVERRIDES)
    ]


def test_codex_reloads_after_the_updated_repository_standards_are_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    selection = tmp_path / ".agents/plugins.json"
    selection.parent.mkdir()
    selection.write_text(
        '{"schema_version": 1, "plugins": ["agent@tj-agents"]}', encoding="utf-8"
    )
    root = tmp_path / "updated-agent"
    standard = root / ".agents/agent/convention/lanes/SKILL.md"
    standard.parent.mkdir(parents=True)
    standard.write_text(
        "---\nname: lanes\nkind: convention\nprofile: core\n"
        "applicability: agent sessions\n---\nFull lanes standard\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        required_plugins, "check", lambda *args: {"agent@tj-agents": root}
    )
    published: list[str] = []

    def publish() -> None:
        published.append((tmp_path / "AGENTS.md").read_text(encoding="utf-8"))

    code, commands, _trusted = daemon_refresh(
        tmp_path, monkeypatch, FakeDaemon(), publish
    )
    assert code == 0
    assert len(published) == 1 and "agent:lanes" in published[0]
    assert commands[-1][:2] == ["tj", "update"]


@pytest.mark.parametrize(
    ("local", "failed"), [(False, False), (True, False), (False, True)]
)
def test_codex_reconciles_other_loaded_repositories_before_publishing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, local: bool, failed: bool
) -> None:
    other = tmp_path / "other-repository"
    selection = other / ".agents/plugins.json"
    selection.parent.mkdir(parents=True)
    selection.write_text(
        '{"schema_version": 1, "plugins": ["agent@tj-agents"]}', encoding="utf-8"
    )
    instructions = other / "AGENTS.md"
    instructions.write_text("Existing repository instructions\n", encoding="utf-8")
    if local:
        (selection.parent / "repo-capabilities.local.json").write_text(
            "{}", encoding="utf-8"
        )
    daemon = FakeDaemon()
    native_request = daemon.request
    update_standards = REFRESH.codex_installed.update_project_standards

    def update(project: Path) -> None:
        if failed:
            raise required_plugins.RequiredPluginsError(
                "Cannot verify updated standards"
            )
        update_standards(project)

    monkeypatch.setattr(REFRESH.codex_installed, "update_project_standards", update)

    def request(method: str, params: dict[str, object]) -> dict[str, object]:
        response = native_request(method, params)
        if method == "thread/loaded/list":
            return {"data": ["other-thread"], "nextCursor": None}
        if method == "thread/read":
            return {"thread": {"cwd": str(other)}}
        if method == "plugin/install" and params.get("pluginName") == "agent":
            standard = (
                tmp_path
                / "codex/plugins/cache/tj-agents/agent/local/.agents/agent/convention/lanes/SKILL.md"
            )
            standard.parent.mkdir(parents=True)
            standard.write_text(
                "---\nname: lanes\nkind: convention\nprofile: core\n"
                "applicability: agent sessions\n---\nFull lanes standard\n",
                encoding="utf-8",
            )
        return response

    def publish() -> None:
        content = instructions.read_text(encoding="utf-8")
        assert ("agent:lanes" in content) is not local
        assert "Existing repository instructions" in content

    monkeypatch.setattr(daemon, "request", request)
    code, commands, _trusted = daemon_refresh(tmp_path, monkeypatch, daemon, publish)
    assert code == int(failed)
    assert (commands[-1][:2] == ["tj", "update"]) is not failed
    if failed:
        failed = False
        report = REFRESH.Report()
        with _patched_homes(tmp_path):
            REFRESH.codex_installed.activate(report)
        assert not report.failures
        content = instructions.read_text(encoding="utf-8")
        assert ("agent:lanes" in content) is not local


def test_a_failed_daemon_install_is_reported_and_the_rest_still_install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    daemon = FakeDaemon(failing=frozenset({"agent"}))
    code, commands, _trusted = daemon_refresh(tmp_path, monkeypatch, daemon)
    assert code == 1
    assert [
        params.get("pluginName")
        for method, params in daemon.requests
        if method == "plugin/install"
    ] == [
        "agent",
        "base",
    ]
    assert not [command for command in commands if command[:2] == ["tj", "update"]]
    assert recorded_failures(tmp_path / "state")[0].startswith(
        "codex: agent@tj-agents: Codex plugin/install failed"
    )


def test_the_cli_path_trusts_new_hooks_once_after_adding_plugins(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    no_codex_daemon: list[tuple[object, ...]],
) -> None:
    inventory = CodexPlugins(tmp_path / "codex", {"agent@tj-agents": "local"})
    order: list[str] = []

    def run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        order.append(" ".join(command[:3]))
        if command[:3] == ["codex", "plugin", "list"]:
            return completed(0, inventory.listing())
        if command[:3] == ["codex", "plugin", "add"]:
            inventory.install(command[3])
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
    ):
        assert REFRESH.main(["update", "--host", "codex"]) == 0
    assert order.count("trust") == 1
    assert order.index("codex plugin add") < order.index("trust")
    assert no_codex_daemon == [
        ("codex", tmp_path / "state", REFRESH.codex_installed.MARKETPLACE_OVERRIDES)
    ]


def test_nothing_is_trusted_when_every_installed_build_is_already_trusted(
    tmp_path: Path, no_codex_daemon: list[tuple[object, ...]]
) -> None:
    inventory = CodexPlugins(tmp_path / "codex", {"agent@tj-agents": "local"})

    def run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        if command[:3] == ["codex", "plugin", "list"]:
            return completed(0, inventory.listing())
        if command[:3] == ["codex", "plugin", "add"]:
            inventory.install(command[3])
        return completed(0)

    with (
        _patched_homes(tmp_path),
        mock.patch.object(REFRESH.process, "run", side_effect=run),
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
    codex_home = tmp_path / "codex"
    inventory = CodexPlugins(
        codex_home,
        {"agent@tj-agents": "local", "base@tj-agents": "local"},
        hidden={"agent@tj-agents"},
    )
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
            return completed(0, inventory.listing())
        if command[:4] == ["codex", "plugin", "marketplace", "list"]:
            return completed(0, roots)
        added = command[:3] == ["codex", "plugin", "add"]
        if added:
            inventory.install(command[3])
        if added and command[3] not in config.read_text(encoding="utf-8"):
            with config.open("a", encoding="utf-8") as stream:
                stream.write(f'\n[plugins."{command[3]}"]\nenabled = true\n')
        return completed(0)

    with (
        _patched_homes(tmp_path),
        mock.patch.object(REFRESH.process, "run", side_effect=run) as spawned,
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


@pytest.mark.parametrize("managed", [True, False])
def test_an_update_publishes_the_marketplace_commit_for_every_running_wrapper(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    managed: bool,
) -> None:
    pin_plugin(tmp_path / "claude", "agent", AGENT_PIN)
    install_plugin(tmp_path / "claude", "agent", AGENT_PIN)
    if managed:
        monkeypatch.setenv("TJ_RUN_LAUNCH_ID", "fixture")
    else:
        monkeypatch.delenv("TJ_RUN_LAUNCH_ID", raising=False)
    with (
        _patched_homes(tmp_path),
        mock.patch.object(REFRESH.process, "run", return_value=completed()) as run,
    ):
        assert REFRESH.main(["update", "--host", "claude"]) == 0
    assert run.call_args.args[0] == [
        "tj",
        "update",
        "--host",
        "claude",
        "--revision",
        MARKETPLACE_REVISION,
    ]
    notice = "Plugins updated: type /reload-plugins."
    assert (notice in capsys.readouterr().out) is not managed


@pytest.mark.parametrize("host", list(Host))
def test_current_plugins_fetch_their_marketplace_and_publish_without_installing(
    host: Host, tmp_path: Path
) -> None:
    claude = tmp_path / "claude"
    pin_plugin(claude, "agent", AGENT_PIN)
    root = install_plugin(claude, "agent", AGENT_PIN)
    (claude / "plugins/known_marketplaces.json").write_text(
        json.dumps({"tj-agents": {"source": {"source": "github"}}}), encoding="utf-8"
    )
    (root / ".codex-plugin").mkdir(parents=True, exist_ok=True)
    (root / ".codex-plugin/plugin.json").write_text(
        json.dumps({"name": "agent", "version": "current"}), encoding="utf-8"
    )
    shutil.copytree(root, tmp_path / "codex/plugins/cache/tj-agents/agent/current")
    with (
        _patched_homes(tmp_path),
        mock.patch.object(REFRESH.process, "run", return_value=completed()) as run,
    ):
        record_codex_plugin(tmp_path / "codex", "agent", AGENT_PIN, "current")
        assert REFRESH.main(["update", "--host", host.value]) == 0
    commands = [call.args[0] for call in run.call_args_list]
    fetch = {
        Host.CLAUDE: ["claude", "plugin", "marketplace", "update", "tj-agents"],
        Host.CODEX: REFRESH.codex_installed.codex_command(
            "plugin", "marketplace", "upgrade", "tj-agents"
        ),
    }[host]
    assert commands == [
        fetch,
        ["tj", "update", "--host", host.value, "--revision", MARKETPLACE_REVISION],
    ]


@pytest.mark.parametrize("failed", [False, True])
def test_current_codex_plugins_repair_pending_hook_trust(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failed: bool
) -> None:
    monkeypatch.setattr(codex_hook_trust, "pending", lambda _state: ["trust"])
    monkeypatch.setattr(codex_launch, "liveness", lambda **_kwargs: {"session": {}})
    with (
        _patched_homes(tmp_path),
        mock.patch.object(REFRESH, "already_current", return_value=True),
        mock.patch.object(
            codex_hook_trust,
            "trust_installed",
            side_effect=RuntimeError("source differs") if failed else None,
            return_value={},
        ) as trust,
        mock.patch.object(
            REFRESH.codex_installed, "activate_open_sessions", return_value=[]
        ) as activate,
        mock.patch.object(REFRESH.process, "run", return_value=completed()) as spawned,
    ):
        assert REFRESH.main(["update", "--host", "codex"]) == int(failed)
    trust.assert_called_once()
    assert activate.call_count == int(not failed)
    assert [call.args[0][:2] for call in spawned.call_args_list] == [
        ["codex", "plugin"],
        *([] if failed else [["tj", "update"]]),
    ]


def test_codex_hook_receipts_detect_content_changes_with_unchanged_commands(
    tmp_path: Path,
) -> None:
    build = tmp_path / "codex/plugins/cache/tj-agents/agent/local"
    (build / ".codex-plugin").mkdir(parents=True)
    (build / ".codex-plugin/plugin.json").write_text(
        json.dumps({"name": "agent", "hooks": "hooks.json"}), encoding="utf-8"
    )
    (build / "hooks.json").write_text(
        json.dumps(
            {
                "hooks": {
                    "SessionStart": [
                        {"hooks": [{"type": "command", "command": "python startup.py"}]}
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    script = build / "startup.py"
    script.write_text("print('before')", encoding="utf-8")
    state = tmp_path / "state"
    state.mkdir()
    with _patched_homes(tmp_path):
        (state / codex_hook_trust.RECEIPT).write_text(
            json.dumps(codex_hook_trust.published_hooks()), encoding="utf-8"
        )
        assert codex_hook_trust.pending(state) == []
        script.write_text("print('changed content')", encoding="utf-8")
        assert codex_hook_trust.pending(state) == ["trust agent@tj-agents hooks"]


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


@pytest.mark.parametrize("host", list(Host))
def test_the_published_revision_is_the_marketplace_clone_commit(
    host: Host, tmp_path: Path
) -> None:
    clone = {
        Host.CLAUDE: tmp_path / "claude/plugins/marketplaces/tj-agents",
        Host.CODEX: tmp_path / "codex/.tmp/marketplaces/tj-agents",
    }[host]
    git = ["git", "-c", "user.name=t", "-c", "user.email=t@t"]
    git += ["-c", "commit.gpgsign=false"]
    variables = without_git_config(dict(os.environ))
    with _patched_homes(tmp_path):
        missing = REFRESH.Report()
        assert READ_MARKETPLACE_REVISION(missing, host) is None
        assert missing.failures
        clone.mkdir(parents=True)
        for command in (
            ["init", "-q"],
            ["commit", "-q", "--allow-empty", "-m", "pins"],
        ):
            subprocess.run([*git, *command], cwd=clone, env=variables, check=True)
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=clone,
            env=variables,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        report = REFRESH.Report()
        assert READ_MARKETPLACE_REVISION(report, host) == head
    assert report.failures == []
