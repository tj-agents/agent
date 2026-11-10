from collections.abc import Sequence
from pathlib import Path

import required_plugins
import standards
from codex import user_plugins
from tj_cli import process


def prepare_instructions(project: Path) -> None:
    override = project / "AGENTS.override.md"
    if override.is_file() and override.read_text(encoding="utf-8").strip():
        raise standards.StandardsError(
            f"{override} shadows the repository standards index"
        )


def install(project: Path, identities: Sequence[str]) -> None:
    try:
        for identity in identities:
            result = process.run(
                ["codex", "plugin", "add", identity, "--json"],
                cwd=project,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=120,
                check=False,
            )
            if result.returncode:
                detail = result.stderr.strip() or result.stdout.strip()
                raise required_plugins.RequiredPluginsError(
                    f"Cannot install {identity} for {project}: {detail}"
                )
    finally:
        path = user_plugins.path()
        if path.is_file():
            original = path.read_text(encoding="utf-8")
            updated = user_plugins.without(original, identities)
            if updated != original:
                path.write_text(updated, encoding="utf-8")
