import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import agent_store
from tj_cli import process

from tj_agent.host import Host


class OwnershipError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class Owner:
    host: Host
    native_id: str
    launch_id: str | None

    def __post_init__(self) -> None:
        if not self.native_id.strip() or self.launch_id == "":
            raise OwnershipError("A worktree owner requires an exact session identity")

    @property
    def session(self) -> str:
        return f"{self.host}:{self.native_id}"

    def arguments(self) -> list[str]:
        arguments = ["--session", self.session]
        if self.launch_id is not None:
            arguments += ["--launch-id", self.launch_id]
        return arguments


@dataclass(frozen=True, slots=True)
class Ownership:
    path: str
    owner: Owner
    claimed_at: str

    def document(self) -> dict[str, str | None]:
        return {
            "path": self.path,
            "session": self.owner.session,
            "launch_id": self.owner.launch_id,
            "claimed_at": self.claimed_at,
        }


def _record(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, dict):
        raise OwnershipError(f"agent-store returned an invalid {label}")
    return cast(Mapping[str, object], value)


def _owner(session: Mapping[str, object]) -> Owner:
    try:
        host = Host(session.get("host"))
    except ValueError as error:
        raise OwnershipError("agent-store returned an invalid session host") from error
    native_id = session.get("native_id")
    launch_id = session.get("launch_id")
    if not isinstance(native_id, str) or not (
        launch_id is None or isinstance(launch_id, str)
    ):
        raise OwnershipError("agent-store returned an invalid session identity")
    owner = Owner(host, native_id, launch_id)
    if session.get("session") != owner.session:
        raise OwnershipError("agent-store session identity does not match its handle")
    return owner


def _validate_launch(owner: Owner, launch: Mapping[str, object]) -> None:
    if (
        launch.get("launch_id") != owner.launch_id
        or launch.get("host") != str(owner.host)
        or launch.get("session") != owner.session
    ):
        raise OwnershipError("agent-store launch and session bindings do not match")


def current_owner(environ: Mapping[str, str]) -> Owner | None:
    identities = [
        (host, native_id)
        for host in Host
        if (native_id := environ.get(host.session_variable, "").strip())
    ]
    if len(identities) > 1:
        raise OwnershipError("Multiple native session identities are present")
    launch_id = environ.get("TJ_RUN_LAUNCH_ID", "").strip()
    if launch_id:
        data = agent_store.run("show", "--launch-id", launch_id)
        launch = _record(data.get("launch"), "launch")
        session = _record(data.get("session"), "bound session")
        owner = _owner(session)
        if owner.launch_id != launch_id:
            raise OwnershipError("The calling launch does not own its bound session")
        _validate_launch(owner, launch)
        if identities and identities[0] != (owner.host, owner.native_id):
            raise OwnershipError("The native session does not match the calling launch")
        return owner
    if not identities:
        if environ.get("TJ_AGENT") == "1" or any(
            environ.get(host.context_variable) for host in Host
        ):
            raise OwnershipError("The agent context has no native session identity")
        return None
    host, native_id = identities[0]
    expected = f"{host}:{native_id}"
    data = agent_store.run("show", expected)
    owner = _owner(_record(data.get("session"), "session"))
    if owner.session != expected:
        raise OwnershipError("agent-store returned a different calling session")
    if owner.launch_id is not None:
        bound = agent_store.run("show", "--launch-id", owner.launch_id)
        _validate_launch(owner, _record(bound.get("launch"), "launch"))
        if _owner(_record(bound.get("session"), "bound session")) != owner:
            raise OwnershipError("agent-store returned a different bound session")
    return owner


def _git(path: Path, *arguments: str) -> str:
    try:
        result: subprocess.CompletedProcess[str] = process.run(
            ["git", "-C", str(path), *arguments],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise OwnershipError(f"Cannot inspect worktree {path}: {error}") from error
    if result.returncode:
        raise OwnershipError(f"Cannot inspect worktree {path}: {result.stderr.strip()}")
    return result.stdout.strip()


def linked_root(path: Path) -> Path | None:
    root = Path(_git(path, "rev-parse", "--show-toplevel")).resolve(strict=True)
    git_dir = Path(_git(root, "rev-parse", "--absolute-git-dir")).resolve(strict=True)
    common = Path(
        _git(root, "rev-parse", "--path-format=absolute", "--git-common-dir")
    ).resolve(strict=True)
    if git_dir == common:
        return None
    entries = _git(root, "worktree", "list", "--porcelain", "-z").split("\0")
    if not any(
        entry.startswith("worktree ") and Path(entry[9:]).resolve() == root
        for entry in entries
    ):
        raise OwnershipError(f"Git does not register linked worktree {root}")
    return root


def claim(path: Path, owner: Owner) -> Ownership:
    requested = path.resolve(strict=True)
    root = linked_root(requested)
    if root is None or root != requested:
        raise OwnershipError(
            f"Ownership requires an exact linked worktree root: {path}"
        )
    result = agent_store.run("claim-worktree", *owner.arguments(), "--path", str(root))
    record = _record(result.get("worktree"), "worktree ownership")
    stored_path = record.get("path")
    claimed_at = record.get("claimed_at")
    if (
        not isinstance(stored_path, str)
        or not Path(stored_path).is_absolute()
        or not Path(stored_path).samefile(root)
        or not isinstance(claimed_at, str)
        or not claimed_at
        or record.get("session") != owner.session
        or record.get("launch_id") != owner.launch_id
    ):
        raise OwnershipError("agent-store returned a different worktree ownership")
    return Ownership(stored_path, owner, claimed_at)


def claim_started_worktree(host: Host, native_id: str, cwd: Path) -> Ownership | None:
    root = linked_root(cwd)
    if root is None:
        return None
    owner = current_owner({host.session_variable: native_id})
    if owner is None:
        raise OwnershipError("The starting session has no ownership identity")
    return claim(root, owner)
