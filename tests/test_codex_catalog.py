from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest import mock

import jobs
import plugin_update
import pytest
from codex import catalog, launch, plugins
from tj_agent.host import Host
from tj_cli import process
from tree_digest import Digests

HOSTS = {host.value: host for host in Host}


def update_plugins(host: str) -> int:
    outcome = plugin_update.update(HOSTS[host], Digests(jobs.state_directory()))
    return 1 if outcome.failures else 0


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(tmp_path)
    directory = tmp_path / "codex"
    monkeypatch.setenv("CODEX_HOME", str(directory))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "absent-claude"))
    monkeypatch.setenv("AGENT_STATE_DIRECTORY", str(tmp_path / "state"))
    monkeypatch.delenv("PLUGIN_ROOT", raising=False)
    monkeypatch.setattr(launch, "liveness", lambda **_kwargs: {})
    return directory


def publish(home: Path, pin: str, version: str) -> dict[str, object]:
    source = {
        "source": "git-subdir",
        "url": "https://github.com/tj-agents/agent.git",
        "path": "./plugins/agent",
        "sha": pin,
    }
    path = home / ".tmp/marketplaces/tj-agents/.agents/plugins/marketplace.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"plugins": [{"name": "agent", "source": source}]}), encoding="utf-8"
    )
    return {
        "pluginId": "agent@tj-agents",
        "marketplaceName": "tj-agents",
        "version": version,
        "source": {**source, "path": "plugins/agent"},
        "installed": True,
    }


def native_installs(calls: list[mock._Call]) -> list[list[str]]:
    return [
        call.args[0]
        for call in calls
        if call.args[0][:3] in (["codex", "plugin", "list"], ["codex", "plugin", "add"])
    ]


def converged(home: Path) -> bool:
    return plugin_update.already_current(Host.CODEX, Digests(home.parent / "state"))


def installed(home: Path, version: str, *, with_version: bool = True) -> Path:
    root = home / "plugins/cache/tj-agents/agent" / version
    (root / ".codex-plugin").mkdir(parents=True)
    manifest = {"name": "agent"}
    if with_version:
        manifest["version"] = version
    (root / ".codex-plugin/plugin.json").write_text(
        json.dumps(manifest), encoding="utf-8"
    )
    return root


def test_native_inventory_seeds_a_codex_only_fast_path(home: Path) -> None:
    entry = publish(home, "a" * 40, "a" * 12)
    installed(home, "a" * 12)
    assert plugins.installations(home, [entry]) == [entry]
    with mock.patch.object(
        process, "run", return_value=subprocess.CompletedProcess([], 0, "", "")
    ) as run:
        outcome = plugin_update.update(Host.CODEX, Digests(jobs.state_directory()))
    assert not outcome.failures
    assert not outcome.changed
    assert native_installs(run.call_args_list) == []
    assert converged(home)


@pytest.mark.parametrize("corrupt", [False, True])
def test_missing_or_corrupt_receipts_bootstrap_once_without_reinstalling(
    home: Path, corrupt: bool
) -> None:
    entry = publish(home, "a" * 40, "a" * 12)
    installed(home, "a" * 12)
    if corrupt:
        state = home.parent / "state"
        state.mkdir()
        (state / catalog.RECEIPT).write_text("invalid", encoding="utf-8")
    listing = json.dumps({"installed": [entry]})
    with mock.patch.object(
        process,
        "run",
        return_value=subprocess.CompletedProcess([], 0, listing, ""),
    ) as run:
        assert update_plugins("codex") == 0
        assert not any(
            call.args[0][:3] == ["codex", "plugin", "add"]
            for call in run.call_args_list
        )
        run.reset_mock()
        assert update_plugins("codex") == 0
        assert native_installs(run.call_args_list) == []


def test_a_codex_only_update_converges_to_the_new_catalog_version(home: Path) -> None:
    before = publish(home, "a" * 40, "a" * 12)
    previous = installed(home, "a" * 12)
    catalog.remember([before])
    after = publish(home, "b" * 40, "b" * 12)

    def run(command: list[str], **_options: object) -> subprocess.CompletedProcess[str]:
        if command[:3] == ["codex", "plugin", "list"]:
            return subprocess.CompletedProcess(
                command, 0, json.dumps({"installed": [after]}), ""
            )
        if command[:3] == ["codex", "plugin", "add"]:
            previous.rename(home / "previous-build")
            installed(home, "b" * 12)
        return subprocess.CompletedProcess(command, 0, "", "")

    with mock.patch.object(process, "run", side_effect=run) as native:
        assert update_plugins("codex") == 0
        assert (
            sum(
                call.args[0][:3] == ["codex", "plugin", "add"]
                for call in native.call_args_list
            )
            == 1
        )
        native.reset_mock()
        assert update_plugins("codex") == 0
        assert native_installs(native.call_args_list) == []
    assert converged(home)


def test_a_new_mutable_install_with_native_local_version_converges(home: Path) -> None:
    entry = publish(home, "a" * 40, "local")
    present = False

    def run(command: list[str], **_options: object) -> subprocess.CompletedProcess[str]:
        nonlocal present
        if command[:3] == ["codex", "plugin", "list"]:
            listing = {"installed": [{**entry, "installed": present}]}
            return subprocess.CompletedProcess(command, 0, json.dumps(listing), "")
        if command[:3] == ["codex", "plugin", "add"]:
            installed(home, "local", with_version=False)
            present = True
        return subprocess.CompletedProcess(command, 0, "", "")

    with mock.patch.object(process, "run", side_effect=run) as native:
        assert update_plugins("codex") == 0
        assert present
        native.reset_mock()
        assert update_plugins("codex") == 0
        assert native_installs(native.call_args_list) == []


