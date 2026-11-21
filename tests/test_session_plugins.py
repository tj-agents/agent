"""Startup evidence stays honest with native inventory and package fixtures."""

import json
import shutil
import subprocess
import time
from pathlib import Path
from unittest import mock

import pytest
import session_plugins
from tj_agent.host import Host
from tj_cli import process


def selection(project: Path, *identities: str) -> None:
    path = project / ".agents/plugins.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"schema_version": 1, "plugins": identities}), encoding="utf-8"
    )


def plugin(
    configuration: Path,
    host: Host,
    identity: str = session_plugins.AGENT,
    version: str = "current",
    dependencies: tuple[str, ...] = (),
) -> tuple[Path, dict[str, object]]:
    name, market = identity.split("@")
    root = configuration / "plugins/cache" / market / name / version
    for manifest_host in Host:
        manifest = root / manifest_host.manifest_directory / "plugin.json"
        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_text(
            json.dumps(
                {"name": name, "version": version, "dependencies": dependencies}
            ),
            encoding="utf-8",
        )
    (root / ".agents/agent/runtime").mkdir(parents=True)
    row: dict[str, object] = {"enabled": True, "installed": True, "scope": "user"}
    locations = {
        Host.CLAUDE: {"id": identity, "installPath": str(root)},
        Host.CODEX: {"name": name, "marketplaceName": market, "version": version},
    }
    row.update(locations[host])
    return root, row


def native_inventory(
    monkeypatch: pytest.MonkeyPatch,
    host: Host,
    configuration: Path,
    rows: list[dict[str, object]],
) -> mock.Mock:
    monkeypatch.setenv(host.home_variable, str(configuration))
    monkeypatch.setattr(shutil, "which", lambda _name: "native-host")
    document: object = rows if host is Host.CLAUDE else {"installed": rows}
    run = mock.Mock(
        return_value=subprocess.CompletedProcess(
            ["native-host"], 0, json.dumps(document), ""
        )
    )
    monkeypatch.setattr(process, "run", run)
    return run


def executing(root: Path) -> Path:
    return root / ".agents/agent/runtime/session_hook.py"


@pytest.mark.parametrize("host", list(Host))
def test_matching_agent_only_attests_to_its_hook_package(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, host: Host
) -> None:
    root, agent = plugin(tmp_path, host, dependencies=("base",))
    _, base = plugin(tmp_path, host, "base@tj-agents")
    selection(tmp_path, session_plugins.AGENT)
    run = native_inventory(monkeypatch, host, tmp_path, [agent, base])

    message = session_plugins.check(host, tmp_path, executing_file=executing(root))

    assert "selected plugin closure is available" in message
    assert "executing Agent hook matches the installed root" in message
    assert "not other cached Agent content" in message
    assert "Session-wide loaded plugin versions remain unverified" in message
    assert "Reload plugins (or restart this session)" in message
    assert run.call_count == 1
    assert 0 < run.call_args.kwargs["timeout"] <= session_plugins.INVENTORY_SECONDS
    assert run.call_args.args[0][-1] == "--json"
    assert run.call_args.args[0][1:3] == ["plugin", "list"]


@pytest.mark.parametrize("host", list(Host))
def test_an_old_executing_agent_build_warns_even_when_admission_passes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, host: Host
) -> None:
    root, agent = plugin(tmp_path, host)
    old, _ = plugin(tmp_path, host, version="old")
    selection(tmp_path, session_plugins.AGENT)
    native_inventory(monkeypatch, host, tmp_path, [agent])

    message = session_plugins.check(host, tmp_path, executing_file=executing(old))

    assert "STALE Agent hook" in message
    assert f"version old at {old}" in message
    assert f"version current at {root}" in message
    assert "selected plugin closure is available" in message
    assert "Reload plugins or restart this session" in message


def test_matching_version_at_a_different_package_root_does_not_prove_freshness(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, agent = plugin(tmp_path, Host.CLAUDE)
    other, _ = plugin(tmp_path / "old-cache", Host.CLAUDE)
    native_inventory(monkeypatch, Host.CLAUDE, tmp_path, [agent])

    message = session_plugins.check(
        Host.CLAUDE, tmp_path, executing_file=executing(other)
    )

    assert "STALE Agent hook or different build" in message
    assert "no declared plugin selection" in message


@pytest.mark.parametrize("host", list(Host))
def test_missing_transitive_plugin_is_not_silently_admitted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, host: Host
) -> None:
    root, agent = plugin(tmp_path, host, dependencies=("base",))
    selection(tmp_path, session_plugins.AGENT)
    native_inventory(monkeypatch, host, tmp_path, [agent])

    message = session_plugins.check(host, tmp_path, executing_file=executing(root))

    assert "Selected plugin admission failed" in message
    assert "base@tj-agents" in message
    assert "executing Agent hook matches" in message
    assert "selected plugin closure is available" not in message


