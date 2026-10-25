#!/usr/bin/env python
"""Resolve a lane to one harness's model and effort.

  resolve.py --host claude --lane L5          -> {"model": "claude-sonnet-5", "effort": "medium"}
  resolve.py --host codex  --lane L7 --format env
  resolve.py --lanes                          -> the rung list, both harnesses, for a diff

Knows nothing about tasks, repos or skills: a lane in, a model out. Which lane a task belongs to is a
judgement the `lanes` skill states; this file only prices it. The tables in this directory are the only
place a model name is written, so nothing here hardcodes one.

Contract: an unknown lane or host is an error, never a silent fallback -- a mistyped lane that quietly
resolved to the default rung would spend the wrong model with nothing to notice it.
"""

import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
HOSTS = ("claude", "codex")


def table(host):
    path = os.environ.get(f"LANES_{host.upper()}") or os.path.join(HERE, f"{host}.json")
    try:
        with open(path, encoding="utf-8-sig") as f:
            return json.load(f)
    except FileNotFoundError:
        raise SystemExit(f"lanes: no table for host {host!r} at {path}")
    except json.JSONDecodeError as exc:
        raise SystemExit(f"lanes: {path} is not valid JSON: {exc}")


def resolve(host, lane):
    if host not in HOSTS:
        raise SystemExit(f"lanes: unknown host {host!r}; known hosts are {', '.join(HOSTS)}")
    data = table(host)
    lanes = data.get("lanes") or {}
    if lane not in lanes:
        raise SystemExit(f"lanes: unknown lane {lane!r} for {host}; table has {' '.join(sorted(lanes))}")

    rung = lanes[lane]
    effort_key = data.get("effort_key", "effort")
    out = {"lane": lane, "host": host, "model": rung["model"]}
    if effort_key in rung:
        out["effort"] = rung[effort_key]
        out["effort_key"] = effort_key
    if rung.get("context_ceiling") is not None:
        out["context_ceiling"] = rung["context_ceiling"]
    if rung.get("handoff") is not None:
        out["handoff"] = rung["handoff"]
    return out


def frontier(host):
    """The tier above the ladder. No lane resolves to it; only an explicit user request selects it."""
    if host not in HOSTS:
        raise SystemExit(f"lanes: unknown host {host!r}; known hosts are {', '.join(HOSTS)}")
    data = table(host)
    tier = data.get("frontier") or {}
    if not tier.get("model"):
        raise SystemExit(f"lanes: the {host} table prices no frontier tier")
    effort_key = data.get("effort_key", "effort")
    out = {"host": host, "model": tier["model"]}
    if effort_key in tier:
        out["effort"] = tier[effort_key]
        out["effort_key"] = effort_key
    return out


def ladder():
    return {host: {lane: rung for lane, rung in table(host)["lanes"].items()} for host in HOSTS}


def main():
    p = argparse.ArgumentParser(add_help=True)
    p.add_argument("--host", choices=HOSTS)
    p.add_argument("--lane")
    p.add_argument("--lanes", action="store_true", help="print both ladders side by side and exit")
    p.add_argument("--format", choices=["json", "env", "line"], default="json")
    a = p.parse_args()

    if a.lanes:
        both = ladder()
        for lane in sorted(both["claude"]):
            c, x = both["claude"][lane], both["codex"].get(lane, {})
            print(
                f"{lane}  claude: {c['model']}"
                f"{'/' + c['effort'] if 'effort' in c else ''}"
                f"   codex: {x.get('model', '-')}"
                f"{'/' + x['reasoning_effort'] if 'reasoning_effort' in x else ''}"
            )
        return

    if not a.host or not a.lane:
        p.error("--host and --lane are both required unless --lanes is given")

    r = resolve(a.host, a.lane)
    if a.format == "env":
        print(f"MODEL={r['model']}")
        if "effort" in r:
            print(f"EFFORT={r['effort']}")
    elif a.format == "line":
        print(f"{r['model']}{' ' + r['effort'] if 'effort' in r else ''}")
    else:
        print(json.dumps(r))


if __name__ == "__main__":
    main()
