import contextlib
import importlib.util
import io
import json
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
    monkeypatch.setattr(module, "_load_agent_cli", lambda: cli)
    if host == Host.CODEX:
        monkeypatch.setattr(cli.host_launch(host), "resolve_executable", executable)
    return Launcher(host, module, cli, tmp_path, prompt, launch, item, executable)


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


def test_parent_item_and_fields_reach_launch(launcher: Launcher) -> None:
    code, _, err = launcher.run(
        "--lane",
        "L4",
        "--item",
        "98",
        "--parent",
        "claude:p",
        "--item-field",
        "attribution=TechPlatform",
    )
    assert code == 0, err
    launcher.item.assert_called_once_with(
        "98",
        "agent#98 review",
        launcher.directory,
        "claude:p",
        [("attribution", "TechPlatform")],
    )
    assert launcher.launch.call_args.kwargs == {
        "item": "auto-item",
        "parent": "claude:p",
    }
    assert '"parent tab" (claude:p)' in launcher.launch.call_args.args[3][-1]


def test_without_parent_prompt_has_no_standing_rule(launcher: Launcher) -> None:
    code, _, err = launcher.run("--lane", "L4")
    assert code == 0, err
    assert "Standing rule" not in launcher.launch.call_args.args[3][-1]
    launcher.item.assert_called_once_with(
        None, "agent#98 review", launcher.directory, None, []
    )


def test_calling_session_becomes_parent(
    launcher: Launcher, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CODEX_THREAD_ID", "caller")
    code, _, err = launcher.run("--lane", "L4")
    assert code == 0, err
    assert launcher.launch.call_args.kwargs["parent"] == "codex:caller"


def test_unconfirmed_launch_exits_three(launcher: Launcher) -> None:
    launcher.launch.side_effect = launcher.cli.LaunchTimeout("launch not confirmed")
    code, _, err = launcher.run("--lane", "L4")
    assert code == 3
    assert "check the terminal" in err


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
