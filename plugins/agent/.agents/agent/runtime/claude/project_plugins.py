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
    if scope == "user":
        return True
    if scope not in {"project", "local"} or not isinstance(owner, str) or not owner:
        return False
    common = repository(directory)
    owner_common = repository(Path(owner))
    if common is not None and owner_common is not None:
        return common == owner_common
    return directory.resolve().is_relative_to(Path(owner).resolve())
