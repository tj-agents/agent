from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest import mock

import jobs
import latest_plugins
import pytest
import tj_plugins
from codex import catalog, launch, plugins
from tj_agent.host import Host
from tj_cli import process
from tree_digest import Digests


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    directory = tmp_path / "codex"
    monkeypatch.setenv("CODEX_HOME", str(directory))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "absent-claude"))
    monkeypatch.setenv("AGENT_STATE_DIRECTORY", str(tmp_path / "state"))
    monkeypatch.delenv("PLUGIN_ROOT", raising=False)
    monkeypatch.setattr(launch, "liveness", dict)
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


def installed(home: Path, version: str) -> Path:
    root = home / "plugins/cache/tj-agents/agent" / version
    (root / ".codex-plugin").mkdir(parents=True)
    (root / ".codex-plugin/plugin.json").write_text(
        json.dumps({"name": "agent", "version": version}), encoding="utf-8"
    )
    return root


def test_native_inventory_seeds_a_codex_only_fast_path(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    entry = publish(home, "a" * 40, "a" * 12)
    root = installed(home, "a" * 12)
    assert plugins.installations(home, [entry]) == [entry]
    with mock.patch.object(process, "run") as run:
        assert tj_plugins.main(["update", "--host", "codex"]) == 0
    run.assert_not_called()
    assert latest_plugins.installed_behind(Host.CODEX) == []
    monkeypatch.setenv("PLUGIN_ROOT", str(root))
    assert latest_plugins.running_behind(Host.CODEX) is None
    assert not (home.parent / "absent-claude").exists()


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
        assert tj_plugins.main(["update", "--host", "codex"]) == 0
        assert not any(
            call.args[0][:3] == ["codex", "plugin", "add"]
            for call in run.call_args_list
        )
        run.reset_mock()
        assert tj_plugins.main(["update", "--host", "codex"]) == 0
        run.assert_not_called()


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
        assert tj_plugins.main(["update", "--host", "codex"]) == 0
        assert (
            sum(
                call.args[0][:3] == ["codex", "plugin", "add"]
                for call in native.call_args_list
            )
            == 1
        )
        native.reset_mock()
        assert tj_plugins.main(["update", "--host", "codex"]) == 0
        native.assert_not_called()
    assert latest_plugins.installed_behind(Host.CODEX) == []


def test_a_new_mutable_install_records_its_post_install_inventory(home: Path) -> None:
    entry = publish(home, "a" * 40, "local")
    present = False

    def run(command: list[str], **_options: object) -> subprocess.CompletedProcess[str]:
        nonlocal present
        if command[:3] == ["codex", "plugin", "list"]:
            listing = {"installed": [{**entry, "installed": present}]}
            return subprocess.CompletedProcess(command, 0, json.dumps(listing), "")
        if command[:3] == ["codex", "plugin", "add"]:
            installed(home, "local")
            present = True
        return subprocess.CompletedProcess(command, 0, "", "")

    with mock.patch.object(process, "run", side_effect=run) as native:
        assert tj_plugins.main(["update", "--host", "codex"]) == 0
        assert present
        native.reset_mock()
        assert tj_plugins.main(["update", "--host", "codex"]) == 0
        native.assert_not_called()


def test_inventory_from_an_old_catalog_cannot_record_a_new_source_pin(
    home: Path,
) -> None:
    old = publish(home, "a" * 40, "a" * 12)
    installed(home, "a" * 12)
    publish(home, "b" * 40, "b" * 12)
    catalog.remember([old])
    assert not catalog.current("tj-agents", "agent", Digests(home.parent / "state"))


@pytest.mark.parametrize(
    "outcome", ["uninstalled", "error", "omitted", "missing-cache"]
)
def test_a_native_add_cannot_report_success_without_converging(
    home: Path, outcome: str
) -> None:
    entry = publish(home, "a" * 40, "a" * 12)
    changed = False

    def run(command: list[str], **_options: object) -> subprocess.CompletedProcess[str]:
        nonlocal changed
        if command[:3] == ["codex", "plugin", "list"]:
            rows = [entry]
            if changed:
                if outcome == "uninstalled":
                    rows = [{**entry, "installed": False}]
                elif outcome == "error":
                    rows = [{**entry, "error": "unusable"}]
                elif outcome == "omitted":
                    rows = []
            return subprocess.CompletedProcess(
                command, 0, json.dumps({"installed": rows}), ""
            )
        if command[:3] == ["codex", "plugin", "add"]:
            changed = True
            if outcome != "missing-cache":
                installed(home, "a" * 12)
        return subprocess.CompletedProcess(command, 0, "", "")

    with mock.patch.object(process, "run", side_effect=run):
        assert tj_plugins.main(["update", "--host", "codex"]) == 1
    failure = jobs.recent_failure(home.parent / "state", jobs.Job.UPDATE)
    assert failure is not None and "did not converge" in failure


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
