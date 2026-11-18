from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import agent_cli
import agent_store
import lifecycle
import pytest
import tj_status
import work_item
from launch_contract import Liveness
from launch_errors import LaunchError
from tj_agent.host import Host


def test_status_resolves_bare_ids_through_the_project_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    canonical = "azure-devops:dev.azure.com/contoso:42"
    reference = Mock(return_value=canonical)
    store = Mock(return_value={"item": {"ref": canonical}, "sessions": []})
    monkeypatch.setattr(work_item, "reference", reference)
    monkeypatch.setattr(agent_store, "run", store)
    monkeypatch.setattr(lifecycle, "liveness", dict)
    monkeypatch.setattr(
        lifecycle, "tj_item", Mock(return_value={"state": "In Progress"})
    )

    [item] = lifecycle.open_items(["42"], cwd=tmp_path)

    reference.assert_called_once_with(tmp_path, "42")
    store.assert_called_once_with("sessions", "--item", canonical)
    assert item.ref == canonical


def test_status_reports_missing_tracker_without_querying_store(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(lifecycle, "liveness", dict)
    monkeypatch.setattr(
        work_item, "reference", Mock(side_effect=LaunchError("no tracker configured"))
    )
    store = Mock(side_effect=AssertionError("unexpected store lookup"))
    monkeypatch.setattr(agent_store, "run", store)

    assert tj_status.main(["42"]) == 1

    assert "no tracker configured" in capsys.readouterr().err
    store.assert_not_called()


@pytest.mark.parametrize("invalid", [True, False])
def test_item_lookup_requires_verified_liveness_only_for_its_linked_hosts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, invalid: bool
) -> None:
    session = {
        "host": "claude",
        "native_id": "thread",
        "state": "active",
        "session": "claude:thread",
    }
    sessions = [session]
    if invalid:
        sessions.append({**session, "host": "codex", "session": "codex:thread"})
    monkeypatch.setattr(work_item, "reference", lambda _cwd, ref: ref)
    monkeypatch.setattr(
        agent_store,
        "run",
        lambda *args: {"item": {"ref": "item"}, "sessions": sessions},
    )
    monkeypatch.setattr(lifecycle, "tj_item", lambda *args, **kwargs: {})

    def inventory(*, strict: bool = False) -> dict[str, object]:
        if invalid and strict:
            raise LaunchError("invalid native process id")
        return {}

    def adapter(host: Host) -> SimpleNamespace:
        if not invalid:
            assert host is Host.CLAUDE, "an unrelated provider must not block this item"
        return SimpleNamespace(liveness=inventory)

    monkeypatch.setattr(agent_cli, "host_launch", adapter)
    if invalid:
        with pytest.raises(lifecycle.LifecycleError, match="liveness is unverified"):
            lifecycle.open_items(["item"], cwd=tmp_path)
    else:
        [item] = lifecycle.open_items(["item"], cwd=tmp_path)
        assert item.agents[0].status == "dead"


def test_status_passes_canonical_filter_to_managed_session_snapshot(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    canonical = "jira:tracker:ABC-42"
    reference = Mock(return_value=canonical)
    snapshot = Mock(return_value=tj_status.StatusReport())
    monkeypatch.setattr(work_item, "reference", reference)
    monkeypatch.setattr(tj_status, "snapshot", snapshot)

    assert tj_status.main(["ABC-42", "--json"]) == 0

    reference.assert_called_once_with(Path.cwd(), "ABC-42")
    snapshot.assert_called_once_with([canonical], Path.cwd())
    assert '"sessions"' in capsys.readouterr().out


def test_absence_from_daemon_inventory_does_not_authorize_item_reuse(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(work_item, "reference", lambda _cwd, ref: ref)
    monkeypatch.setattr(
        agent_store,
        "run",
        lambda *args: {
            "item": {"ref": "item"},
            "sessions": [
                {"host": "codex", "native_id": "thread", "session": "codex:thread"}
            ],
        },
    )
    monkeypatch.setattr(lifecycle, "liveness", lambda _hosts: {Host.CODEX: {}})
    monkeypatch.setattr(lifecycle, "tj_item", lambda *args, **kwargs: {})
    with pytest.raises(lifecycle.LifecycleError, match="no native process evidence"):
        lifecycle.open_items(["item"], cwd=tmp_path)


@pytest.mark.parametrize("live", [None, Liveness(False), Liveness(True, "busy")])
def test_ended_codex_history_preserves_available_liveness(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, live: Liveness | None
) -> None:
    monkeypatch.setattr(work_item, "reference", lambda _cwd, ref: ref)
    monkeypatch.setattr(
        agent_store,
        "run",
        lambda *args: {
            "item": {"ref": "item"},
            "sessions": [
                {
                    "host": "codex",
                    "native_id": "thread",
                    "session": "codex:thread",
                    "state": "ended",
                }
            ],
        },
    )
    monkeypatch.setattr(
        lifecycle,
        "liveness",
        lambda _hosts: {Host.CODEX: {"thread": live} if live else {}},
    )
    monkeypatch.setattr(lifecycle, "tj_item", lambda *args, **kwargs: {})

    [item] = lifecycle.open_items(["item"], cwd=tmp_path)

    assert item.agents[0].status == ("busy" if live and live.alive else "ended")
