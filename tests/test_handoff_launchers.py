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
PACKAGE = ROOT / ".agents/agent"


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
                        "--host",
                        self.host.value,
                        "--",
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
    item = Mock(side_effect=cli.resolve_item)
    executable = Mock(return_value=("/bin/codex", {}))
    monkeypatch.setattr(cli, "launch", launch)
    monkeypatch.setattr(cli, "resolve_item", item)
    monkeypatch.setattr(
        cli.work_item, "create", Mock(side_effect=AssertionError("unexpected tracker mutation"))
    )
    monkeypatch.setattr(
        cli.work_item, "parent_item_ref", Mock(wraps=cli.work_item.parent_item_ref)
    )
    monkeypatch.setattr(
        cli.agent_store, "run", Mock(return_value={"session": {"title": "parent tab"}})
    )
    for name in ("TJ_RUN_LAUNCH_ID", "CLAUDE_CODE_SESSION_ID", "CODEX_THREAD_ID"):
        monkeypatch.delenv(name, raising=False)
    handler = load(PACKAGE / f"resources/handoff/launch_{host}.py", f"handoff_{host}")
    monkeypatch.setattr(handler, "_load_agent_cli", lambda: cli)
    module = load(PACKAGE / "utility/handoff/scripts/handoff.py", f"handoff_entry_{host}")

    def load_handler(selected_host: Host) -> ModuleType:
        assert selected_host == host
        return handler

    monkeypatch.setattr(module, "_load_handler", load_handler)
    if host == Host.CODEX:
        monkeypatch.setattr(cli.host_launch(host), "resolve_executable", executable)
    return Launcher(host, module, cli, tmp_path, prompt, launch, item, executable)


@pytest.mark.parametrize("lane", ("L1", "L2", "L3", "L4", "L5", "L6"))
def test_lane_controls_both_model_and_effort(launcher: Launcher, lane: str) -> None:
    code, out, err = launcher.run("--lane", lane, "--independent")
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
    code, _, _ = launcher.run(*lane_arguments, *override, "--independent")
    assert code == 2
    launcher.launch.assert_not_called()
    launcher.item.assert_not_called()
    launcher.executable.assert_not_called()


def test_handoff_requires_a_lane(launcher: Launcher) -> None:
    code, _, err = launcher.run("--independent")
    assert code == 2
    assert "--lane" in err
    launcher.launch.assert_not_called()
    launcher.item.assert_not_called()


@pytest.mark.parametrize("lane", ("L7", "L99"))
def test_handoff_refuses_in_session_and_unknown_lanes(
    launcher: Launcher, lane: str
) -> None:
    code, _, _ = launcher.run("--lane", lane, "--independent")
    assert code != 0
    launcher.launch.assert_not_called()
    launcher.item.assert_not_called()


@pytest.mark.parametrize("parent", ("claude:p", "codex:p"))
def test_explicit_existing_item_reaches_launch(launcher: Launcher, parent: str) -> None:
    code, _, err = launcher.run(
        "--lane",
        "L4",
        "--item",
        "98",
        "--parent",
        parent,
    )
    assert code == 0, err
    assert launcher.launch.call_args.kwargs == {
        "item": "98",
        "parent": parent,
    }
    launcher.cli.work_item.parent_item_ref.assert_not_called()
    launcher.cli.work_item.create.assert_not_called()
    assert f'"parent tab" ({parent})' in launcher.launch.call_args.args[3][-1]


@pytest.mark.parametrize("parent", (None, "claude:p", "codex:p"))
def test_generic_handoff_launches_unlinked_without_tracker(
    launcher: Launcher, parent: str | None
) -> None:
    relationship = ("--parent", parent) if parent else ("--independent",)
    code, out, err = launcher.run("--lane", "L4", *relationship)
    assert code == 0, err
    assert launcher.launch.call_args.kwargs == {"item": None, "parent": parent}
    assert json.loads(out.splitlines()[-1])["parent"] == parent
    launcher.cli.work_item.parent_item_ref.assert_not_called()
    launcher.cli.work_item.create.assert_not_called()


@pytest.mark.parametrize("item_arguments", ((), ("--item", "98")))
def test_handoff_rejects_creation_fields_without_explicit_creation(
    launcher: Launcher, item_arguments: tuple[str, ...]
) -> None:
    code, _, err = launcher.run(
        "--lane", "L4", "--independent", *item_arguments,
        "--item-field", "attribution=TechPlatform",
    )
    assert code != 0
    assert "--item-field requires --create-item" in err
    launcher.launch.assert_not_called()
    launcher.cli.work_item.parent_item_ref.assert_not_called()
    launcher.cli.work_item.create.assert_not_called()


@pytest.mark.parametrize("parent", (None, "claude:p", "codex:p"))
def test_explicit_creation_links_the_canonical_parent_item(
    launcher: Launcher, parent: str | None
) -> None:
    launcher.cli.agent_store.run.return_value = {
        "session": {
            "title": "parent tab",
            "links": [
                {"item": {"ref": "reviewed#1"}, "role": "reviewer"},
                {"item": {"ref": "canonical#42"}, "role": "owner"},
            ],
        }
    }
    create = launcher.cli.work_item.create
    create.side_effect = None
    create.return_value = "canonical#99"
    relationship = ("--parent", parent) if parent else ("--independent",)
    code, out, err = launcher.run(
        "--lane", "L4", *relationship, "--create-item",
        "--item-field", "attribution=TechPlatform",
    )
    assert code == 0, err
    create.assert_called_once_with(
        launcher.directory, "agent#98 review", "canonical#42" if parent else None,
        {"attribution": "TechPlatform"},
    )
    assert launcher.launch.call_args.kwargs == {"item": "canonical#99", "parent": parent}
    assert json.loads(out.splitlines()[-1])["parent"] == parent
    if parent:
        launcher.cli.work_item.parent_item_ref.assert_called_once_with(parent)
    else:
        launcher.cli.work_item.parent_item_ref.assert_not_called()
        launcher.cli.agent_store.run.assert_not_called()


