"""Plugin reconciliation must not allocate consoles from a background worker."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "plugins/agent/.agents/agent/utility/plugins/scripts"
PROBE = ROOT / "tests/fixtures/plugin_console_probe.py"


@pytest.mark.parametrize(
    "operation",
    ["selection", "clone", "snapshot", "tracked-settings", "conflicted-settings"],
)
def test_background_plugin_child_stays_hidden(tmp_path: Path, operation: str) -> None:
    result = subprocess.run(
        [
            getattr(sys, "_base_executable", sys.executable),
            str(PROBE),
            str(SCRIPTS),
            operation,
            str(tmp_path),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=20,
        creationflags=getattr(subprocess, "DETACHED_PROCESS", 0),
    )
    assert result.returncode == 0, result.stderr
    observed = json.loads(result.stdout)
    assert not observed["parent_console"]
    assert not observed["child_visible"], f"{operation} showed a console"
