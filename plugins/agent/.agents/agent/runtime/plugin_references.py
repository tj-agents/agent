"""Find retired plugin references in projects, worktrees and host registrations."""

import os
import stat
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
LOCAL_FILES = (
    ".agents/plugins.json",
    ".agents/repo-capabilities.local.json",
    ".claude/settings.json",
    ".claude/settings.local.json",
    ".codex/config.toml",
    "AGENTS.md",
    "CLAUDE.md",
)


def source_files(root: Path) -> set[Path]:
    result = process.run(
        [
            "git",
            "-C",
            str(root),
            "ls-files",
            "--cached",
            "--others",
            "--exclude-standard",
            "-z",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
        check=False,
    )
    if result.returncode:
        raise OSError(
            f"Cannot inventory Git sources for {root}: {result.stderr.strip()}"
        )
    if result.stdout and not result.stdout.endswith("\0"):
        raise ValueError(f"Invalid Git inventory for {root}: missing NUL terminator")
    files = set()
    for name in result.stdout.split("\0")[:-1]:
        relative = Path(name)
        if not name or relative.anchor or ".." in relative.parts or relative == Path():
            raise ValueError(f"Invalid Git inventory for {root}: {name!r}")
        if relative.suffix in TEXT:
            files.add(root / relative)
    return files


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
        requested = pending.pop()
        try:
            root = requested.resolve()
            mode = root.stat().st_mode
        except OSError as error:
            problems.append(f"Unverified project root {requested}: {error}")
            continue
        if root in visited:
            continue
        visited.add(root)
        if not stat.S_ISDIR(mode):
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
                children[:] = []
                files.update(current / name for name in LOCAL_FILES)
                try:
                    pending.extend(worktrees(current) - visited)
                except (OSError, subprocess.SubprocessError) as error:
                    problems.append(str(error))
                try:
                    inventory = source_files(current)
                except (OSError, ValueError, subprocess.SubprocessError) as error:
                    problems.append(f"Unverified Git inventory for {current}: {error}")
                else:
                    files.update(inventory)
                continue
            files.update(current / name for name in names if Path(name).suffix in TEXT)
    findings = []
    for path in sorted(files):
        try:
            if path.is_file():
                with path.open(encoding="utf-8-sig") as stream:
                    for number, line in enumerate(stream, 1):
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
    print(
        "Scope: requested roots, native registrations and linked worktrees; "
        "Git tracked/unignored text plus explicit local settings. "
        "Ignored build files are excluded unless tracked; repository subtrees "
        "are not recursively scanned. Non-repository roots include text files "
        "outside skipped cache directories. This is not a universal disk scan."
    )
    return int(bool(findings or problems or unreadable))