@pytest.mark.parametrize("host", list(Host))
def test_native_loader_errors_remain_visible(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, host: Host
) -> None:
    root, agent = plugin(tmp_path, host)
    agent["errors"] = ["Agent refused: dependency base is disabled"]
    selection(tmp_path, session_plugins.AGENT)
    native_inventory(monkeypatch, host, tmp_path, [agent])

    message = session_plugins.check(host, tmp_path, executing_file=executing(root))

    assert "Selected plugin admission failed" in message
    assert "Agent refused: dependency base is disabled" in message
    assert "no valid enabled installed build" in message
    assert "executing Agent hook matches" not in message


@pytest.mark.parametrize("host", list(Host))
@pytest.mark.parametrize("problem", ["disabled", "incomplete"])
def test_invalid_installed_agent_never_becomes_loaded_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, host: Host, problem: str
) -> None:
    root, agent = plugin(tmp_path, host)
    if problem == "disabled":
        agent["enabled"] = False
    else:
        (root / host.manifest_directory / "plugin.json").unlink()
    native_inventory(monkeypatch, host, tmp_path, [agent])

    message = session_plugins.check(host, tmp_path, executing_file=executing(root))

    assert "no valid enabled installed build" in message
    assert "executing Agent hook matches" not in message


@pytest.mark.parametrize("host", list(Host))
def test_unreadable_executing_manifest_is_unverified(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, host: Host
) -> None:
    _, agent = plugin(tmp_path, host)
    old, _ = plugin(tmp_path, host, version="old")
    (old / host.manifest_directory / "plugin.json").write_text("{", encoding="utf-8")
    native_inventory(monkeypatch, host, tmp_path, [agent])

    message = session_plugins.check(host, tmp_path, executing_file=executing(old))

    assert "Executing Agent hook freshness is unverified" in message
    assert "executing Agent hook matches" not in message


@pytest.mark.parametrize("host", list(Host))
def test_invalid_selection_is_reported_while_the_executing_agent_is_still_checked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, host: Host
) -> None:
    root, agent = plugin(tmp_path, host)
    selection(tmp_path, "not-an-identity")
    native_inventory(monkeypatch, host, tmp_path, [agent])

    message = session_plugins.check(host, tmp_path, executing_file=executing(root))

    assert "Selected plugin admission is unverified" in message
    assert "executing Agent hook matches" in message
    assert "selected plugin closure is available" not in message


@pytest.mark.parametrize("host", list(Host))
@pytest.mark.parametrize("failure", ["timeout", "bad-json", "native-failure"])
def test_native_inventory_failure_is_a_visible_unverified_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, host: Host, failure: str
) -> None:
    root, agent = plugin(tmp_path, host)
    run = native_inventory(monkeypatch, host, tmp_path, [agent])
    if failure == "timeout":
        run.side_effect = subprocess.TimeoutExpired("native-host", 3)
    else:
        run.return_value = subprocess.CompletedProcess(
            ["native-host"], 1 if failure == "native-failure" else 0, "invalid", ""
        )

    message = session_plugins.check(host, tmp_path, executing_file=executing(root))

    assert "Installed plugin admission and freshness are unverified" in message
    assert "Reload plugins" in message
    assert "selected plugin closure is available" not in message


def test_codex_follows_cross_marketplace_dependencies_with_a_shared_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, agent = plugin(tmp_path, Host.CODEX, dependencies=("company@employer",))
    _, company = plugin(tmp_path, Host.CODEX, "company@employer")
    selection(tmp_path, session_plugins.AGENT)
    run = native_inventory(monkeypatch, Host.CODEX, tmp_path, [agent])
    run.side_effect = [
        subprocess.CompletedProcess([], 0, json.dumps({"installed": rows}), "")
        for rows in ([agent], [company])
    ]
    clock = iter([0.0, 0.5, 1.5])
    monkeypatch.setattr(time, "monotonic", lambda: next(clock))

    message = session_plugins.check(
        Host.CODEX, tmp_path, executing_file=executing(root)
    )

    assert "selected plugin closure is available" in message
    assert [call.args[0][-2] for call in run.call_args_list] == [
        "tj-agents",
        "employer",
    ]
    assert [call.kwargs["timeout"] for call in run.call_args_list] == [2.5, 1.5]


def test_an_expired_budget_does_not_start_another_inventory_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, agent = plugin(tmp_path, Host.CODEX, dependencies=("company@employer",))
    run = native_inventory(monkeypatch, Host.CODEX, tmp_path, [agent])
    clock = iter([0.0, 0.0, 3.0])
    monkeypatch.setattr(time, "monotonic", lambda: next(clock))

    message = session_plugins.check(
        Host.CODEX, tmp_path, executing_file=executing(root)
    )

    assert "exceeded its 3-second budget" in message
    assert run.call_count == 1


def test_long_native_errors_cannot_hide_the_evidence_limit() -> None:
    message = session_plugins.warning("x" * (session_plugins.DETAIL_LIMIT + 100))
    assert "x" * (session_plugins.DETAIL_LIMIT + 1) not in message
    assert message.endswith(session_plugins.EVIDENCE_LIMIT)
