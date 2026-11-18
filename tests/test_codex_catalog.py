from __future__ import annotations

import json
from pathlib import Path

import pytest
from codex import catalog
from tree_digest import Digests


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    directory = tmp_path / "codex"
    monkeypatch.setenv("CODEX_HOME", str(directory))
    monkeypatch.setenv("AGENT_STATE_DIRECTORY", str(tmp_path / "state"))
    return directory


def entry(version: str) -> dict[str, object]:
    return {
        "pluginId": "agent@tj-agents",
        "version": version,
        "source": {
            "source": "git-subdir",
            "url": "https://github.com/tj-agents/agent.git",
            "path": "plugins/agent",
            "sha": "a" * 40,
        },
    }


def installed(home: Path, version: str) -> Path:
    root = home / "plugins/cache/tj-agents/agent" / version
    (root / ".codex-plugin").mkdir(parents=True)
    (root / ".codex-plugin/plugin.json").write_text(
        json.dumps({"name": "agent", "version": version}), encoding="utf-8"
    )
    return root


def test_verified_cache_acquisition_persists_without_marketplace_metadata(
    home: Path,
) -> None:
    build = installed(home, "local")
    (build / "startup.py").write_text("original", encoding="utf-8")

    catalog.remember([entry("local")], verified_install=True)

    assert not (home / ".tmp/marketplaces").exists()
    assert catalog.current("tj-agents", "agent", Digests(home.parent / "state"))


def test_unverified_native_inventory_cannot_create_a_catalog_receipt(
    home: Path,
) -> None:
    installed(home, "local")

    catalog.remember([entry("local")])

    assert not (home.parent / "state" / catalog.RECEIPT).exists()
    assert not catalog.current("tj-agents", "agent", Digests(home.parent / "state"))


def test_invalid_inventory_evicts_a_verified_catalog_build(home: Path) -> None:
    installed(home, "local")
    catalog.remember([entry("local")], verified_install=True)

    catalog.remember([{**entry("local"), "installed": False}])

    assert not catalog.current("tj-agents", "agent", Digests(home.parent / "state"))


def test_mutated_cache_payload_is_not_current(home: Path) -> None:
    build = installed(home, "local")
    script = build / "startup.py"
    script.write_text("original", encoding="utf-8")
    catalog.remember([entry("local")], verified_install=True)

    script.write_text("mutated", encoding="utf-8")

    assert not catalog.current("tj-agents", "agent", Digests(home.parent / "state"))


def test_immutable_cache_requires_its_recorded_manifest_version(home: Path) -> None:
    build = installed(home, "a" * 12)
    catalog.remember([entry("a" * 12)], verified_install=True)

    (build / ".codex-plugin/plugin.json").write_text(
        '{"name":"agent","version":"other"}', encoding="utf-8"
    )

    assert not catalog.current("tj-agents", "agent", Digests(home.parent / "state"))
