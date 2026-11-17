import shutil
import subprocess
from pathlib import Path
from unittest import mock

import agent_store
import pytest
import work_item
from launch_errors import LaunchError


def link(ref: str, role: str) -> dict[str, object]:
    return {"item": {"ref": ref}, "role": role}


def test_parent_item_ref_prefers_the_unique_owner_link() -> None:
    shown = {"session": {"links": [link("1", "reviewer"), link("2", "owner")]}}
    with mock.patch.object(agent_store, "run", return_value=shown):
        assert work_item.parent_item_ref("claude:p") == "2"


def test_parent_item_ref_uses_a_unique_non_owner_link() -> None:
    shown = {"session": {"links": [link("5", "reviewer")]}}
    with mock.patch.object(agent_store, "run", return_value=shown):
        assert work_item.parent_item_ref("claude:p") == "5"


@pytest.mark.parametrize("role", ["owner", "reviewer"])
def test_parent_item_ref_refuses_ambiguous_links(role: str) -> None:
    shown = {"session": {"links": [link("1", role), link("2", role)]}}
    with (
        mock.patch.object(agent_store, "run", return_value=shown),
        pytest.raises(LaunchError, match="several work items; pass --item"),
    ):
        work_item.parent_item_ref("claude:p")


def test_parent_item_ref_is_none_without_any_links() -> None:
    shown: dict[str, object] = {"session": {"links": []}}
    with mock.patch.object(agent_store, "run", return_value=shown):
        assert work_item.parent_item_ref("claude:p") is None


def test_parent_item_ref_is_none_when_the_session_is_unknown() -> None:
    error = agent_store.AgentStoreError("not-found", "unknown session")
    with mock.patch.object(agent_store, "run", side_effect=error):
        assert work_item.parent_item_ref("claude:p") is None


@pytest.mark.parametrize("code", ["missing", "unexpected"])
def test_parent_store_failures_are_reported(code: str) -> None:
    error = agent_store.AgentStoreError(code, "store unavailable")
    with (
        mock.patch.object(agent_store, "run", side_effect=error),
        pytest.raises(LaunchError, match="parent work item") as raised,
    ):
        work_item.parent_item_ref("claude:p")
    assert raised.value.__cause__ is error


@pytest.mark.parametrize(
    "value",
    [
        "https://dev.azure.com/contoso/Project/_workitems/edit/42",
        "https://tracker.example/browse/ABC-12",
        "azure-devops:dev.azure.com/contoso:42",
        "jira:tracker.example:ABC-12",
    ],
)
def test_full_references_need_no_tracker_command(value: str) -> None:
    run = mock.Mock(side_effect=AssertionError("unexpected tracker command"))
    assert work_item.reference(Path("/work"), value, run=run) == value
    run.assert_not_called()


def test_bare_reference_uses_the_project_tracker_without_creating() -> None:
    canonical = "azure-devops:dev.azure.com/contoso:42"
    run = mock.Mock(
        return_value=subprocess.CompletedProcess(
            [], 0, '{"ref": "' + canonical + '"}', ""
        )
    )
    with mock.patch.object(shutil, "which", return_value="/usr/bin/tj"):
        assert work_item.reference(Path("/work"), "42", run=run) == canonical
    assert run.call_args.args[0] == [
        "/usr/bin/tj",
        "item",
        "--cwd",
        str(Path("/work")),
        "reference",
        "42",
    ]


def test_bare_reference_without_tj_is_a_launch_error() -> None:
    with (
        mock.patch.object(shutil, "which", return_value=None),
        pytest.raises(LaunchError, match="tj is not on PATH"),
    ):
        work_item.reference(Path("/work"), "42")


@pytest.mark.parametrize(
    ("stdout", "returncode", "message"),
    [
        ("", 2, "tj item reference failed"),
        ("not json", 0, "printed no JSON"),
        ("{}", 0, 'no "ref"'),
    ],
)
def test_reference_command_errors_are_reported(
    stdout: str, returncode: int, message: str
) -> None:
    run = mock.Mock(
        return_value=subprocess.CompletedProcess([], returncode, stdout, "")
    )
    with (
        mock.patch.object(shutil, "which", return_value="/usr/bin/tj"),
        pytest.raises(LaunchError, match=message),
    ):
        work_item.reference(Path("/work"), "42", run=run)


def test_empty_reference_is_refused() -> None:
    with pytest.raises(LaunchError, match="existing work-item reference"):
        work_item.reference(Path("/work"), " ")
