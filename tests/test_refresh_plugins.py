import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

import install
import jobs
import plugin_update
import pytest
import required_plugins
from codex import hook_trust as codex_hook_trust
from codex import installed as codex_installed
from codex import launch as codex_launch
from fake_machine import (
    CodexPlugins,
    claude_payload,
    environment,
    install_plugin,
    pin_plugin,
    record_codex_plugin,
    update_claude_plugin,
)
from git_isolation import without_git_config
from tj_agent.host import Host
from tj_cli import process
from tree_digest import Digests

READ_MARKETPLACE_REVISION = plugin_update.marketplace_revision


HOSTS = {host.value: host for host in Host}


def update_plugins(host: str) -> int:
    outcome = plugin_update.update(HOSTS[host], Digests(jobs.state_directory()))
    return 1 if outcome.failures else 0


@pytest.fixture(autouse=True)
def no_codex_daemon(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> list[tuple[object, ...]]:
    monkeypatch.chdir(tmp_path)
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
            return completed(0)

        with mock.patch.object(process, "run", side_effect=run) as spawned:
            outcome = plugin_update.update(Host.CLAUDE, Digests(self.state))
        self.assertEqual(["claude: agent@tj-agents: update failed"], outcome.failures)
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

        with mock.patch.object(process, "run", side_effect=run) as spawned:
            outcome = plugin_update.update(Host.CLAUDE, Digests(self.state))
        self.assertEqual([], outcome.failures)
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

        with mock.patch.object(process, "run", side_effect=run) as spawned:
            outcome = plugin_update.update(Host.CODEX, Digests(self.state))
        self.assertEqual([], outcome.failures)
        commands = [call.args[0] for call in spawned.call_args_list]
        command_fn = codex_installed.codex_command
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

        with mock.patch.object(process, "run", side_effect=run) as spawned:
            outcome = plugin_update.update(Host.CLAUDE, Digests(self.state))
        self.assertEqual([], outcome.failures)
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

        with mock.patch.object(process, "run", side_effect=run) as spawned:
            outcome = plugin_update.update(Host.CODEX, Digests(self.state))
        self.assertEqual([], outcome.failures)
        adds = [
            call.args[0]
            for call in spawned.call_args_list
            if call.args[0][:3] == ["codex", "plugin", "add"]
        ]
        command_fn = codex_installed.codex_command
        self.assertEqual(
            [
                command_fn("plugin", "add", "base@tj-agents"),
                command_fn("plugin", "add", "cli@tj-agents"),
            ],
            adds,
        )


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
        mock.patch.object(process, "run", side_effect=run) as spawned,
    ):
        assert update_plugins("codex") == 0
    overrides = codex_installed.MARKETPLACE_OVERRIDES
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
        mock.patch.object(process, "run", side_effect=run),
    ):
        outcome = plugin_update.update(Host.CODEX, Digests(tmp_path / "state"))
    assert outcome.failures == ["codex: list: no tj-agents plugins installed"]


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
        mock.patch.object(process, "run", side_effect=run) as spawned,
    ):
        state = Digests(tmp_path / "state")
        assert not plugin_update.update(Host.CLAUDE, state).failures
        assert not plugin_update.update(Host.CODEX, state).failures
    commands = [call.args[0] for call in spawned.call_args_list]
    assert {command[0] for command in commands} == {"claude", "codex"}
    assert not [command for command in commands if "converge" in command]


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
) -> tuple[plugin_update.Outcome, list[list[str]], list[tuple[object, ...]]]:
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
        mock.patch.object(process, "run", side_effect=run) as spawned,
    ):
        outcome = plugin_update.update(Host.CODEX, Digests(tmp_path / "state"))
    return outcome, [call.args[0] for call in spawned.call_args_list], trusted


