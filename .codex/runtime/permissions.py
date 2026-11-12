"""Codex's native execpolicy rules that Agent installs (Python 3.9+)."""

from __future__ import annotations

import string
from pathlib import Path
from typing import cast

from launch_errors import LaunchError
from tj_agent import worktrees

RULES = "tj-agents-credentials.rules"
TEXT = """prefix_rule(
    pattern = [["gh", "gh.exe"], "auth", ["login", "refresh"]],
    decision = "forbidden",
    justification = "Account-wide OAuth scopes reach every organization; ask the human for a fine-grained token for the one call instead.",
    match = [["gh", "auth", "refresh", "-s", "admin:org"], ["gh", "auth", "login"]],
    not_match = [["gh", "auth", "status"], ["gh", "auth", "token"]],
)
"""


def rules_path(config: Path) -> Path:
    return config.parent / "rules" / RULES


def ascii_lower(value: str) -> str:
    return value.translate(
        str.maketrans(string.ascii_uppercase, string.ascii_lowercase)
    )


def project_settings(
    projects: dict[str, object], path: Path, *, windows: bool, wsl: bool
) -> dict[str, object] | None:
    canonical = str(path.resolve(strict=True))
    if windows and not canonical.startswith("\\\\?\\"):
        canonical = (
            "\\\\?\\UNC\\" + canonical[2:]
            if canonical.startswith("\\\\")
            else "\\\\?\\" + canonical
        )
    if (
        wsl
        and len(path.parts) > 2
        and ascii_lower(path.parts[1]) == "mnt"
        and len(path.parts[2]) == 1
    ):
        canonical = ascii_lower(canonical)
    for spelling in dict.fromkeys((canonical, str(path))):
        key = ascii_lower(spelling) if windows else spelling
        matches = [
            value
            for candidate, value in projects.items()
            if (ascii_lower(candidate) if windows else candidate) == key
        ]
        if len(matches) > 1:
            raise LaunchError(f"Codex project trust is ambiguous for {path}")
        if matches:
            if not isinstance(matches[0], dict):
                raise LaunchError(f"Codex project trust is invalid for {path}")
            return cast(dict[str, object], matches[0])
    return None


def pointer(path: Path, prefix: str = "") -> Path:
    if (
        path.is_symlink()
        or path.is_junction()
        or not path.is_file()
        or path.stat().st_size > 65536
    ):
        raise LaunchError(f"Cannot validate Codex Git trust identity: {path}")
    text = path.read_text(encoding="utf-8").strip()
    if not text.startswith(prefix) or not text[len(prefix) :].strip():
        raise LaunchError(f"Cannot validate Codex Git trust pointer: {path}")
    return path.parent / text[len(prefix) :].strip()


def main_repository(directory: Path) -> Path:
    root = cast(Path, worktrees.repository_root(directory))
    canonical_root = root.resolve(strict=True)
    nearest = next(
        (path for path in (directory, *directory.parents) if (path / ".git").exists()),
        None,
    )
    if (
        nearest is None
        or str(nearest.resolve(strict=True)) != str(canonical_root)
        or directory.resolve(strict=True).parts[: len(canonical_root.parts)]
        != canonical_root.parts
    ):
        raise LaunchError(f"Codex Git trust root does not contain {directory}")
    marker = root / ".git"
    if (
        marker.is_dir()
        and not marker.is_symlink()
        and not marker.is_junction()
        and (marker / "HEAD").is_file()
    ):
        return root
    git_dir = pointer(marker, "gitdir:").absolute()
    if (
        git_dir.is_symlink()
        or git_dir.is_junction()
        or not git_dir.is_dir()
        or git_dir.parent.name != "worktrees"
    ):
        raise LaunchError(f"Unsupported Codex linked worktree identity: {root}")
    common = git_dir.parent.parent
    main = common.parent
    linked_root = worktrees.linked_root(root)
    if (
        str(pointer(git_dir / "gitdir").resolve(strict=True))
        != str(marker.resolve(strict=True))
        or str(pointer(git_dir / "commondir").resolve(strict=True))
        != str(common.resolve(strict=True))
        or not (main / ".git").is_dir()
        or (main / ".git").is_symlink()
        or (main / ".git").is_junction()
        or str((main / ".git").resolve(strict=True)) != str(common.resolve(strict=True))
        or linked_root is None
        or str(linked_root.resolve(strict=True)) != str(canonical_root)
    ):
        raise LaunchError(f"Codex linked worktree backlinks do not match: {root}")
    return main


def require_project_trust(
    document: dict[str, object], directory: Path, *, windows: bool, wsl: bool = False
) -> None:
    config, layers = document.get("config"), document.get("layers")
    if not isinstance(config, dict) or not isinstance(layers, list):
        raise LaunchError(
            "Codex config/read did not return merged configuration and layers"
        )
    features = config.get("features", {})
    if isinstance(features, dict) and features.get("daemon_auto_start") is False:
        raise LaunchError(
            "Codex shared daemon startup is disabled in the effective configuration"
        )
    projects = config.get("projects")
    if (
        not isinstance(projects, dict)
        or not projects
        or not all(isinstance(key, str) for key in projects)
    ):
        raise LaunchError(f"Codex has no explicit project trust for {directory}")
    selected = project_settings(projects, directory, windows=windows, wsl=wsl)
    if selected is None:
        selected = project_settings(
            projects, main_repository(directory), windows=windows, wsl=wsl
        )
    if selected is None or selected.get("trust_level") != "trusted":
        raise LaunchError(
            f"Codex requires existing explicit project trust for {directory}"
        )
