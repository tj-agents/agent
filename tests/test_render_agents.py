"""Agent publishes one lane agent per rung for each host, with resolved values."""

import json
import re
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
LANES = ROOT / ".agents" / "agent" / "resources" / "lanes"
CLAUDE = LANES / "agents" / "claude"
CODEX = LANES / "agents" / "codex"


def table(host: str) -> dict[str, Any]:
    value: dict[str, Any] = json.loads(
        (LANES / f"{host}.json").read_text(encoding="utf-8-sig")
    )
    return value


def front_matter(path: Path) -> dict[str, str]:
    match = re.match(r"(?s)\A---\n(.*?)\n---\n", path.read_text(encoding="utf-8"))
    assert match, f"{path} has no front matter"
    pairs = (line.split(": ", 1) for line in match.group(1).splitlines())
    return dict(pairs)


def test_render_agents_reports_the_committed_agents_current() -> None:
    done = subprocess.run(
        [sys.executable, "-B", str(LANES / "render_agents.py"), "--check"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert done.returncode == 0, done.stdout


@pytest.mark.parametrize(
    ("host", "directory", "suffix"),
    [
        ("claude", CLAUDE, ".md"),
        ("codex", CODEX, ".toml"),
    ],
)
def test_render_agents_emits_one_agent_per_rung_and_no_others(
    host: str, directory: Path, suffix: str
) -> None:
    expected = {f"lane-{lane.lower()}{suffix}" for lane in table(host)["lanes"]}
    assert {path.name for path in directory.iterdir()} == expected


def test_render_agents_gives_claude_agents_the_resolved_rung() -> None:
    for lane, rung in table("claude")["lanes"].items():
        fields = front_matter(CLAUDE / f"lane-{lane.lower()}.md")
        assert fields["name"] == f"lane-{lane.lower()}"
        assert fields["model"] == rung["model"]
        assert fields.get("effort") == rung.get("effort")
        assert fields["disallowedTools"] == "Agent"
        assert fields["model"] != "claude-fable-5"


def test_render_agents_gives_codex_agents_the_resolved_rung_including_effort() -> None:
    for lane, rung in table("codex")["lanes"].items():
        agent = tomllib.loads(
            (CODEX / f"lane-{lane.lower()}.toml").read_text(encoding="utf-8")
        )
        assert agent["name"] == f"lane_{lane.lower()}"
        assert agent["model"] == rung["model"]
        # Codex has no per-skill effort key, so the agent is the only place it lands.
        assert agent["model_reasoning_effort"] == rung["reasoning_effort"]
        assert agent["agents"] == {"enabled": False}


def test_render_agents_makes_claude_l1_only_return_the_codex_handoff() -> None:
    body = (CLAUDE / "lane-l1.md").read_text(encoding="utf-8")
    assert "required native handoff request to your parent" in body
    assert "The parent owns authorization and context preparation." in body
    assert "Codex L1" in body
    assert "Do not inspect, execute, delegate" in body


def test_agent_plugin_ships_the_claude_lane_agents_for_discovery() -> None:
    shipped = ROOT / "plugins" / "agent" / "agents"
    for path in CLAUDE.iterdir():
        assert (shipped / path.name).read_bytes() == path.read_bytes()
    assert {path.name for path in shipped.iterdir()} == {
        path.name for path in CLAUDE.iterdir()
    }
