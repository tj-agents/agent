"""Filesystem-only resolution of repository configuration files."""

from pathlib import Path


def repository_file(repository: Path, relative: str | Path) -> Path:
    """Return a local configuration file or its linked worktree's primary file.

    A local path wins even when it is a broken symlink. Linked-worktree metadata
    is trusted only after its backlink and common Git directory agree.
    """
    relative_path = Path(relative)
    if relative_path.anchor or ".." in relative_path.parts:
        raise ValueError("relative must not be absolute or escape the repository")

    local = repository / relative_path
    if _lexists(local):
        return local

    primary = _primary_repository(repository)
    if primary is None:
        return local

    primary_file = primary / relative_path
    return primary_file if _lexists(primary_file) else local


def _primary_repository(repository: Path) -> Path | None:
    local_git = repository / ".git"
    gitdir_value = _gitdir_value(local_git)
    if gitdir_value is None:
        return None
    try:
        gitdir = _metadata_path(local_git.parent, gitdir_value).resolve()
    except (OSError, RuntimeError):
        return None
    if not gitdir.is_dir():
        return None

    backlink = _metadata_value(gitdir / "gitdir")
    if backlink is None or not _same_path(_metadata_path(gitdir, backlink), local_git):
        return None

    common_value = _metadata_value(gitdir / "commondir")
    if common_value is None:
        return None
    try:
        common = _metadata_path(gitdir, common_value).resolve()
    except (OSError, RuntimeError):
        return None
    if not _is_primary_git_directory(common):
        return None
    return common.parent


def _gitdir_value(path: Path) -> str | None:
    value = _metadata_value(path)
    if value is None or not value.startswith("gitdir: "):
        return None
    return value.removeprefix("gitdir: ") or None


def _metadata_value(path: Path) -> str | None:
    try:
        value = path.read_text(encoding="utf-8").rstrip("\r\n")
    except (OSError, UnicodeError):
        return None
    return value if value and "\n" not in value and "\r" not in value else None


def _metadata_path(directory: Path, value: str) -> Path:
    path = Path(value)
    return path if path.anchor else directory / path


def _same_path(left: Path, right: Path) -> bool:
    try:
        return left.resolve() == right.resolve()
    except (OSError, RuntimeError):
        return False


def _is_primary_git_directory(path: Path) -> bool:
    return (
        path.name == ".git"
        and path.is_dir()
        and (path / "HEAD").is_file()
        and (path / "objects").is_dir()
    )


def _lexists(path: Path) -> bool:
    return path.exists() or path.is_symlink()
