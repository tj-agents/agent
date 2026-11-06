"""Select one capable native executable for an owned Codex handoff."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import agent_cli


def handoff_executable(
    target: str,
    project: Path,
    environment: Mapping[str, str],
    *,
    allow_managed: bool = True,
) -> str:
    prerequisite = (
        "Codex handoff requires an installed native Codex 0.166.0 or newer "
        "for paginated input verification"
    )
    floor = agent_cli.parse_codex_version("0.166.0")

    def probe(path: str) -> tuple[str, dict[str, Any]]:
        candidate = Path(path)
        if not candidate.is_absolute():
            raise ValueError("the runtime path is not absolute")
        candidate = candidate.resolve(strict=True)
        if not candidate.is_file():
            raise ValueError("the runtime path is not a file")
        if os.name == "nt":
            if candidate.suffix.casefold() != ".exe":
                raise ValueError("the runtime path is not a native executable")
        else:
            with candidate.open("rb") as stream:
                if stream.read(2) == b"#!" or not os.access(candidate, os.X_OK):
                    raise ValueError("the runtime path is not a native executable")
        result = invoke(str(candidate), "--version")
        version = agent_cli.parse_codex_version(result)
        if version is None:
            raise ValueError("the runtime did not report a Codex version")
        return str(candidate), version

    def invoke(executable: str, *arguments: str) -> str:
        creationflags = 0
        if sys.platform == "win32":
            creationflags = subprocess.CREATE_NO_WINDOW
        result = subprocess.run(
            [executable, *arguments],
            cwd=project,
            env=dict(environment),
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=15,
            creationflags=creationflags,
        )
        if result.returncode:
            raise ValueError(
                f"native {' '.join(arguments)} failed with exit {result.returncode}"
            )
        return result.stdout.strip()

    try:
        selected, version = probe(target)
        if agent_cli.compare_codex_version(version, floor) >= 0:
            return selected
        if not allow_managed:
            raise ValueError(f"the selected runtime is {version['text']}")
        metadata = json.loads(invoke(selected, "app-server", "daemon", "version"))
        if (
            not isinstance(metadata, dict)
            or metadata.get("status") != "running"
            or metadata.get("cliVersion") != version["text"]
            or not isinstance(metadata.get("managedCodexPath"), str)
            or not isinstance(metadata.get("managedCodexVersion"), str)
        ):
            raise ValueError(
                "read-only daemon version metadata is missing or inconsistent"
            )
        managed, installed = probe(metadata["managedCodexPath"])
        if installed["text"] != metadata["managedCodexVersion"]:
            raise ValueError(
                "the managed runtime version does not match read-only daemon metadata"
            )
        if agent_cli.compare_codex_version(installed, floor) < 0:
            raise ValueError(f"the installed managed runtime is {installed['text']}")
        return managed
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        raise RuntimeError(f"{prerequisite}: {error}") from error
