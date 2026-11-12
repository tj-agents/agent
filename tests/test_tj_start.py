from pathlib import Path
from unittest.mock import Mock

import agent_cli
import lifecycle
import pytest
import tj_start
from launch_errors import LaunchError
from tj_agent.host import Host


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
    monkeypatch.setattr(lifecycle, "tj_item", tracker)
    monkeypatch.setattr(lifecycle, "open_items", Mock(return_value=[]))
    monkeypatch.setattr(lifecycle, "repository_name", Mock(return_value="agent"))
    monkeypatch.setattr(tj_start, "create_worktree", Mock(return_value=str(tmp_path)))
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
