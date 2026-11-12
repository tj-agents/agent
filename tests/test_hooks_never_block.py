from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MANIFESTS = (ROOT / ".claude/hooks/agent.json", ROOT / ".codex/hooks/agent.json")
SCRIPT = re.compile(r"\.agents/agent/runtime/[\w/]+\.py")
BLOCKING = (
    re.compile(r"""["']decision["']"""),
    re.compile(r"""["']continue["']\s*:\s*False"""),
    re.compile(r"\b(?:sys\.)?exit\(\s*2\s*\)|\breturn\s+2\b"),
)


def registered_scripts() -> set[Path]:
    scripts: set[Path] = set()
    for manifest in MANIFESTS:
        for groups in json.loads(manifest.read_text(encoding="utf-8"))[
            "hooks"
        ].values():
            for group in groups:
                for hook in group["hooks"]:
                    text = " ".join([hook["command"], *hook.get("args", [])])
                    scripts.update(
                        ROOT
                        / match.replace(
                            ".agents/agent/runtime/claude/", ".claude/runtime/"
                        )
                        for match in SCRIPT.findall(text)
                    )
    return scripts


def test_every_registered_hook_script_is_checked() -> None:
    assert {path.name for path in registered_scripts()} == {
        "session_hook.py",
    }


@pytest.mark.parametrize(
    "script", sorted(registered_scripts()), ids=lambda path: path.name
)
def test_no_registered_hook_can_block(script: Path) -> None:
    source = script.read_text(encoding="utf-8")
    assert [pattern.pattern for pattern in BLOCKING if pattern.search(source)] == []
