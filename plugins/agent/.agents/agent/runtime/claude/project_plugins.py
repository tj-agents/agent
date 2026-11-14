from collections.abc import Callable
from pathlib import Path


def repository(directory: Path) -> Path | None:
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
    return None


def applies(directory: Path, scope: str, owner: str | None) -> bool:
    return matcher(directory)(scope, owner)


def matcher(directory: Path) -> Callable[[str, str | None], bool]:
    """`applies` for one directory, resolving each owner's repository once."""
    common = repository(directory)
    owners: dict[str, Path | None] = {}

    def applies_here(scope: str, owner: str | None) -> bool:
        if scope == "user":
            return True
        if scope not in {"project", "local"} or not isinstance(owner, str) or not owner:
            return False
        if owner not in owners:
            owners[owner] = repository(Path(owner))
        if common is not None and owners[owner] is not None:
            return common == owners[owner]
        return directory.resolve().is_relative_to(Path(owner).resolve())

    return applies_here