def test_explicit_creation_with_unlinked_parent_creates_a_root_item(launcher: Launcher) -> None:
    launcher.cli.agent_store.run.return_value = {"session": {"title": "parent tab", "links": []}}
    create = launcher.cli.work_item.create
    create.side_effect = None
    create.return_value = "canonical#99"
    code, _, err = launcher.run("--lane", "L4", "--parent", "claude:p", "--create-item")
    assert code == 0, err
    create.assert_called_once_with(launcher.directory, "agent#98 review", None, {})
    assert launcher.launch.call_args.kwargs == {"item": "canonical#99", "parent": "claude:p"}


def test_handoff_refuses_existing_item_with_creation(launcher: Launcher) -> None:
    code, _, err = launcher.run(
        "--lane", "L4", "--independent", "--item", "98", "--create-item"
    )
    assert code == 2
    assert "not allowed with argument" in err
    launcher.launch.assert_not_called()
    launcher.item.assert_not_called()
    launcher.executable.assert_not_called()
    launcher.cli.work_item.parent_item_ref.assert_not_called()
    launcher.cli.work_item.create.assert_not_called()


def test_creation_lookup_failure_cannot_create_an_unparented_item(launcher: Launcher) -> None:
    launcher.cli.agent_store.run.side_effect = launcher.cli.agent_store.AgentStoreError(
        "unavailable", "store unavailable"
    )
    code, _, err = launcher.run("--lane", "L4", "--parent", "codex:p", "--create-item")
    assert code == 1
    assert "Could not read the parent work item" in err
    launcher.launch.assert_not_called()
    launcher.cli.work_item.create.assert_not_called()


def test_creation_failure_cannot_launch_unlinked(launcher: Launcher) -> None:
    launcher.cli.work_item.create.side_effect = launcher.cli.LaunchError("tracker unavailable")
    code, _, err = launcher.run("--lane", "L4", "--independent", "--create-item")
    assert code == 1
    assert "tracker unavailable" in err
    launcher.launch.assert_not_called()


@pytest.mark.parametrize("invalid", (("--title", " "), ("--lane", "L7"), ("--prompt-path", "missing.md")))
def test_invalid_creation_request_never_reaches_tracker(
    launcher: Launcher, invalid: tuple[str, ...]
) -> None:
    code, _, _ = launcher.run("--lane", "L4", "--independent", "--create-item", *invalid)
    assert code != 0
    launcher.launch.assert_not_called()
    launcher.cli.work_item.parent_item_ref.assert_not_called()
    launcher.cli.work_item.create.assert_not_called()


def test_without_parent_prompt_has_no_standing_rule(launcher: Launcher) -> None:
    code, _, err = launcher.run("--lane", "L4", "--independent")
    assert code == 0, err
    assert "Standing rule" not in launcher.launch.call_args.args[3][-1]
    assert launcher.launch.call_args.kwargs["item"] is None


def test_independent_launch_ignores_all_ambient_parent_identities(
    launcher: Launcher, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CODEX_THREAD_ID", "caller")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "other-caller")
    monkeypatch.setenv("TJ_RUN_LAUNCH_ID", "ambient-launch")
    code, out, err = launcher.run("--lane", "L4", "--independent")
    assert code == 0, err
    assert launcher.launch.call_args.kwargs["parent"] is None
    assert json.loads(out.splitlines()[-1])["parent"] is None
    assert "Standing rule" not in launcher.launch.call_args.args[3][-1]
    assert launcher.launch.call_args.kwargs["item"] is None


@pytest.mark.parametrize(
    "mode", ((), ("--independent", "--parent", "codex:p"), ("--parent", ""))
)
def test_handoff_requires_one_unambiguous_parent_mode(
    launcher: Launcher, mode: tuple[str, ...]
) -> None:
    code, _, _ = launcher.run("--lane", "L4", *mode)
    assert code == 2
    launcher.launch.assert_not_called()
    launcher.item.assert_not_called()
    launcher.executable.assert_not_called()


def test_unconfirmed_launch_exits_three(launcher: Launcher) -> None:
    launcher.launch.side_effect = launcher.cli.LaunchTimeout("launch not confirmed")
    code, _, err = launcher.run("--lane", "L4", "--independent")
    assert code == 3
    assert "check the terminal" in err


def test_blank_title_is_refused(launcher: Launcher) -> None:
    code, _, _ = launcher.run("--lane", "L4", "--title", " ", "--independent")
    assert code == 1
    launcher.launch.assert_not_called()


def test_claude_permission_flag_reaches_native_cli(launcher: Launcher) -> None:
    if launcher.host != Host.CLAUDE:
        return
    code, _, err = launcher.run(
        "--lane", "L4", "--dangerously-skip-permissions", "--independent"
    )
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
    code, _, err = launcher.run("--lane", "L2", "--independent")
    assert code == 0, err
    host, directory, title, arguments = launcher.launch.call_args.args
    prepared = adapter.prepare(directory, title, arguments, "tj", {})
    entry = launcher.cli.resolve_lane(host, lane="L2")
    assert prepared.command[prepared.command.index("--model") + 1] == entry["model"]
    assert prepared.command[prepared.command.index("--effort") + 1] == entry["effort"]
