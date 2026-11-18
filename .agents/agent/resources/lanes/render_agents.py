"""Render Agent's lane agents from the lane tables (Python 3.12+).

  render_agents.py           rewrite agents/claude/*.md and agents/codex/*.toml
  render_agents.py --check   exit 1 when a committed agent differs from the tables

The Agent plugin ships agents/claude as its Claude `agents/` directory (`agent:lane-l1`
to `agent:lane-l7`). Codex cannot load an agent from a plugin, so `tj install` copies
agents/codex into the Codex profile as `lane_l1` to `lane_l7`.
"""

import argparse
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

TOOLS = ("Read", "Glob", "Grep", "Write", "Edit", "Bash")
EXTENSIONS = {"claude": ".md", "codex": ".toml"}


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8-sig").replace("\r\n", "\n").strip()


def table(lanes: Path, host: str) -> dict[str, Any]:
    value: dict[str, Any] = json.loads(read(lanes / f"{host}.json"))
    return value


def body(lanes: Path, rung: Mapping[str, Any]) -> str:
    target = rung.get("handoff")
    if target is None:
        return read(lanes / "agent-body.md")
    return read(lanes / "handoff-agent-body.md").format(
        host=target["host"].title(), lane=target["lane"]
    )


def description(lane: str, rung: Mapping[str, Any]) -> str:
    return f"Runs one delegated task at lane {lane}. {rung['summary']}"


def claude_agent(lane: str, rung: Mapping[str, Any], effort_key: str, text: str) -> str:
    fields = {
        "name": f"lane-{lane.lower()}",
        "description": description(lane, rung),
        "kind": "lane",
        "model": rung["model"],
    }
    if effort_key in rung:
        fields["effort"] = rung[effort_key]
    fields["tools"] = ", ".join(TOOLS)
    fields["disallowedTools"] = "Agent"
    header = "\n".join(f"{key}: {value}" for key, value in fields.items())
    return f"---\n{header}\n---\n\n{text}\n"


def codex_agent(lane: str, rung: Mapping[str, Any], effort_key: str, text: str) -> str:
    fields = {
        "name": f"lane_{lane.lower()}",
        "description": description(lane, rung),
        "model": rung["model"],
        "model_reasoning_effort": rung[effort_key],
        "sandbox_mode": "workspace-write",
        "developer_instructions": text,
    }
    lines = "\n".join(
        f"{key} = {json.dumps(value, ensure_ascii=False)}"
        for key, value in fields.items()
    )
    return f"{lines}\n\n[agents]\nenabled = false\n"


def render(lanes: Path, host: str) -> dict[str, str]:
    """One agent file per rung of `host`'s table, keyed by file name."""
    data = table(lanes, host)
    effort_key = data["effort_key"]
    agent = claude_agent if host == "claude" else codex_agent
    return {
        f"lane-{lane.lower()}{EXTENSIONS[host]}": agent(
            lane, rung, effort_key, body(lanes, rung)
        )
        for lane, rung in data["lanes"].items()
    }


def drift(lanes: Path) -> list[str]:
    problems = []
    for host in EXTENSIONS:
        directory = lanes / "agents" / host
        expected = render(lanes, host)
        actual = {path.name for path in directory.glob("*") if path.is_file()}
        stale = {
            name
            for name in expected.keys() & actual
            if (directory / name).read_bytes() != expected[name].encode("utf-8")
        }
        for label, names in (
            ("MISSING", expected.keys() - actual),
            ("EXTRA", actual - expected.keys()),
            ("STALE", stale),
        ):
            problems += [f"{label}: agents/{host}/{name}" for name in sorted(names)]
    return problems


def write(lanes: Path) -> None:
    for host in EXTENSIONS:
        directory = lanes / "agents" / host
        directory.mkdir(parents=True, exist_ok=True)
        expected = render(lanes, host)
        for path in directory.glob("*"):
            if path.is_file() and path.name not in expected:
                path.unlink()
        for name, text in expected.items():
            (directory / name).write_bytes(text.encode("utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    lanes = Path(__file__).resolve().parent
    if not parser.parse_args().check:
        write(lanes)
        return 0
    problems = drift(lanes)
    for problem in problems:
        print(problem)
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
