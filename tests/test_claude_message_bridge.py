"""Exercise the authored adapter in Claude's isolated Mods test engine."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MODULE = ".agents/agent/runtime/claude/message_bridge.mjs"


def test_native_message_module_is_required() -> None:
    harness = json.loads(
        (ROOT / ".agents/plugins/manifests/harness/agent.json").read_text(
            encoding="utf-8"
        )
    )
    hooks = json.loads((ROOT / ".claude/hooks/agent.json").read_text(encoding="utf-8"))
    assert {"path": MODULE, "hosts": ["claude"]} in harness["requires"]["hooks"]
    assert "../" + MODULE in hooks["modules"]


def test_shipped_native_message_module() -> None:
    """Run after the owning parent refreshes the generated plugin payload."""
    package = ROOT / "plugins/agent"
    harness = json.loads((package / "harness.json").read_text(encoding="utf-8"))
    hooks = json.loads((package / "hooks/claude.json").read_text(encoding="utf-8"))
    assert {"path": MODULE, "hosts": ["claude"]} in harness["requires"]["hooks"]
    assert "../" + MODULE in hooks["modules"]
    assert (package / MODULE).read_bytes() == (
        ROOT / ".claude/runtime/message_bridge.mjs"
    ).read_bytes()


def test_native_message_adapter(tmp_path: Path) -> None:
    claude = shutil.which("claude")
    if claude is None:
        pytest.skip("Claude's isolated plugin test engine is not installed")
    metadata = tmp_path / ".claude-plugin/plugin.json"
    metadata.parent.mkdir()
    metadata.write_text(
        json.dumps({"name": "tj-bridge-fixture", "version": "0.0.0"}), encoding="utf-8"
    )
    hooks = tmp_path / "hooks/hooks.json"
    hooks.parent.mkdir()
    hooks.write_text(json.dumps({"modules": ["../" + MODULE]}), encoding="utf-8")
    module = tmp_path / MODULE
    module.parent.mkdir(parents=True)
    shutil.copyfile(ROOT / ".claude/runtime/message_bridge.mjs", module)
    shutil.copyfile(
        ROOT / "tests/fixtures/claude_message_bridge.test.ts",
        tmp_path / "bridge.test.ts",
    )
    result = subprocess.run(
        [claude, "plugin", "test", str(tmp_path)],
        capture_output=True,
        text=True,
        timeout=90,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    print(result.stdout)
