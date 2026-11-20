"""A matching version string cannot hide a missing producer release or stale lock."""

import json
from pathlib import Path

import delivery
import pytest

PIN = "a" * 40


@pytest.fixture
def source(tmp_path: Path) -> Path:
    runtime = tmp_path / ".agents/agent/runtime"
    runtime.mkdir(parents=True)
    (runtime / "native_runtime.py").write_text(
        f'NATIVE_REVISION = "{PIN}"\n', encoding="utf-8"
    )
    (tmp_path / "pyproject.toml").write_text(
        f'[tool.uv.sources]\ntj-cli = {{rev = "{PIN}"}}\n', encoding="utf-8"
    )
    (tmp_path / "uv.lock").write_text(
        '[[package]]\nname = "tj-cli"\n'
        f'source = {{git = "https://github.com/tj-agents/cli.git?rev={PIN}#{PIN}"}}\n',
        encoding="utf-8",
    )
    return tmp_path


def published() -> dict[str, object]:
    return {
        "draft": False,
        "prerelease": False,
        "tag_name": f"main-{PIN}",
        "target_commitish": PIN,
        "assets": [{"name": name, "size": 42} for name in delivery.ASSETS],
    }


def test_matching_pins_require_both_platform_releases(source: Path) -> None:
    assert delivery.revision(source) == PIN
    assert delivery.release_problems(PIN, published()) == []
    incomplete = published() | {
        "assets": [{"name": "tj-x86_64-pc-windows-msvc.zip", "size": 0}]
    }
    assert len(delivery.release_problems(PIN, incomplete)) == 2


@pytest.mark.parametrize("path", ["pyproject.toml", "uv.lock"])
def test_library_pin_and_resolution_must_match_native(source: Path, path: str) -> None:
    target = source / path
    target.write_text(
        target.read_text(encoding="utf-8").replace(PIN, "b" * 40), encoding="utf-8"
    )
    with pytest.raises(delivery.DeliveryError, match=r"differ|does not resolve"):
        delivery.revision(source)


@pytest.mark.parametrize(
    "changes", [{"draft": True}, {"prerelease": True}, {"target_commitish": "main"}]
)
def test_an_unpublished_or_unidentified_release_cannot_pass(
    changes: dict[str, object],
) -> None:
    assert delivery.release_problems(PIN, published() | changes)


def test_a_merged_unpinned_release_fails_the_scheduled_check(
    source: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    latest = "b" * 40
    replies = [
        published(),
        {"object": {"sha": latest}},
        json.loads(json.dumps(published()).replace(PIN, latest)),
    ]
    monkeypatch.setattr(delivery, "api", lambda endpoint: replies.pop(0))
    assert delivery.main(["--root", str(source), "--latest"]) == 1
    assert "Renovate update is pending" in capsys.readouterr().err
