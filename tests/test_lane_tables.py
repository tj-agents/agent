"""The lane ladder is Agent's only model decision.

Both host tables pair rung for rung, every handoff lands on a declared lane, and the
resolver prices a lane without a fallback.
"""

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
LANES = ROOT / ".agents" / "agent" / "resources" / "lanes"
HOSTS = ("claude", "codex")
EFFORTS = ("low", "medium", "high", "xhigh", "max")

# Capability order per vendor, most capable first. A rung never outranks the one above.
FAMILY_ORDER = {
    "claude": (
        "claude-fable-5",
        "claude-opus-5-5",
        "claude-sonnet-5",
        "claude-haiku-4-5",
    ),
    "codex": ("gpt-6-astra", "gpt-6.1-sol", "gpt-6.1-terra", "gpt-6-luna"),
}


def table(host: str) -> dict[str, Any]:
    value: dict[str, Any] = json.loads(
        (LANES / f"{host}.json").read_text(encoding="utf-8-sig")
    )
    return value


def resolve(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-B", str(LANES / "resolve.py"), *arguments],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )


def test_tables_declare_the_same_rungs_in_order() -> None:
    claude, codex = (list(table(host)["lanes"]) for host in HOSTS)
    assert claude == codex
    assert claude == sorted(claude)
    assert claude[0] == "L1"


@pytest.mark.parametrize("host", HOSTS)
def test_tables_give_every_rung_a_model_and_a_yaml_safe_summary(host: str) -> None:
    for rung in table(host)["lanes"].values():
        assert rung.get("model")
        # The Claude agent renders the summary into an unquoted YAML description.
        assert rung.get("summary")
        assert ": " not in rung["summary"]


@pytest.mark.parametrize("host", HOSTS)
def test_tables_never_rank_a_rung_above_the_rung_before_it(host: str) -> None:
    order = FAMILY_ORDER[host]
    seen = -1
    for rung in table(host)["lanes"].values():
        if rung.get("handoff"):
            target = rung["handoff"]
            assert host == "claude"
            assert (
                table(target["host"])["lanes"][target["lane"]]["model"]
                == table("codex")["lanes"]["L1"]["model"]
            )
            continue
        rank = order.index(rung["model"])
        assert rank >= seen
        seen = rank


def test_tables_keep_codex_l4_below_the_top_general_family() -> None:
    lanes = table("codex")["lanes"]
    assert lanes["L4"]["model"] == "gpt-6.1-terra"
    assert lanes["L4"]["reasoning_effort"] == "high"
    order = FAMILY_ORDER["codex"]
    assert order.index(lanes["L4"]["model"]) > order.index(lanes["L3"]["model"])


@pytest.mark.parametrize("host", HOSTS)
def test_tables_run_l4_at_high_effort_on_a_different_model_from_l3(host: str) -> None:
    data = table(host)
    assert data["lanes"]["L4"][data["effort_key"]] == "high"
    assert data["lanes"]["L3"]["model"] != data["lanes"]["L4"]["model"]


@pytest.mark.parametrize("host", HOSTS)
def test_tables_declare_known_efforts_or_say_why_a_rung_has_none(host: str) -> None:
    data = table(host)
    for rung in data["lanes"].values():
        if data["effort_key"] in rung:
            assert rung[data["effort_key"]] in EFFORTS
        else:
            assert rung.get("no_effort_reason")


def test_tables_declare_whether_a_skill_can_carry_effort() -> None:
    assert table("claude")["skill_supports_effort"] is True
    assert table("codex")["skill_supports_effort"] is False


def test_tables_record_a_smaller_claude_context_ceiling_for_the_cheapest_rung() -> None:
    ceilings = [rung["context_ceiling"] for rung in table("claude")["lanes"].values()]
    assert all(isinstance(ceiling, int) and ceiling > 0 for ceiling in ceilings)
    assert ceilings[-1] < ceilings[0]


def test_tables_never_select_claude_fable() -> None:
    assert all(
        rung["model"] != "claude-fable-5" for rung in table("claude")["lanes"].values()
    )


@pytest.mark.parametrize("host", HOSTS)
def test_tables_route_every_handoff_to_a_declared_lane(host: str) -> None:
    for rung in table(host)["lanes"].values():
        target = rung.get("handoff")
        if target:
            assert target["host"] in HOSTS
            assert target["host"] != host
            assert target["lane"] in table(target["host"])["lanes"]


def test_resolve_prices_a_lane_with_its_host_model() -> None:
    done = resolve("--host", "claude", "--lane", "L4")
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout)["model"] == table("claude")["lanes"]["L4"]["model"]


def test_resolve_exposes_the_claude_l1_codex_handoff() -> None:
    done = resolve("--host", "claude", "--lane", "L1")
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout)["handoff"] == {"host": "codex", "lane": "L1"}


def test_resolve_prices_codex_l4_with_model_and_effort() -> None:
    done = resolve("--host", "codex", "--lane", "L4")
    assert done.returncode == 0, done.stderr
    resolved = json.loads(done.stdout)
    rung = table("codex")["lanes"]["L4"]
    assert resolved["model"] == rung["model"]
    assert resolved["effort"] == rung["reasoning_effort"]
    assert resolved["effort_key"] == "reasoning_effort"


@pytest.mark.parametrize(
    "arguments",
    [("--host", "claude", "--lane", "L99"), ("--host", "nope", "--lane", "L1")],
)
def test_resolve_fails_rather_than_falling_back(arguments: tuple[str, ...]) -> None:
    done = resolve(*arguments)
    assert done.returncode != 0
    assert "model" not in done.stdout
