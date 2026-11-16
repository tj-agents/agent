import os
from collections.abc import Mapping
from pathlib import Path

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
) -> None:
    """Serve the producer and a catalog pinning it through Git URL rewriting."""
    uri, commit = published_plugins.publish(producer, permissions)
    source = producer.parent / "marketplace"
    (source / ".claude-plugin").mkdir(parents=True)
    (source / ".claude-plugin/marketplace.json").write_text(
        published_plugins.catalog(permissions, commit), encoding="utf-8"
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
