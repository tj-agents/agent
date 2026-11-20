"""Verify the native CLI release and the Agent source pins before delivery."""

import argparse
import ast
import json
import re
import subprocess
import sys
import tomllib
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

ASSETS = frozenset(
    {"tj-x86_64-pc-windows-msvc.zip", "tj-x86_64-unknown-linux-gnu.tar.gz"}
)


class DeliveryError(RuntimeError):
    pass


def revision(root: Path) -> str:
    source = root / ".agents/agent/runtime/native_runtime.py"
    module = ast.parse(source.read_text(encoding="utf-8"))
    values = []
    for node in module.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "NATIVE_REVISION"
            for target in node.targets
        ):
            values.append(ast.literal_eval(node.value))
    if (
        len(values) != 1
        or not isinstance(values[0], str)
        or not re.fullmatch("[0-9a-f]{40}", values[0])
    ):
        raise DeliveryError("native_runtime.py must pin one immutable CLI revision")
    pinned: str = values[0]
    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    if project["tool"]["uv"]["sources"]["tj-cli"]["rev"] != pinned:
        raise DeliveryError(
            "The tj-cli library and native CLI pins differ; advance them together"
        )
    lock = tomllib.loads((root / "uv.lock").read_text(encoding="utf-8"))
    libraries = [package for package in lock["package"] if package["name"] == "tj-cli"]
    if len(libraries) != 1:
        raise DeliveryError("uv.lock must contain exactly one tj-cli library")
    locked = urlsplit(libraries[0]["source"]["git"])
    if locked.fragment != pinned or parse_qs(locked.query).get("rev") != [pinned]:
        raise DeliveryError("uv.lock does not resolve the pinned native CLI revision")
    return pinned


def api(endpoint: str) -> dict[str, object]:
    result = subprocess.run(
        ["gh", "api", f"repos/tj-agents/cli/{endpoint}"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
        check=False,
    )
    if result.returncode:
        raise DeliveryError(
            f"CLI delivery could not be verified: {result.stderr.strip()}"
        )
    value = json.loads(result.stdout)
    if not isinstance(value, dict):
        raise DeliveryError("GitHub returned an invalid CLI delivery response")
    return value


def release_problems(pinned: str, release: dict[str, object]) -> list[str]:
    problems = []
    if release.get("draft") is not False or release.get("prerelease") is not False:
        problems.append("the native CLI release is not published as stable")
    if (
        release.get("tag_name") != f"main-{pinned}"
        or release.get("target_commitish") != pinned
    ):
        problems.append(
            "the native CLI release does not identify the exact pinned commit"
        )
    assets = release.get("assets")
    present = (
        {
            asset.get("name")
            for asset in assets
            if isinstance(asset, dict)
            and isinstance(asset.get("size"), int)
            and asset["size"] > 0
        }
        if isinstance(assets, list)
        else set()
    )
    problems.extend(
        f"missing published native CLI asset: {name}"
        for name in sorted(ASSETS - present)
    )
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument(
        "--latest",
        action="store_true",
        help="Also require CLI main to have reached Agent's pin",
    )
    args = parser.parse_args(argv)
    try:
        pinned = revision(args.root)
        problems = release_problems(pinned, api(f"releases/tags/main-{pinned}"))
        if args.latest:
            reference = api("git/ref/heads/main").get("object")
            latest = reference.get("sha") if isinstance(reference, dict) else None
            if not isinstance(latest, str) or not re.fullmatch("[0-9a-f]{40}", latest):
                raise DeliveryError("GitHub did not identify CLI main")
            if latest != pinned:
                problems.extend(
                    release_problems(latest, api(f"releases/tags/main-{latest}"))
                )
                problems.append(
                    f"CLI main {latest} has not reached Agent's pin {pinned}; "
                    "the Renovate update is pending"
                )
        if problems:
            raise DeliveryError("; ".join(problems))
    except (
        DeliveryError,
        OSError,
        ValueError,
        KeyError,
        subprocess.SubprocessError,
    ) as error:
        print(f"CLI delivery: {error}", file=sys.stderr)
        return 1
    print(
        f"CLI delivery verified: {pinned}, Windows and Linux releases, "
        "matching library lock"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
