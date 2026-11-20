"""Adoption uses dependency edges published by the pinned producer catalog."""

import json
import subprocess
from pathlib import Path

import adopt_plugins
import lock_io
import pytest
import update_marketplace_plugins


def commit(root: Path) -> None:
    subprocess.run(["git", "-C", str(root), "add", "."], check=True, timeout=15)
    subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "-c",
            "core.hooksPath=",
            "commit",
            "--quiet",
            "-m",
            "Marketplace fixture",
        ],
        check=True,
        timeout=15,
    )


@pytest.fixture
def marketplace(tmp_path: Path) -> Path:
    subprocess.run(["git", "init", "--quiet", str(tmp_path)], check=True, timeout=15)
    plugins = [
        {"name": name, "repository": name, "revision": "a" * 40, "kind": "shared"}
        for name in ("base", "docs", "work")
    ]
    (tmp_path / "plugins.json").write_text(
        json.dumps({"name": "tj-agents", "plugins": plugins}), encoding="utf-8"
    )
    (tmp_path / ".claude-plugin").mkdir()
    (tmp_path / ".claude-plugin/marketplace.json").write_text(
        json.dumps(
            {
                "name": "tj-agents",
                "plugins": [{"name": name} for name in ("base", "docs", "work")],
            }
        ),
        encoding="utf-8",
    )
    commit(tmp_path)
    return tmp_path


def test_catalog_only_commit_updates_offline_adoption_dependencies(
    marketplace: Path,
) -> None:
    catalog = marketplace / ".claude-plugin/marketplace.json"
    document = json.loads(catalog.read_text(encoding="utf-8"))
    document["plugins"][-1]["dependencies"] = ["base", "docs"]
    catalog.write_text(json.dumps(document), encoding="utf-8")
    commit(marketplace)
    expected = subprocess.run(
        ["git", "-C", str(marketplace), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
        timeout=15,
    ).stdout.strip()
    (marketplace / "plugins.json").write_text("uncommitted edit", encoding="utf-8")

    snapshot = json.loads(update_marketplace_plugins.snapshot(marketplace))

    assert snapshot["revision"] == expected
    assert adopt_plugins.expand({"work"}, adopt_plugins.load_roster(snapshot)) == [
        "base",
        "docs",
        "work",
    ]


@pytest.mark.parametrize("names", [("base", "work"), ("base", "docs", "work", "extra")])
def test_snapshot_refuses_incomplete_catalog(
    marketplace: Path, names: tuple[str, ...]
) -> None:
    (marketplace / ".claude-plugin/marketplace.json").write_text(
        json.dumps(
            {"name": "tj-agents", "plugins": [{"name": name} for name in names]}
        ),
        encoding="utf-8",
    )
    commit(marketplace)

    with pytest.raises(ValueError, match="name different plugins"):
        update_marketplace_plugins.snapshot(marketplace)


def test_adoption_refuses_snapshot_without_dependency_inventory(
    marketplace: Path,
) -> None:
    snapshot = json.loads(update_marketplace_plugins.snapshot(marketplace))
    del snapshot["marketplace"]["plugins"][-1]["dependencies"]

    with pytest.raises(lock_io.LockError, match=r"work\.dependencies"):
        adopt_plugins.load_roster(snapshot)
