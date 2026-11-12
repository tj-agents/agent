import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from unittest import mock

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugins/agent/.agents/agent/runtime"))

import agent_store  # noqa: E402
import work_item  # noqa: E402
from launch_errors import LaunchError  # noqa: E402


def link(ref: str, role: str) -> dict[str, object]:
    return {"item": {"ref": ref}, "role": role}


def test_parent_item_ref_prefers_the_owner_role_link() -> None:
    shown = {
        "session": {
            "links": [link("1", "reviewer"), link("2", "owner"), link("3", "owner")]
        }
    }
    with mock.patch.object(agent_store, "run", return_value=shown):
        assert work_item.parent_item_ref("claude:p") == "2"


def test_parent_item_ref_falls_back_to_the_first_link_without_an_owner() -> None:
    shown = {"session": {"links": [link("5", "reviewer"), link("6", "reviewer")]}}
    with mock.patch.object(agent_store, "run", return_value=shown):
        assert work_item.parent_item_ref("claude:p") == "5"


def test_parent_item_ref_is_none_without_any_links() -> None:
    shown: dict[str, object] = {"session": {"links": []}}
    with mock.patch.object(agent_store, "run", return_value=shown):
        assert work_item.parent_item_ref("claude:p") is None


def test_parent_item_ref_is_none_when_agent_store_fails() -> None:
    error = agent_store.AgentStoreError("not-found", "unknown session")
    with mock.patch.object(agent_store, "run", side_effect=error):
        assert work_item.parent_item_ref("claude:p") is None


@pytest.mark.parametrize("code", ["missing", "unexpected"])
def test_parent_store_failures_do_not_create_an_unparented_handoff(code: str) -> None:
    error = agent_store.AgentStoreError(code, "store unavailable")
    with (
        mock.patch.object(agent_store, "run", side_effect=error),
        pytest.raises(LaunchError, match="parent work item") as raised,
    ):
        work_item.parent_item_ref("claude:p")
    assert raised.value.__cause__ is error


def fake_run(
    stdout: str = "", returncode: int = 0
) -> Callable[..., subprocess.CompletedProcess[str]]:
    def run(command: list[str], **_options: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, returncode, stdout, "")

    return run


def test_create_runs_tj_item_create_and_returns_its_ref() -> None:
    run = mock.Mock(side_effect=fake_run('{"ref": "99"}'))
    with mock.patch.object(shutil, "which", return_value="/usr/bin/tj"):
        ref = work_item.create(Path("/work"), "a title", None, {}, run=run)
    assert ref == "99"
    assert run.call_args.args[0] == [
        "/usr/bin/tj",
        "item",
        "--cwd",
        str(Path("/work")),
        "create",
        "--title",
        "a title",
    ]


def test_create_links_under_the_given_parent_ref() -> None:
    run = mock.Mock(side_effect=fake_run('{"ref": "100"}'))
    with mock.patch.object(shutil, "which", return_value="/usr/bin/tj"):
        work_item.create(Path("/work"), "a title", "42", {}, run=run)
    assert run.call_args.args[0][-2:] == ["--parent", "42"]


def test_create_passes_each_field() -> None:
    run = mock.Mock(side_effect=fake_run('{"ref": "101"}'))
    with mock.patch.object(shutil, "which", return_value="/usr/bin/tj"):
        work_item.create(
            Path("/work"), "a title", None, {"attribution": "Techops"}, run=run
        )
    assert run.call_args.args[0][-2:] == ["--field", "attribution=Techops"]


def test_create_without_tj_on_path_is_a_launch_error() -> None:
    with (
        mock.patch.object(shutil, "which", return_value=None),
        pytest.raises(LaunchError, match="tj is not on PATH"),
    ):
        work_item.create(Path("/work"), "a title", None, {})


def test_create_a_failing_command_is_a_launch_error() -> None:
    run = mock.Mock(side_effect=fake_run("", returncode=1))
    with (
        mock.patch.object(shutil, "which", return_value="/usr/bin/tj"),
        pytest.raises(LaunchError, match="tj item create failed"),
    ):
        work_item.create(Path("/work"), "a title", None, {}, run=run)


def test_create_non_json_output_is_a_launch_error() -> None:
    run = mock.Mock(side_effect=fake_run("not json"))
    with (
        mock.patch.object(shutil, "which", return_value="/usr/bin/tj"),
        pytest.raises(LaunchError, match="printed no JSON"),
    ):
        work_item.create(Path("/work"), "a title", None, {}, run=run)


def test_create_without_a_ref_field_is_a_launch_error() -> None:
    run = mock.Mock(side_effect=fake_run("{}"))
    with (
        mock.patch.object(shutil, "which", return_value="/usr/bin/tj"),
        pytest.raises(LaunchError, match='no "ref"'),
    ):
        work_item.create(Path("/work"), "a title", None, {}, run=run)
