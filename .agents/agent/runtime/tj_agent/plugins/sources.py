from __future__ import annotations

from pathlib import Path
import re

import plugin_refresh


def tree(source: object, location: Path, project: Path, temporary: Path,
         publishers: dict, modes: dict[str, int] | None = None) -> dict[str, bytes]:
    if isinstance(source, dict) and source.get("source") == "git-subdir":
        publisher = plugin_refresh.repository(source.get("url"))
        if not plugin_refresh.tj_agents(publisher):
            raise plugin_refresh.RefreshError("Cannot verify package from an unknown publisher")
        ref = source.get("sha") or source.get("ref") or "main"
        if not isinstance(ref, str) or not ref or ref.startswith("-"):
            raise plugin_refresh.RefreshError("Invalid package source revision")
        key = (publisher, ref)
        if key not in publishers:
            checkout = temporary / str(len(publishers))
            plugin_refresh.run(["git", "init", str(checkout)], project)
            plugin_refresh.run(["git", "-C", str(checkout), "fetch", "--depth=1",
                                f"https://github.com/{publisher}.git", ref], project)
            plugin_refresh.run(["git", "-C", str(checkout), "checkout", "--detach", "FETCH_HEAD"], project)
            publishers[key] = checkout
        location = publishers[key]
        source = source.get("path")
    elif isinstance(source, dict) and source.get("source") == "local":
        source = source.get("path")
    if not isinstance(source, str) or not source or Path(source).is_absolute():
        raise plugin_refresh.RefreshError("Cannot verify catalog package source")
    root = (location / source).resolve()
    if not root.is_relative_to(location.resolve()):
        raise plugin_refresh.RefreshError("Catalog package escapes its publisher")
    files = plugin_refresh.package_tree(root)
    if modes is not None:
        modes.update({name: (root / name).stat().st_mode & 0o777 for name in files})
    return files


def resolved_head(source: object, publishers: dict) -> str | None:
    if not isinstance(source, dict) or source.get("source") != "git-subdir":
        return None
    key = (plugin_refresh.repository(source.get("url")), source.get("sha") or source.get("ref") or "main")
    checkout = publishers.get(key)
    if checkout is None:
        return None
    head = (checkout / ".git/HEAD").read_text(encoding="ascii").strip()
    if not re.fullmatch(r"[0-9a-f]{40}", head):
        raise plugin_refresh.RefreshError("Fetched publisher is not at a detached commit")
    return head
