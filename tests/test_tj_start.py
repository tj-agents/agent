import json
import shutil
import sys
from pathlib import Path
from unittest.mock import Mock

import agent_cli
import agent_store
import lifecycle
import pytest
import required_plugins
import tj_start
from launch_errors import LaunchError
from tj_agent import worktrees
from tj_agent.host import Host


@pytest.fixture(autouse=True)
def no_creation_owner(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(worktrees, "current_owner", lambda _environ: None)


@pytest.mark.parametrize(
    "arguments",
    (
        ("175",),
        ("175", "--model", "arbitrary-model"),
        ("175", "--lane", "L4", "--model", "arbitrary-model"),
    ),
)
def test_start_rejects_missing_lane_and_model_overrides(
    arguments: tuple[str, ...], monkeypatch: pytest.MonkeyPatch
) -> None:
    start = Mock()
    monkeypatch.setattr(tj_start, "start", start)
    with pytest.raises(SystemExit, match="2") as error:
        tj_start.main(list(arguments))
    assert error.value.code == 2
    start.assert_not_called()


@pytest.mark.parametrize(
    ("host", "lane"), ((Host.CLAUDE, "L1"), (Host.CODEX, "L7"), (Host.CODEX, "L99"))
)
def test_start_rejects_unlaunchable_lanes_before_tracker_changes(
    host: Host, lane: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tracker = Mock()
    monkeypatch.setattr(lifecycle, "tj_item", tracker)
    with pytest.raises(LaunchError, match=r"routes|in-session|unknown lane"):
        tj_start.start("175", host, tmp_path, lane)
    tracker.assert_not_called()


def test_start_resolves_lane_before_creating_worktree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    card = {
        "ref": "github:github.com/tj-agents/agent:175",
        "title": "Lane selection",
        "url": "issue",
    }
    tracker = Mock(return_value=card)
    launch = Mock(return_value={"launch": {"launch_id": "launch-1"}})
    creator = Mock(return_value=str(tmp_path))
    monkeypatch.setattr(lifecycle, "tj_item", tracker)
    monkeypatch.setattr(lifecycle, "open_items", Mock(return_value=[]))
    monkeypatch.setattr(lifecycle, "repository_name", Mock(return_value="agent"))
    monkeypatch.setattr(tj_start, "create_worktree", creator)
    monkeypatch.setattr(tj_start, "unique_title", Mock(return_value="agent#175"))
    monkeypatch.setattr(agent_cli, "launch", launch)
    result = tj_start.start("175", Host.CODEX, tmp_path, "L4")
    entry = agent_cli.resolve_lane(Host.CODEX, lane="L4")
    arguments = launch.call_args.args[3]
    assert arguments[arguments.index("--model") + 1] == entry["model"]
    assert (
        arguments[arguments.index("-c") + 1]
        == f"model_reasoning_effort={entry['effort']}"
    )
    assert result["launch_id"] == "launch-1"
    creator.assert_called_once_with(tmp_path, "Feature/175-LaneSelection", Host.CODEX)


def test_start_releases_creator_claim_before_the_child_starts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner = worktrees.Owner(Host.CODEX, "parent", "parent-launch")
    tracker = Mock(
        return_value={
            "ref": "github:github.com/tj-agents/agent:175",
            "title": "Canonical worktree",
        }
    )
    release = Mock()

    def launch(*_arguments: object, **_keywords: object) -> dict[str, object]:
        release.assert_called_once_with(
            "release-worktree",
            "--session",
            "codex:parent",
            "--launch-id",
            "parent-launch",
            "--path",
            str(tmp_path),
        )
        return {"launch": {"launch_id": "child-launch"}}

    monkeypatch.setattr(lifecycle, "tj_item", tracker)
    monkeypatch.setattr(lifecycle, "open_items", Mock(return_value=[]))
    monkeypatch.setattr(lifecycle, "repository_name", Mock(return_value="agent"))
    monkeypatch.setattr(tj_start, "create_worktree", Mock(return_value=str(tmp_path)))
    monkeypatch.setattr(tj_start, "unique_title", Mock(return_value="agent#175"))
    monkeypatch.setattr(agent_store, "run", release)
    monkeypatch.setattr(agent_cli, "launch", launch)
    result = tj_start.start("175", Host.CODEX, tmp_path, "L4", owner=owner)
    assert result["launch_id"] == "child-launch"


@pytest.mark.parametrize("host", tuple(Host))
def test_create_worktree_runs_the_enabled_base_creator(
    host: Host, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plugin = tmp_path / "Base package ü"
    script = plugin / "hooks/worktrees/create_worktree.py"
    script.parent.mkdir(parents=True)
    script.write_text("", encoding="utf-8")
    repository = tmp_path / "Repository ü"
    repository.mkdir()
    path = repository / ".worktrees/Feature-Canonical"
    inventory = Mock(return_value={"base@tj-agents": plugin})
    command = Mock(return_value=json.dumps({"path": str(path)}, indent=2) + "\n")
    monkeypatch.setattr(required_plugins, "inventory_roots", inventory)
    monkeypatch.setattr(lifecycle, "command", command)

    assert tj_start.create_worktree(repository, "Feature/Canonical", host) == str(path)
    inventory.assert_called_once_with(repository, host, identities=("base@tj-agents",))
    command.assert_called_once_with(
        [sys.executable, "-B", str(script.resolve()), "Feature/Canonical"],
        cwd=repository,
    )


@pytest.mark.parametrize("host", tuple(Host))
def test_create_worktree_executes_the_delivered_script_with_literal_arguments(
    host: Host, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plugin = tmp_path / "Base package ü"
    script = plugin / "hooks/worktrees/create_worktree.py"
    script.parent.mkdir(parents=True)
    shutil.copyfile(Path(__file__).parent / "fixtures/worktree_creator.py", script)
    repository = tmp_path / "Repository ü"
    repository.mkdir()
    path = repository / ".worktrees/Feature-Canonical"
    script.with_suffix(".request.json").write_text(
        json.dumps({"path": str(path)}, indent=2), encoding="utf-8"
    )
    monkeypatch.setattr(
        required_plugins,
        "inventory_roots",
        Mock(return_value={"base@tj-agents": plugin}),
    )

    assert tj_start.create_worktree(repository, "Feature/Canonical", host) == str(path)
    assert json.loads(
        script.with_suffix(".actual.json").read_text(encoding="utf-8")
    ) == {"arguments": ["Feature/Canonical"], "cwd": str(repository)}


@pytest.mark.parametrize("available", (False, True))
def test_create_worktree_requires_base_creator_without_a_fallback(
    available: bool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plugin = tmp_path / "base"
    plugin.mkdir()
    inventory = {"base@tj-agents": plugin} if available else {}
    command = Mock()
    monkeypatch.setattr(
        required_plugins, "inventory_roots", Mock(return_value=inventory)
    )
    monkeypatch.setattr(lifecycle, "command", command)
    with pytest.raises(
        lifecycle.LifecycleError,
        match=r"creator requires|creator is unavailable",
    ):
        tj_start.create_worktree(tmp_path, "Feature/Canonical", Host.CODEX)
    command.assert_not_called()


@pytest.mark.parametrize("payload", ([], {"path": 1}, {"path": "relative"}, {}))
def test_create_worktree_rejects_invalid_creator_results(
    payload: object, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script = tmp_path / "create_worktree.py"
    monkeypatch.setattr(
        required_plugins,
        "inventory_roots",
        Mock(return_value={"base@tj-agents": tmp_path}),
    )
    monkeypatch.setattr(required_plugins, "private_path", Mock(return_value=script))
    monkeypatch.setattr(lifecycle, "command", Mock(return_value=json.dumps(payload)))
    with pytest.raises(lifecycle.LifecycleError, match="creator returned no path"):
        tj_start.create_worktree(tmp_path, "Feature/Canonical", Host.CODEX)


@pytest.mark.parametrize(
    "failure",
    (
        lifecycle.LifecycleError("creator refused ownership"),
        required_plugins.RequiredPluginsError("native inventory failed"),
    ),
)
def test_start_reports_creation_failure_before_progress_or_launch(
    failure: RuntimeError,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    tracker = Mock(return_value={"ref": "github:github.com/tj-agents/agent:175"})
    launch = Mock()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(lifecycle, "tj_item", tracker)
    monkeypatch.setattr(lifecycle, "open_items", Mock(return_value=[]))
    monkeypatch.setattr(tj_start, "create_worktree", Mock(side_effect=failure))
    monkeypatch.setattr(agent_cli, "launch", launch)

    assert tj_start.main(["175", "--codex", "--lane", "L4"]) == 1
    assert str(failure) in capsys.readouterr().err
    tracker.assert_called_once_with("resolve", "175", cwd=tmp_path)
    launch.assert_not_called()
