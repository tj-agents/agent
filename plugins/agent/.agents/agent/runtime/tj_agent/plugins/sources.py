from __future__ import annotations

import importlib.util
from pathlib import Path
import re

from tj_cli import process

IN_USE = ".in_use"
TIMEOUT = 300


class RefreshError(RuntimeError):
    pass


def repository(source: object) -> str | None:
    match = re.fullmatch(r"(?:(?:https?://|ssh://git@|git@)github\.com[/:])?([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)"
                         r"(?:\.git)?/?", source.strip()) if isinstance(source, str) else None
    return f"{match[1]}/{match[2]}".casefold() if match else None


def tj_agents(repository_name: str | None) -> bool:
    return bool(repository_name) and repository_name.split("/", 1)[0] == "tj-agents"


def run(command: list[str], project: Path) -> str:
    result = process.run(command, cwd=project, capture_output=True, text=True,
                         encoding="utf-8", errors="replace", timeout=TIMEOUT)
    if result.returncode:
        detail = (result.stderr or result.stdout).strip() or f"Command exited {result.returncode}"
        raise RefreshError(f"{' '.join(command[1:])}: {detail}")
    return result.stdout


def generated_cache(path: Path) -> bool:
    if not path.is_file() or path.parent.name != "__pycache__" or path.suffix != ".pyc":
        return False
    try:
        importlib.util.source_from_cache(str(path))
    except ValueError:
        return False
    return True


def package_tree(root: Path) -> dict[str, bytes]:
    if root.is_symlink() or getattr(root, "is_junction", lambda: False)():
        raise RefreshError(f"Unexpected plugin link: {root}")
    if not root.is_dir():
        raise RefreshError(f"Missing plugin directory: {root}")
    result = {}
    for path in root.rglob("*"):
        parts = path.relative_to(root).parts
        if path.is_symlink() or getattr(path, "is_junction", lambda: False)():
            raise RefreshError(f"Unexpected plugin symlink: {path}")
        if parts[0] == IN_USE or generated_cache(path):
            continue
        if path.is_file() and ".git" not in parts:
            result[path.relative_to(root).as_posix()] = path.read_bytes()
    return result


def tree(source: object, location: Path, project: Path, temporary: Path,
         publishers: dict, modes: dict[str, int] | None = None) -> dict[str, bytes]:
    if isinstance(source, dict) and source.get("source") == "git-subdir":
        publisher = repository(source.get("url"))
        if not tj_agents(publisher):
            raise RefreshError("Cannot verify package from an unknown publisher")
        ref = source.get("sha") or source.get("ref") or "main"
        if not isinstance(ref, str) or not ref or ref.startswith("-"):
            raise RefreshError("Invalid package source revision")
        key = (publisher, ref)
        if key not in publishers:
            checkout = temporary / str(len(publishers))
            run(["git", "init", str(checkout)], project)
            run(["git", "-C", str(checkout), "fetch", "--depth=1",
                f"https://github.com/{publisher}.git", ref], project)
            run(["git", "--no-replace-objects", "-C", str(checkout), "checkout", "--detach", "FETCH_HEAD"], project)
            publishers[key] = checkout
        location = publishers[key]
        source = source.get("path")
    elif isinstance(source, dict) and source.get("source") == "local":
        source = source.get("path")
    if not isinstance(source, str) or not source or Path(source).is_absolute():
        raise RefreshError("Cannot verify catalog package source")
    root = (location / source).resolve()
    if not root.is_relative_to(location.resolve()):
        raise RefreshError("Catalog package escapes its publisher")
    files = package_tree(root)
    if modes is not None:
        modes.update({name: (root / name).stat().st_mode & 0o777 for name in files})
    return files


def resolved_head(source: object, publishers: dict) -> str | None:
    if not isinstance(source, dict) or source.get("source") != "git-subdir":
        return None
    key = (repository(source.get("url")), source.get("sha") or source.get("ref") or "main")
    checkout = publishers.get(key)
    if checkout is None:
        return None
    head = (checkout / ".git/HEAD").read_text(encoding="ascii").strip()
    if not re.fullmatch(r"[0-9a-f]{40}", head):
        raise RefreshError("Fetched publisher is not at a detached commit")
    return head
