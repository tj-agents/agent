import contextlib
import importlib.util
import io
import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from unittest.mock import Mock

import pytest
from tj_agent.host import Host

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "plugins/agent/.agents/agent"


def load(path: Path, name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@dataclass(frozen=True, slots=True)
class Launcher:
    host: Host
    module: ModuleType
    cli: ModuleType
    directory: Path
    prompt: Path
    launch: Mock
    item: Mock
    executable: Mock
    resolve_item: Callable[[str | None, Path], str | None]

    def run(self, *arguments: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = self.module.main(
                    [
                        "--working-directory",
                        str(self.directory),
                        "--prompt-path",
                        str(self.prompt),
                        "--title",
                        "agent#98 review",
                        *arguments,
                    ]
                )
            except SystemExit as error:
                assert isinstance(error.code, int)
                code = error.code
        return code, out.getvalue(), err.getvalue()


@pytest.fixture(params=tuple(Host))
def launcher(
    request: pytest.FixtureRequest, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Launcher:
    host: Host = request.param
    prompt = tmp_path / "prompt.md"
    prompt.write_text("Do the work.", encoding="utf-8")
    cli = load(PACKAGE / "runtime/agent_cli.py", f"handoff_cli_{host}")
    resolve_item = cli.resolve_item
    launch = Mock(return_value={"launch": {"launch_id": "launch-1"}})
    item = Mock(return_value="auto-item")
    executable = Mock(return_value=("/bin/codex", {}))
    monkeypatch.setattr(cli, "launch", launch)
    monkeypatch.setattr(cli, "resolve_item", item)
    monkeypatch.setattr(
        cli.agent_store, "run", Mock(return_value={"session": {"title": "parent tab"}})
    )
    for name in ("TJ_RUN_LAUNCH_ID", "CLAUDE_CODE_SESSION_ID", "CODEX_THREAD_ID"):
        monkeypatch.delenv(name, raising=False)
    module = load(PACKAGE / f"resources/handoff/launch_{host}.py", f"handoff_{host}")
    monkeypatch.setattr(module, "agent_cli", cli)
    if host == Host.CODEX:
        monkeypatch.setattr(module.codex_launch, "resolve_executable", executable)
    return Launcher(
        host, module, cli, tmp_path, prompt, launch, item, executable, resolve_item
    )


@pytest.mark.parametrize("lane", ("L1", "L2", "L3", "L4", "L5", "L6"))
def test_lane_controls_both_model_and_effort(launcher: Launcher, lane: str) -> None:
    code, out, err = launcher.run("--lane", lane)
    entry = launcher.cli.resolve_lane(launcher.host, lane=lane)
    if entry.get("handoff"):
        assert code == 1
        assert "--host codex" in err
        launcher.launch.assert_not_called()
        launcher.item.assert_not_called()
        return
    assert code == 0, err
    host, directory, title, arguments = launcher.launch.call_args.args
    assert (host, directory, title) == (
        launcher.host,
        launcher.directory,
        "agent#98 review",
    )
    assert arguments[arguments.index("--model") + 1] == entry["model"]
    if launcher.host == Host.CODEX:
        assert (
            arguments[arguments.index("-c") + 1]
            == f"model_reasoning_effort={entry['effort']}"
        )
        assert arguments[:2] == ["--cd", str(launcher.directory)]
    else:
        assert arguments[arguments.index("--effort") + 1] == entry["effort"]
        assert arguments[:2] == ["--name", "agent#98 review"]
    assert f"lane {lane}" in out
    assert json.loads(out.splitlines()[-1])["launch_id"] == "launch-1"


@pytest.mark.parametrize("lane_arguments", ((), ("--lane", "L4")))
@pytest.mark.parametrize(
    "override",
    (
        ("--model", "unlabelled-model"),
        ("--reasoning-effort", "max"),
        ("--effort", "max"),
        ("--frontier",),
    ),
)
def test_handoff_rejects_selection_bypasses(
    launcher: Launcher, lane_arguments: tuple[str, ...], override: tuple[str, ...]
) -> None:
    code, _, _ = launcher.run(*lane_arguments, *override)
    assert code == 2
    launcher.launch.assert_not_called()
    launcher.item.assert_not_called()
    launcher.executable.assert_not_called()


def test_handoff_requires_a_lane(launcher: Launcher) -> None:
    code, _, err = launcher.run()
    assert code == 2
    assert "--lane" in err
    launcher.launch.assert_not_called()
    launcher.item.assert_not_called()


@pytest.mark.parametrize("lane", ("L7", "L99"))
def test_handoff_refuses_in_session_and_unknown_lanes(
    launcher: Launcher, lane: str
) -> None:
    code, _, _ = launcher.run("--lane", lane)
    assert code != 0
    launcher.launch.assert_not_called()
    launcher.item.assert_not_called()


def test_parent_and_item_reach_launch(launcher: Launcher) -> None:
    code, _, err = launcher.run(
        "--lane",
        "L4",
        "--item",
        "98",
        "--parent",
        "claude:p",
    )
    assert code == 0, err
    launcher.item.assert_called_once_with("98", launcher.directory)
    assert launcher.launch.call_args.kwargs == {
        "item": "auto-item",
        "parent": "claude:p",
    }
    assert "tj message to contact claude:p" in launcher.launch.call_args.args[3][-1]


@pytest.mark.parametrize(
    "parent_links", [[], [{"role": "owner", "item": {"ref": "7"}}]]
)
def test_handoff_without_item_launches_unlinked_without_tracker_access(
    launcher: Launcher,
    monkeypatch: pytest.MonkeyPatch,
    parent_links: list[dict[str, object]],
) -> None:
    monkeypatch.setattr(launcher.cli, "resolve_item", launcher.resolve_item)
    monkeypatch.setattr(
        launcher.cli.agent_store,
        "run",
        Mock(return_value={"session": {"title": "parent", "links": parent_links}}),
    )
    reference = Mock(side_effect=AssertionError("unexpected tracker call"))
    monkeypatch.setattr(launcher.cli.work_item, "reference", reference)
    code, _, err = launcher.run("--lane", "L4", "--parent", "claude:p")
    assert code == 0, err
    assert launcher.launch.call_args.kwargs == {"item": None, "parent": "claude:p"}
    reference.assert_not_called()


@pytest.mark.parametrize(
    "ref",
    [
        "https://dev.azure.com/contoso/Project/_workitems/edit/42",
        "https://tracker.example/browse/ABC-12",
    ],
)
def test_handoff_links_tracker_urls_without_creating(
    launcher: Launcher, monkeypatch: pytest.MonkeyPatch, ref: str
) -> None:
    monkeypatch.setattr(launcher.cli, "resolve_item", launcher.resolve_item)
    code, _, err = launcher.run("--lane", "L4", "--item", ref)
    assert code == 0, err
    assert launcher.launch.call_args.kwargs["item"] == ref


def test_handoff_rejects_retired_creation_fields(launcher: Launcher) -> None:
    code, _, err = launcher.run(
        "--lane", "L4", "--item-field", "attribution=TechPlatform"
    )
    assert code == 2
    assert "unrecognized arguments" in err
    launcher.launch.assert_not_called()


def test_without_parent_prompt_has_no_standing_rule(launcher: Launcher) -> None:
    code, _, err = launcher.run("--lane", "L4")
    assert code == 0, err
    assert "Standing rule" not in launcher.launch.call_args.args[3][-1]
    launcher.item.assert_called_once_with(None, launcher.directory)


def test_inherited_session_and_launch_identities_do_not_create_a_parent(
    launcher: Launcher, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CODEX_THREAD_ID", "caller")
    monkeypatch.setenv("TJ_RUN_LAUNCH_ID", "previous")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "other")
    code, out, err = launcher.run("--lane", "L4")
    assert code == 0, err
    assert launcher.launch.call_args.kwargs["parent"] is None
    assert "parent" not in json.loads(out.splitlines()[-1])
    assert "Standing rule" not in launcher.launch.call_args.args[3][-1]


def test_unconfirmed_launch_exits_three(launcher: Launcher) -> None:
    launcher.launch.side_effect = launcher.cli.LaunchTimeout("launch not confirmed")
    code, _, err = launcher.run("--lane", "L4")
    assert code == 3
    assert "check the terminal" in err


def test_requested_codex_floor_is_checked_during_actual_preparation(
    launcher: Launcher,
) -> None:
    if launcher.host is not Host.CODEX:
        return
    code, _, err = launcher.run("--lane", "L4", "--minimum-version", "0.171.0")
    assert code == 0, err
    launcher.executable.side_effect = launcher.cli.LaunchError(
        "no compatible executable"
    )
    prepare = launcher.launch.call_args.kwargs["prepare"]
    with pytest.raises(launcher.cli.LaunchError, match="no compatible executable"):
        prepare(launcher.directory, "Floor", [], "tj", {})
    launcher.executable.assert_called_once_with(
        minimum="0.171.0", match_daemon=True, start_if_absent=True
    )


def test_blank_title_is_refused(launcher: Launcher) -> None:
    code, _, _ = launcher.run("--lane", "L4", "--title", " ")
    assert code == 1
    launcher.launch.assert_not_called()


def test_claude_permission_flag_reaches_native_cli(launcher: Launcher) -> None:
    if launcher.host != Host.CLAUDE:
        return
    code, _, err = launcher.run("--lane", "L4", "--dangerously-skip-permissions")
    assert code == 0, err
    assert launcher.launch.call_args.args[3][0] == "--dangerously-skip-permissions"


def test_claude_prepared_tab_keeps_lane_model_and_effort(
    launcher: Launcher,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if launcher.host != Host.CLAUDE:
        return
    adapter = launcher.cli.host_launch(Host.CLAUDE)
    monkeypatch.setattr(adapter, "executable", lambda: "native-claude")
    code, _, err = launcher.run("--lane", "L2")
    assert code == 0, err
    host, directory, title, arguments = launcher.launch.call_args.args
    prepared = adapter.prepare(directory, title, arguments, "tj", {})
    entry = launcher.cli.resolve_lane(host, lane="L2")
    assert prepared.command[prepared.command.index("--model") + 1] == entry["model"]
    assert prepared.command[prepared.command.index("--effort") + 1] == entry["effort"]
