"""Find retired plugin references in projects, worktrees and host registrations."""

import os
import subprocess
from collections.abc import Iterable
from pathlib import Path

from claude import plugin_references as claude_references
from codex import plugin_references as codex_references
from tj_cli import process

SKIPPED = frozenset(
    {
        ".git",
        ".venv",
        "node_modules",
        "__pycache__",
        "target",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
    }
)
TEXT = frozenset(
    {".json", ".toml", ".md", ".yaml", ".yml", ".py", ".ps1", ".sh", ".rs"}
)


def worktrees(root: Path) -> set[Path]:
    result = process.run(
        ["git", "-C", str(root), "worktree", "list", "--porcelain", "-z"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=10,
        check=False,
    )
    if result.returncode:
        raise OSError(f"Cannot enumerate worktrees for {root}: {result.stderr.strip()}")
    return {
        Path(row.removeprefix("worktree "))
        for row in result.stdout.split("\0")
        if row.startswith("worktree ")
    }


def scan(
    identities: Iterable[str], roots: Iterable[Path], extra_files: Iterable[Path]
) -> tuple[list[str], list[str]]:
    tokens = set(identities)
    pending = list(roots)
    visited: set[Path] = set()
    files = set(extra_files)
    problems: list[str] = []
    while pending:
        root = pending.pop().resolve()
        if root in visited:
            continue
        visited.add(root)
        if not root.is_dir():
            problems.append(f"Unverified missing project root: {root}")
            continue

        def failed(error: OSError) -> None:
            problems.append(str(error))

        for directory, children, names in os.walk(
            root, followlinks=False, onerror=failed
        ):
            current = Path(directory)
            children[:] = [
                name
                for name in children
                if name not in SKIPPED
                and not (current / name).is_symlink()
                and not (current / name).is_junction()
            ]
            if current != root and current in visited:
                children[:] = []
                continue
            visited.add(current)
            if (current / ".git").exists():
                try:
                    pending.extend(worktrees(current) - visited)
                except (OSError, subprocess.SubprocessError) as error:
                    problems.append(str(error))
            files.update(current / name for name in names if Path(name).suffix in TEXT)
    findings = []
    for path in sorted(files):
        try:
            if path.is_file():
                for number, line in enumerate(
                    path.read_text(encoding="utf-8-sig").splitlines(), 1
                ):
                    for identity in sorted(tokens):
                        if identity in line:
                            findings.append(f"{path}:{number}: {identity}")
        except (OSError, UnicodeError) as error:
            problems.append(f"Unverified file {path}: {error}")
    return findings, problems


def audit(identities: Iterable[str], roots: Iterable[Path]) -> int:
    projects = set(roots)
    files = set()
    problems = []
    for provider in (claude_references, codex_references):
        files.update(provider.files())
        try:
            for project in provider.projects():
                if (
                    not project.exists()
                    or (project / ".git").exists()
                    or (project / ".agents/plugins.json").is_file()
                ):
                    projects.add(project)
                else:
                    problems.append(
                        "Registered non-repository root requires explicit --root: "
                        f"{project}"
                    )
        except (OSError, ValueError) as error:
            problems.append(f"Cannot enumerate {provider.__name__} projects: {error}")
    findings, unreadable = scan(identities, projects, files)
    for line in [*findings, *problems, *unreadable]:
        print(line)
    print(
        f"Plugin reference audit: {len(findings)} references; "
        f"{len(problems) + len(unreadable)} unverified paths"
    )
    return int(bool(findings or problems or unreadable))
