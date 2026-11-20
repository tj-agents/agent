import shutil
import subprocess
from collections.abc import Callable
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


@pytest.mark.parametrize(
    "value",
    [
        "42",
        "azure-devops:dev.azure.com/contoso:42",
        "https://dev.azure.com/contoso/Project/_workitems/edit/42",
    ],
)
def test_existing_item_checks_tracker_existence_after_reference_resolution(
    value: str,
) -> None:
    canonical = "azure-devops:dev.azure.com/contoso:42"
    run = mock.Mock(
        return_value=subprocess.CompletedProcess(
            [], 0, '{"ref": "' + canonical + '"}', ""
        )
    )
    with mock.patch.object(shutil, "which", return_value="/usr/bin/tj"):
        assert work_item.existing(Path("/work"), value, run=run) == canonical
    expected = []
    if ":" not in value:
        expected.append(
            ["/usr/bin/tj", "item", "--cwd", str(Path("/work")), "reference", value]
        )
    expected.append(
        [
            "/usr/bin/tj",
            "item",
            "--cwd",
            str(Path("/work")),
            "resolve",
            canonical if ":" not in value else value,
        ]
    )
    assert [call.args[0] for call in run.call_args_list] == expected


@pytest.mark.parametrize(
    ("stdout", "returncode", "message"),
    [
        ("", 1, "item not found"),
        ("not json", 0, "printed no JSON"),
        ('{"ref": " "}', 0, 'no "ref"'),
    ],
)
def test_existing_item_refuses_missing_or_invalid_tracker_results(
    stdout: str, returncode: int, message: str
) -> None:
    run = mock.Mock(
        return_value=subprocess.CompletedProcess(
            [], returncode, stdout, "item not found"
        )
    )
    with (
        mock.patch.object(shutil, "which", return_value="/usr/bin/tj"),
        pytest.raises(LaunchError, match=message),
    ):
        work_item.existing(Path("/work"), "jira:tracker:ABC-12", run=run)


@pytest.mark.parametrize("operation", [work_item.existing, work_item.reference])
def test_tracker_process_failure_is_reported(operation: Callable[..., str]) -> None:
    error = subprocess.TimeoutExpired("tj", 30)
    run = mock.Mock(side_effect=error)
    with (
        mock.patch.object(shutil, "which", return_value="/usr/bin/tj"),
        pytest.raises(LaunchError, match="could not run") as raised,
    ):
        operation(Path("/work"), "42", run=run)
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
