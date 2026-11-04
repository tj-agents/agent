"""Verify library delivery outside the development interpreter and producer tree."""

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PROBE = ROOT / "tests/fixtures/runtime_import_probe.py"


@pytest.mark.parametrize("kind", ["plugins", "store", "peer"])
def test_missing_local_runtime_cannot_select_an_enclosing_owner(
    tmp_path: Path, kind: str
) -> None:
    outer, inner = tmp_path / "outer", tmp_path / "outer/inner"
    for owner in (outer, inner):
        marker = owner / ".agents/plugins/payloads.json"
        marker.parent.mkdir(parents=True)
        marker.write_text("{}", encoding="utf-8")
    package = "agent-store" if kind == "store" else "agent"
    runtime = outer / "plugins" / package / ".agents" / package / "runtime"
    shutil.copytree(
        ROOT / "plugins" / package / ".agents" / package / "runtime/tj_cli",
        runtime / "tj_cli",
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    if kind == "store":
        relative = ".agents/agent-store/runtime/checkout.py"
        shutil.copytree(
            ROOT / ".agents/agent-store/runtime", inner / ".agents/agent-store/runtime"
        )
    else:
        relative = (
            ".agents/agent/utility/plugins/scripts/plugin_runtime.py"
            if kind == "plugins"
            else ".agents/agent/utility/peer/scripts/register_session.py"
        )
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
        ".agents/agent/utility/peer/scripts/session_close.py",
        ".codex/skills/agent-peer/scripts/session_close.py",
        ".claude/skills/agent-peer/scripts/session_close.py",
        "plugins/agent/.agents/agent/utility/peer/scripts/session_close.py",
    ],
)
def test_observer_stages_delivered_cli_independently_of_store_source(
    tmp_path: Path, relative: str
) -> None:
    state = tmp_path / "observer state"
    command = [getattr(sys, "_base_executable", sys.executable), "-I", "-B", str(PROBE)]
    result = subprocess.run(
        [*command, str(ROOT / relative), str(state)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=20,
        check=True,
    )
    staging = Path(json.loads(result.stdout)["staging"])
    relocated = subprocess.run(
        [*command, str(staging / "checkout.py")],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=20,
        check=True,
    )
    assert Path(json.loads(relocated.stdout)["runtime"]).is_relative_to(staging)


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