def test_open_codex_sessions_install_through_the_daemon(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    daemon = FakeDaemon()
    outcome, commands, trusted = daemon_refresh(tmp_path, monkeypatch, daemon)
    assert not outcome.failures
    assert not [
        command for command in commands if command[:3] == ["codex", "plugin", "add"]
    ]
    manifest = str(tmp_path / "market" / ".agents" / "plugins" / "marketplace.json")
    assert daemon.requests == [
        ("plugin/install", {"marketplacePath": manifest, "pluginName": "agent"}),
        ("plugin/install", {"marketplacePath": manifest, "pluginName": "base"}),
        ("thread/loaded/list", {"cursor": None}),
    ]
    assert trusted == [
        ("codex", tmp_path / "state", codex_installed.MARKETPLACE_OVERRIDES)
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
    outcome, _commands, _trusted = daemon_refresh(tmp_path, monkeypatch, FakeDaemon())
    assert not outcome.failures
    content = (tmp_path / "AGENTS.md").read_text(encoding="utf-8")
    assert "agent:lanes" in content


@pytest.mark.parametrize(
    ("local", "failed"), [(False, False), (True, False), (False, True)]
)
def test_codex_reconciles_other_loaded_repositories_while_activating(
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
    update_standards = codex_installed.update_project_standards

    def update(project: Path) -> None:
        if failed:
            raise required_plugins.RequiredPluginsError(
                "Cannot verify updated standards"
            )
        update_standards(project)

    monkeypatch.setattr(codex_installed, "update_project_standards", update)

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

    monkeypatch.setattr(daemon, "request", request)
    outcome, _commands, _trusted = daemon_refresh(tmp_path, monkeypatch, daemon)
    assert bool(outcome.failures) == failed
    if not failed:
        content = instructions.read_text(encoding="utf-8")
        assert ("agent:lanes" in content) is not local
        assert "Existing repository instructions" in content
    else:
        failed = False
        report = plugin_update.Report()
        with _patched_homes(tmp_path):
            codex_installed.activate(report)
        assert not report.failures
        content = instructions.read_text(encoding="utf-8")
        assert ("agent:lanes" in content) is not local


def test_a_failed_daemon_install_is_reported_and_the_rest_still_install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    daemon = FakeDaemon(failing=frozenset({"agent"}))
    outcome, _commands, _trusted = daemon_refresh(tmp_path, monkeypatch, daemon)
    assert [
        params.get("pluginName")
        for method, params in daemon.requests
        if method == "plugin/install"
    ] == [
        "agent",
        "base",
    ]
    assert outcome.failures[0].startswith(
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
        mock.patch.object(process, "run", side_effect=run),
    ):
        assert update_plugins("codex") == 0
    assert order.count("trust") == 1
    assert order.index("codex plugin add") < order.index("trust")
    assert no_codex_daemon == [
        ("codex", tmp_path / "state", codex_installed.MARKETPLACE_OVERRIDES)
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
        mock.patch.object(process, "run", side_effect=run),
    ):
        assert update_plugins("codex") == 0
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
            codex_installed.MARKETPLACE_OVERRIDES,
        )
    ]


def test_updated_tj_agents_plugins_load_only_where_a_repository_selects_them(
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
    config.write_text(
        '[plugins."base@tj-agents"]\nenabled = true\n\n'
        '[plugins."concertable@agents"]\nenabled = true\n',
        encoding="utf-8",
    )

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
        mock.patch.object(process, "run", side_effect=run) as spawned,
    ):
        assert update_plugins("codex") == 0
    adds = [
        call.args[0][3]
        for call in spawned.call_args_list
        if call.args[0][:3] == ["codex", "plugin", "add"]
    ]
    assert adds == ["agent@tj-agents", "base@tj-agents"]
    text = config.read_text(encoding="utf-8")
    assert 'plugins."agent@tj-agents"' not in text
    assert 'plugins."base@tj-agents"' not in text
    assert 'plugins."concertable@agents"' in text


@pytest.mark.parametrize("failed", [False, True])
def test_current_codex_plugins_repair_pending_hook_trust(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failed: bool
) -> None:
    monkeypatch.setattr(codex_hook_trust, "pending", lambda _state: ["trust"])
    monkeypatch.setattr(codex_launch, "liveness", lambda **_kwargs: {"session": {}})
    with (
        _patched_homes(tmp_path),
        mock.patch.object(plugin_update, "already_current", return_value=True),
        mock.patch.object(
            codex_hook_trust,
            "trust_installed",
            side_effect=RuntimeError("source differs") if failed else None,
            return_value={},
        ) as trust,
        mock.patch.object(
            codex_installed, "activate_open_sessions", return_value=[]
        ) as activate,
        mock.patch.object(process, "run", return_value=completed()) as spawned,
    ):
        outcome = plugin_update.update(Host.CODEX, Digests(tmp_path / "state"))
    assert bool(outcome.failures) == failed
    trust.assert_called_once()
    assert activate.call_count == int(not failed)
    assert [call.args[0][:2] for call in spawned.call_args_list] == [
        ["codex", "plugin"],
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
        assert not plugin_update.already_current(
            Host.CLAUDE, Digests(tmp_path / "state")
        )


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
        assert READ_MARKETPLACE_REVISION(host) is None
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
        assert READ_MARKETPLACE_REVISION(host) == head
