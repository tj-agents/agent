"""Verify library delivery outside the development interpreter and producer tree."""

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PROBE = ROOT / "tests/fixtures/runtime_import_probe.py"


@pytest.mark.parametrize(
    "relative",
    [
        ".agents/agent/utility/plugins/scripts/machine_paths.py",
        ".codex/skills/agent-plugins/scripts/machine_paths.py",
        ".claude/skills/agent-plugins/scripts/machine_paths.py",
        ".agents/agent-store/runtime/checkout.py",
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


def test_relocated_standalone_store_uses_its_own_process_library(
    tmp_path: Path,
) -> None:
    runtime = tmp_path / "standalone store"
    shutil.copytree(ROOT / "plugins/agent-store/.agents/agent-store/runtime", runtime)
    result = subprocess.run(
        [
            getattr(sys, "_base_executable", sys.executable),
            "-I",
            "-B",
            str(PROBE),
            str(runtime / "checkout.py"),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=20,
        check=True,
    )
    assert Path(json.loads(result.stdout)["runtime"]).is_relative_to(runtime)
