from collections.abc import Sequence
from pathlib import Path

import required_plugins
import standards
from tj_cli import process


def repository(directory: Path) -> Path:
    directory = directory.resolve()
    for parent in (directory, *directory.parents):
        git = parent / ".git"
        if git.is_dir():
            return git.resolve()
        if git.is_file():
            text = git.read_text(encoding="utf-8").strip()
            if text.startswith("gitdir: "):
                git = (parent / text.removeprefix("gitdir: ")).resolve()
                common = git / "commondir"
                if common.is_file():
                    return (git / common.read_text(encoding="utf-8").strip()).resolve()
                return git
    return directory


def applies(directory: Path, scope: str, owner: str | None) -> bool:
    if scope == "user":
        return True
    if scope not in {"project", "local"} or not owner:
        return False
    return repository(directory) == repository(Path(owner))


def prepare_instructions(project: Path) -> None:
    pointer = project / "CLAUDE.md"
    shared = project / "AGENTS.md"
    for path in (pointer, shared):
        if path.is_symlink() or path.is_junction():
            raise standards.StandardsError(
                f"Refusing redirected instruction file: {path}"
            )
    original = pointer.read_text(encoding="utf-8") if pointer.exists() else ""
    if original.strip() == "@AGENTS.md":
        return
    if original.strip():
        current = shared.read_text(encoding="utf-8") if shared.exists() else ""
        content = original.replace("@AGENTS.md", "").strip()
        shared.write_text(current.rstrip() + "\n\n" + content + "\n", encoding="utf-8")
    pointer.write_text("@AGENTS.md\n", encoding="utf-8")


def install(project: Path, identities: Sequence[str]) -> None:
    local = (project / ".agents/repo-capabilities.local.json").is_file()
    scope = "local" if local else "project"
    for identity in identities:
        result = process.run(
            [
                "claude",
                "plugin",
                "install",
                identity,
                "--scope",
                scope,
                "--json",
                "-y",
            ],
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
