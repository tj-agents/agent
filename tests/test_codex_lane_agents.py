import json
from pathlib import Path

import pytest
from codex import lane_agents

ROOT = Path(__file__).resolve().parents[1]
PACKAGED = ROOT / lane_agents.PACKAGED


@pytest.fixture
def profile(tmp_path: Path) -> Path:
    path = tmp_path / ".codex"
    path.mkdir()
    return path


def delivered(profile: Path) -> dict[str, bytes]:
    return {
        path.name: path.read_bytes()
        for path in (profile / "agents").glob("lane-*.toml")
    }


def packaged() -> dict[str, bytes]:
    return {path.name: path.read_bytes() for path in PACKAGED.glob("lane-*.toml")}


def test_apply_delivers_every_packaged_lane_agent_then_reports_nothing(
    profile: Path,
) -> None:
    changes = lane_agents.apply(ROOT, profile)
    assert len(changes) == len(packaged())
    assert all(change.startswith("add ") for change in changes)
    assert delivered(profile) == packaged()
    assert lane_agents.check(ROOT, profile) == []


def test_apply_adopts_an_identical_agent_another_installer_left(profile: Path) -> None:
    agents = profile / "agents"
    agents.mkdir()
    (agents / "lane-l4.toml").write_bytes(packaged()["lane-l4.toml"])
    assert f"adopt {agents / 'lane-l4.toml'}" in lane_agents.check(ROOT, profile)
    lane_agents.apply(ROOT, profile)
    assert lane_agents.check(ROOT, profile) == []


def test_apply_replaces_an_owned_agent_whose_package_changed(profile: Path) -> None:
    lane_agents.apply(ROOT, profile)
    path = profile / "agents" / "lane-l4.toml"
    ownership = profile / "agents" / lane_agents.OWNERSHIP
    old = b'name = "lane_l4"\n'
    path.write_bytes(old)
    record = json.loads(ownership.read_text(encoding="utf-8"))
    record["files"]["lane-l4.toml"] = lane_agents.digest(old)
    ownership.write_text(json.dumps(record), encoding="utf-8")
    assert lane_agents.check(ROOT, profile) == [f"replace {path}"]
    lane_agents.apply(ROOT, profile)
    assert path.read_bytes() == packaged()["lane-l4.toml"]


def test_apply_refuses_to_overwrite_an_agent_it_does_not_own(profile: Path) -> None:
    agents = profile / "agents"
    agents.mkdir()
    personal = b'name = "lane_l4"\nmodel = "mine"\n'
    (agents / "lane-l4.toml").write_bytes(personal)
    with pytest.raises(lane_agents.LaneAgentError, match="did not deliver it"):
        lane_agents.apply(ROOT, profile)
    assert (agents / "lane-l4.toml").read_bytes() == personal


def test_apply_removes_a_retired_owned_agent_and_keeps_unrelated_ones(
    profile: Path,
) -> None:
    lane_agents.apply(ROOT, profile)
    agents = profile / "agents"
    retired = agents / "lane-l9.toml"
    retired.write_bytes(b"retired\n")
    unrelated = agents / "reviewer.toml"
    unrelated.write_bytes(b"mine\n")
    ownership = agents / lane_agents.OWNERSHIP
    record = json.loads(ownership.read_text(encoding="utf-8"))
    record["files"]["lane-l9.toml"] = lane_agents.digest(b"retired\n")
    ownership.write_text(json.dumps(record), encoding="utf-8")
    assert lane_agents.check(ROOT, profile) == [f"remove {retired}"]
    lane_agents.apply(ROOT, profile)
    assert not retired.exists()
    assert unrelated.read_bytes() == b"mine\n"
    assert (
        "lane-l9.toml" not in json.loads(ownership.read_text(encoding="utf-8"))["files"]
    )


def test_apply_refuses_to_remove_a_modified_retired_agent(profile: Path) -> None:
    lane_agents.apply(ROOT, profile)
    agents = profile / "agents"
    (agents / "lane-l9.toml").write_bytes(b"edited\n")
    ownership = agents / lane_agents.OWNERSHIP
    record = json.loads(ownership.read_text(encoding="utf-8"))
    record["files"]["lane-l9.toml"] = lane_agents.digest(b"retired\n")
    ownership.write_text(json.dumps(record), encoding="utf-8")
    with pytest.raises(lane_agents.LaneAgentError, match="modified retired"):
        lane_agents.apply(ROOT, profile)
    assert (agents / "lane-l9.toml").read_bytes() == b"edited\n"


@pytest.mark.parametrize(
    "record",
    [
        {"owner": "someone-else", "files": {}},
        {"owner": lane_agents.OWNER, "files": {"../config.toml": "0"}},
        {"owner": lane_agents.OWNER, "files": []},
    ],
)
def test_check_rejects_ownership_it_did_not_write(
    profile: Path, record: dict[str, object]
) -> None:
    agents = profile / "agents"
    agents.mkdir()
    (agents / lane_agents.OWNERSHIP).write_text(json.dumps(record), encoding="utf-8")
    with pytest.raises(
        lane_agents.LaneAgentError, match="invalid lane agent ownership"
    ):
        lane_agents.check(ROOT, profile)


def test_check_fails_when_the_package_carries_no_lane_agents(
    tmp_path: Path, profile: Path
) -> None:
    with pytest.raises(
        lane_agents.LaneAgentError, match="no packaged Codex lane agents"
    ):
        lane_agents.check(tmp_path, profile)
