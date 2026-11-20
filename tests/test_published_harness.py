import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import published_harness
import published_plugins
import pytest
from lock_io import LockError

MARKETPLACES = {"tj-agents": {"repository": "tj-agents/marketplace"}}
DECLARED = {
    "claude_allow": ["Bash(tj item:*)"],
    "claude_deny": ["Bash(gh auth login *)"],
    "codex_prefix_rules": [],
}


@pytest.fixture
def producer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    # A zero count makes git ignore any injected GIT_CONFIG_KEY_n/VALUE_n pairs.
    monkeypatch.setenv("GIT_CONFIG_COUNT", "0")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    return tmp_path / "producer"


def publish(
    monkeypatch: pytest.MonkeyPatch,
    producer: Path,
    permissions: Mapping[str, Mapping[str, object]],
    dependencies: Mapping[str, list[object]] | None = None,
) -> None:
    """Serve the producer and a catalog pinning it through Git URL rewriting."""
    uri, commit = published_plugins.publish(producer, permissions)
    source = producer.parent / "marketplace"
    (source / ".claude-plugin").mkdir(parents=True)
    (source / ".claude-plugin/marketplace.json").write_text(
        published_plugins.catalog(permissions, commit, dependencies), encoding="utf-8"
    )
    published_plugins.git(source, "init", "--quiet", "--initial-branch=main")
    published_plugins.git(source, "add", ".")
    published_plugins.git(source, "commit", "--quiet", "-m", "Catalog")
    rewrites = {
        uri: published_plugins.PRODUCER,
        source.as_uri(): "https://github.com/tj-agents/marketplace.git",
    }
    for index, (target, published) in enumerate(rewrites.items()):
        monkeypatch.setenv(f"GIT_CONFIG_KEY_{index}", f"url.{target}.insteadOf")
        monkeypatch.setenv(f"GIT_CONFIG_VALUE_{index}", published)
    monkeypatch.setenv("GIT_CONFIG_COUNT", str(len(rewrites)))


def test_declarations_read_each_plugin_at_its_pinned_revision(
    monkeypatch: pytest.MonkeyPatch, producer: Path
) -> None:
    publish(monkeypatch, producer, {"alpha": DECLARED, "beta": published_plugins.EMPTY})
    unreleased = producer / "plugins/alpha/harness.json"
    unreleased.write_text(
        published_plugins.harness("tj-agents", "alpha", published_plugins.EMPTY),
        encoding="utf-8",
    )
    published_plugins.git(producer, "commit", "--quiet", "-am", "Unreleased")

    declared = published_harness.declarations(
        MARKETPLACES, ["beta@tj-agents", "alpha@tj-agents"]
    )

    assert declared == {
        "alpha@tj-agents": DECLARED,
        "beta@tj-agents": published_plugins.EMPTY,
    }


def test_declarations_reject_a_published_machine_specific_rule(
    monkeypatch: pytest.MonkeyPatch, producer: Path
) -> None:
    rooted = {**DECLARED, "claude_allow": ["Bash(python -B ${PLUGIN_ROOT}/x.py)"]}
    publish(monkeypatch, producer, {"alpha": rooted})

    with pytest.raises(LockError, match=r"alpha@tj-agents.*machine-specific path"):
        published_harness.declarations(MARKETPLACES, ["alpha@tj-agents"])


def test_declarations_reject_a_plugin_its_marketplace_does_not_publish(
    monkeypatch: pytest.MonkeyPatch, producer: Path
) -> None:
    publish(monkeypatch, producer, {"alpha": DECLARED})

    with pytest.raises(LockError, match="gamma@tj-agents is not published"):
        published_harness.declarations(MARKETPLACES, ["gamma@tj-agents"])


def test_declarations_follow_published_dependencies_transitively(
    monkeypatch: pytest.MonkeyPatch, producer: Path
) -> None:
    publish(
        monkeypatch,
        producer,
        {
            "alpha": published_plugins.EMPTY,
            "beta": DECLARED,
            "gamma": published_plugins.EMPTY,
        },
        {"alpha": ["beta@tj-agents"], "beta": [{"name": "gamma"}]},
    )

    declared = published_harness.declarations(MARKETPLACES, ["alpha@tj-agents"])

    assert declared == {
        "alpha@tj-agents": published_plugins.EMPTY,
        "beta@tj-agents": DECLARED,
        "gamma@tj-agents": published_plugins.EMPTY,
    }


def catalogs(
    **plugins: list[object],
) -> dict[str, dict[str, Any]]:
    return {
        "tj-agents": {
            "plugins": [
                {"name": name, "dependencies": dependencies}
                for name, dependencies in plugins.items()
            ]
        }
    }


def test_closure_terminates_on_a_dependency_cycle() -> None:
    documents = catalogs(alpha=["beta"], beta=["alpha", "gamma@tj-agents"], gamma=[])

    assert published_harness.closure(["alpha@tj-agents"], documents.get) == [
        "alpha@tj-agents",
        "beta@tj-agents",
        "gamma@tj-agents",
    ]


def test_closure_refuses_a_dependency_outside_the_declared_marketplaces() -> None:
    documents = catalogs(alpha=["kit@elsewhere"])

    with pytest.raises(
        LockError,
        match="alpha@tj-agents depends on kit@elsewhere, but the selection does not "
        "declare the elsewhere marketplace",
    ):
        published_harness.closure(["alpha@tj-agents"], documents.get)


def test_closure_names_the_dependent_of_an_unpublished_dependency() -> None:
    documents = catalogs(alpha=["beta"])

    with pytest.raises(
        LockError,
        match="alpha@tj-agents depends on beta@tj-agents: "
        "beta@tj-agents is not published",
    ):
        published_harness.closure(["alpha@tj-agents"], documents.get)


def test_closure_rejects_an_invalid_dependency() -> None:
    documents = catalogs(alpha=[{"version": "1"}])

    with pytest.raises(
        LockError, match="alpha@tj-agents: invalid published dependency"
    ):
        published_harness.closure(["alpha@tj-agents"], documents.get)


def write_catalog(directory: Path, **plugins: list[object]) -> None:
    path = directory / "tj-agents/.claude-plugin/marketplace.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(catalogs(**plugins)["tj-agents"]), encoding="utf-8")


def test_local_closure_reads_the_claude_catalog_before_codex(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    write_catalog(tmp_path / "codex/.tmp/marketplaces", alpha=["stale"], stale=[])
    write_catalog(tmp_path / "claude/plugins/marketplaces", alpha=["beta"], beta=[])

    assert published_harness.local_closure(MARKETPLACES, ["alpha@tj-agents"]) == [
        "alpha@tj-agents",
        "beta@tj-agents",
    ]


def test_local_closure_falls_back_to_the_codex_catalog(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    write_catalog(tmp_path / "codex/.tmp/marketplaces", alpha=["beta"], beta=[])

    assert published_harness.local_closure(MARKETPLACES, ["alpha@tj-agents"]) == [
        "alpha@tj-agents",
        "beta@tj-agents",
    ]


def test_local_closure_without_a_fetched_catalog_fails_loudly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))

    with pytest.raises(LockError, match="No local tj-agents marketplace catalog"):
        published_harness.local_closure(MARKETPLACES, ["alpha@tj-agents"])
