"""Published plugin harness declarations for tests: offline or local Git publishers."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path

PRODUCER = "https://github.com/tj-agents/fixture-producer.git"
EMPTY: dict[str, list[object]] = {"claude_allow": [], "codex_prefix_rules": []}


def none_declared(
    marketplaces: Mapping[str, Mapping[str, str]], identities: Iterable[str]
) -> dict[str, dict[str, list[object]]]:
    """Stand in for `published_harness.declarations`: no plugin declares anything."""
    return {identity: dict(EMPTY) for identity in identities}


def declared_with(
    dependencies: Mapping[str, Iterable[str]],
) -> Callable[
    [Mapping[str, Mapping[str, str]], Iterable[str]], dict[str, dict[str, list[object]]]
]:
    """Stand in for `published_harness.declarations` with dependencies by plugin name.

    Bare names resolve in the dependent's marketplace; no plugin declares permissions.
    """

    def declarations(
        marketplaces: Mapping[str, Mapping[str, str]], identities: Iterable[str]
    ) -> dict[str, dict[str, list[object]]]:
        resolved: set[str] = set()
        pending = list(identities)
        while pending:
            identity = pending.pop()
            if identity in resolved:
                continue
            resolved.add(identity)
            name, _, marketplace = identity.partition("@")
            pending.extend(
                f"{dependency}@{marketplace}"
                for dependency in dependencies.get(name, ())
            )
        return {identity: dict(EMPTY) for identity in resolved}

    return declarations


def git(directory: Path, *arguments: str) -> str:
    result = subprocess.run(
        [
            "git",
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "-c",
            "core.hooksPath=",
            "-C",
            str(directory),
            *arguments,
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=15,
        check=True,
    )
    return result.stdout.strip()


def harness(marketplace: str, name: str, permissions: Mapping[str, object]) -> str:
    return json.dumps(
        {
            "schema_version": 1,
            "plugin": f"{marketplace}/{name}",
            "source_roots": [f".agents/{name}"],
            "requires": {
                "marketplaces": [
                    {"id": marketplace, "repository": "tj-agents/marketplace"}
                ],
                "plugins": [f"{marketplace}/{name}"],
                "hooks": [],
                "permissions": permissions,
            },
        }
    )


def publish(
    producer: Path, permissions: Mapping[str, Mapping[str, object]]
) -> tuple[str, str]:
    """Commit each plugin's harness to a local producer; return its URI and commit."""
    producer.mkdir(parents=True, exist_ok=True)
    git(producer, "init", "--quiet", "--initial-branch=main")
    for name, declared in permissions.items():
        path = producer / "plugins" / name / "harness.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(harness("tj-agents", name, declared), encoding="utf-8")
    git(producer, "add", ".")
    git(producer, "commit", "--quiet", "-m", "Publish plugins")
    return producer.as_uri(), git(producer, "rev-parse", "HEAD")


def catalog(
    names: Iterable[str],
    commit: str,
    dependencies: Mapping[str, list[object]] | None = None,
) -> str:
    """A Claude marketplace catalog pinning every plugin to the fixture producer."""
    return json.dumps(
        {
            "name": "tj-agents",
            "plugins": [
                {
                    "name": name,
                    "source": {
                        "source": "git-subdir",
                        "url": PRODUCER,
                        "path": f"./plugins/{name}",
                        "sha": commit,
                    },
                    **(
                        {"dependencies": dependencies[name]}
                        if dependencies and name in dependencies
                        else {}
                    ),
                }
                for name in sorted(names)
            ],
        }
    )
