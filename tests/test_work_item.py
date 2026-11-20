import shutil
import subprocess
from pathlib import Path
from unittest import mock

import pytest
import work_item
from launch_errors import LaunchError


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
    with pytest.raises(LaunchError, match=r"--item is empty.*omit --item"):
        work_item.reference(Path("/work"), " ")
