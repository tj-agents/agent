from pathlib import Path
from unittest.mock import Mock

import agent_store
import lifecycle
import pytest
import tj_status
import work_item
from launch_errors import LaunchError


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
