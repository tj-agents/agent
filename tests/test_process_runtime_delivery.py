"""Verify library delivery outside the development interpreter and producer tree."""

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PROBE = ROOT / "tests/fixtures/runtime_import_probe.py"


def test_missing_local_runtime_cannot_select_an_enclosing_owner(tmp_path: Path) -> None:
    outer, inner = tmp_path / "outer", tmp_path / "outer/inner"
    for owner in (outer, inner):
        marker = owner / ".agents/plugins/payloads.json"
        marker.parent.mkdir(parents=True)
        marker.write_text("{}", encoding="utf-8")
    package = "agent"
    runtime = outer / "plugins" / package / ".agents" / package / "runtime"
    shutil.copytree(
        ROOT / "plugins" / package / ".agents" / package / "runtime/tj_cli",
        runtime / "tj_cli",
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    relative = ".agents/agent/utility/plugins/scripts/plugin_runtime.py"
    target = inner / relative
    target.parent.mkdir(parents=True)
    shutil.copy2(ROOT / relative, target)
    result = subprocess.run(
        [
            getattr(sys, "_base_executable", sys.executable),
            "-I",
            "-B",
            str(PROBE),
            str(inner / relative),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=20,
    )
    assert result.returncode != 0
    expected = inner / "plugins" / package / ".agents" / package / "runtime"
    assert f"process runtime is unavailable at {expected}" in result.stderr


@pytest.mark.parametrize(
    "relative",
    [
        ".agents/agent/utility/plugins/scripts/machine_paths.py",
        ".codex/skills/agent-plugins/scripts/machine_paths.py",
        ".claude/skills/agent-plugins/scripts/machine_paths.py",
    ],
)
def test_source_entry_points_resolve_their_declared_runtime(relative: str) -> None:
    result = subprocess.run(
        [
            getattr(sys, "_base_executable", sys.executable),
            "-I",
            "-B",
            str(PROBE),
            str(ROOT / relative),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=20,
        check=True,
    )
    runtime = Path(json.loads(result.stdout)["runtime"])
    assert runtime.is_relative_to(ROOT / "plugins")