def test_a_marketplace_local_plugin_listed_by_absolute_path_converges(
    home: Path,
) -> None:
    clone = home / ".tmp/marketplaces/agents"
    manifest = clone / ".agents/plugins/marketplace.json"
    manifest.parent.mkdir(parents=True)
    (clone / "plugins/concertable").mkdir(parents=True)
    manifest.write_text(
        json.dumps(
            {
                "plugins": [
                    {
                        "name": "concertable",
                        "source": {"source": "local", "path": "./plugins/concertable"},
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    root = home / "plugins/cache/agents/concertable/local"
    (root / ".codex-plugin").mkdir(parents=True)
    (root / ".codex-plugin/plugin.json").write_text(
        json.dumps({"name": "concertable"}), encoding="utf-8"
    )
    entry = {
        "pluginId": "concertable@agents",
        "version": "local",
        "installed": True,
        "source": {"source": "local", "path": str(clone / "plugins" / "concertable")},
    }
    catalog.remember([entry], verified_install=True)
    assert catalog.current("agents", "concertable", Digests(home.parent / "state"))


def test_a_local_build_rejects_a_malformed_manifest_version(home: Path) -> None:
    entry = publish(home, "a" * 40, "local")
    root = installed(home, "local")
    (root / ".codex-plugin/plugin.json").write_text(
        json.dumps({"name": "agent", "version": None}), encoding="utf-8"
    )
    catalog.remember([entry], verified_install=True)
    assert not catalog.current("tj-agents", "agent", Digests(home.parent / "state"))


def test_inventory_from_an_old_catalog_cannot_record_a_new_source_pin(
    home: Path,
) -> None:
    old = publish(home, "a" * 40, "a" * 12)
    installed(home, "a" * 12)
    publish(home, "b" * 40, "b" * 12)
    catalog.remember([old])
    assert not catalog.current("tj-agents", "agent", Digests(home.parent / "state"))


@pytest.mark.parametrize(
    "scenario", ["uninstalled", "error", "omitted", "missing-cache"]
)
def test_a_native_add_cannot_report_success_without_converging(
    home: Path, scenario: str
) -> None:
    entry = publish(home, "a" * 40, "a" * 12)
    changed = False

    def run(command: list[str], **_options: object) -> subprocess.CompletedProcess[str]:
        nonlocal changed
        if command[:3] == ["codex", "plugin", "list"]:
            rows = [entry]
            if changed:
                if scenario == "uninstalled":
                    rows = [{**entry, "installed": False}]
                elif scenario == "error":
                    rows = [{**entry, "error": "unusable"}]
                elif scenario == "omitted":
                    rows = []
            return subprocess.CompletedProcess(
                command, 0, json.dumps({"installed": rows}), ""
            )
        if command[:3] == ["codex", "plugin", "add"]:
            changed = True
            if scenario != "missing-cache":
                installed(home, "a" * 12)
        return subprocess.CompletedProcess(command, 0, "", "")

    with mock.patch.object(process, "run", side_effect=run):
        result = plugin_update.update(Host.CODEX, Digests(home.parent / "state"))
    assert any("did not converge" in failure for failure in result.failures)


@pytest.mark.parametrize(
    "invalid", [{"installed": False}, {"error": "unusable"}, {"errors": ["unusable"]}]
)
def test_unusable_inventory_invalidates_a_previously_current_build(
    home: Path, invalid: dict[str, object]
) -> None:
    entry = publish(home, "a" * 40, "a" * 12)
    installed(home, "a" * 12)
    catalog.remember([entry])
    catalog.remember([{**entry, **invalid}])
    assert not catalog.current("tj-agents", "agent", Digests(home.parent / "state"))


def test_inventory_cannot_accept_mutated_local_contents(home: Path) -> None:
    entry = publish(home, "a" * 40, "local")
    root = installed(home, "local")
    script = root / "startup.py"
    script.write_text("original", encoding="utf-8")
    catalog.remember([entry], verified_install=True)
    state = home.parent / "state"
    assert catalog.current("tj-agents", "agent", Digests(state))
    script.write_text("mutated contents", encoding="utf-8")
    catalog.remember([entry])
    assert not catalog.current("tj-agents", "agent", Digests(state))


def test_immutable_builds_check_metadata_and_reject_a_changed_manifest(
    home: Path,
) -> None:
    entry = publish(home, "a" * 40, "a" * 12)
    root = installed(home, "a" * 12)
    catalog.remember([entry])
    digests = Digests(home.parent / "state")
    with mock.patch.object(digests, "tree") as tree:
        assert catalog.current("tj-agents", "agent", digests)
        tree.assert_not_called()
    (root / ".codex-plugin/plugin.json").write_text(
        '{"version":"local"}', encoding="utf-8"
    )
    assert not catalog.current("tj-agents", "agent", digests)


def test_an_unconverged_install_names_its_cause(home: Path) -> None:
    entry = publish(home, "a" * 40, "a" * 12)
    assert catalog.unconverged("tj-agents", "agent", entry) == (
        f"no cached build labelled {'a' * 12} at "
        f"{home / 'plugins/cache/tj-agents/agent' / ('a' * 12)}"
    )
    installed(home, "a" * 12)
    installed(home, "b" * 12)
    assert catalog.unconverged("tj-agents", "agent", entry) == (
        f"its cache holds builds {'a' * 12}, {'b' * 12} besides {'a' * 12}"
    )
    moved = {**entry, "source": {"source": "git-subdir", "path": "elsewhere"}}
    assert catalog.unconverged("tj-agents", "agent", moved).startswith(
        'Codex lists source {"path": "elsewhere"'
    )
