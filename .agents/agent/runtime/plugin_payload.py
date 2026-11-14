"""Check out a plugin's git-subdir source at its pinned commit, as native installers do.

Claude and Codex both materialise a `git-subdir` marketplace source by a sparse git
checkout of the pinned commit. `git archive` applies the same attributes and line-ending
conversion from the same git configuration, so the exported tree matches what either
host would cache.
"""

from __future__ import annotations

import io
import re
import subprocess
import tarfile
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from functools import partial
from pathlib import Path, PurePosixPath

from tj_cli import process

GIT_TIMEOUT_SECONDS = 120
CONCURRENT_REPOSITORIES = 4
REVISION = re.compile(r"[0-9a-f]{40}")


class PayloadError(Exception):
    """A pinned plugin payload could not be checked out."""


@dataclass(frozen=True, slots=True)
class Source:
    """A marketplace `git-subdir` source pinned to a full commit."""

    url: str
    path: str
    revision: str

    @classmethod
    def parse(cls, source: object) -> Source | None:
        """The pinned git-subdir source an entry declares; None for any other source."""
        if not isinstance(source, Mapping) or source.get("source") != "git-subdir":
            return None
        url, path, revision = source.get("url"), source.get("path"), source.get("sha")
        if (
            not isinstance(url, str)
            or not url
            or not isinstance(path, str)
            or not isinstance(revision, str)
            or REVISION.fullmatch(revision) is None
        ):
            return None
        relative = PurePosixPath(path)
        if relative.is_absolute() or ".." in relative.parts or not relative.parts:
            return None
        return cls(url, relative.as_posix(), revision)


@dataclass(frozen=True, slots=True)
class Export:
    """Checked-out payload roots, and why each other requested payload is missing."""

    payloads: Mapping[str, Path]
    problems: Mapping[str, str]


def export(sources: Mapping[str, Source], staging: Path) -> Export:
    """Check out each source under `staging/payloads/<name>`, fetching once per URL."""
    repositories: dict[str, dict[str, Source]] = {}
    for name, source in sources.items():
        repositories.setdefault(source.url, {})[name] = source
    payloads: dict[str, Path] = {}
    problems: dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=CONCURRENT_REPOSITORIES) as pool:
        for exported in pool.map(
            partial(_repository, staging), enumerate(repositories.values())
        ):
            payloads.update(exported.payloads)
            problems.update(exported.problems)
    return Export(payloads, problems)


def _repository(staging: Path, indexed: tuple[int, Mapping[str, Source]]) -> Export:
    index, named = indexed
    url = next(iter(named.values())).url
    repository = staging / f"repository-{index}.git"
    revisions = sorted({source.revision for source in named.values()})
    try:
        _git(None, "init", "--quiet", "--bare", str(repository))
        _git(repository, "fetch", "--quiet", "--depth=1", "--no-tags", url, *revisions)
        for revision in revisions:
            fetched = _git(
                repository, "rev-parse", "--verify", f"{revision}^{{commit}}"
            )
            if fetched.strip() != revision:
                raise PayloadError(f"{url} did not provide commit {revision}")
    except PayloadError as error:
        return Export({}, dict.fromkeys(named, str(error)))
    payloads: dict[str, Path] = {}
    problems: dict[str, str] = {}
    for name, source in named.items():
        try:
            payloads[name] = _extract(repository, source, staging / "payloads" / name)
        except PayloadError as error:
            problems[name] = str(error)
    return Export(payloads, problems)


def _extract(repository: Path, source: Source, destination: Path) -> Path:
    archive = _archive(repository, source)
    prefix = f"{source.path}/"
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        members = []
        for member in tar.getmembers():
            if not member.name.startswith(prefix):
                continue
            if not member.isfile() and not member.isdir():
                raise PayloadError(
                    f"{source.path} at {source.revision} holds {member.name}, "
                    "which is neither a file nor a directory"
                )
            member.name = member.name.removeprefix(prefix)
            members.append(member)
        if not any(member.isfile() for member in members):
            raise PayloadError(f"{source.path} is empty at {source.revision}")
        destination.mkdir(parents=True)
        tar.extractall(destination, members=members, filter="data")
    return destination


def _git(repository: Path | None, *arguments: str) -> str:
    command = ["git", *(["-C", str(repository)] if repository else []), *arguments]
    try:
        result = process.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=GIT_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise PayloadError(f"git {arguments[0]} failed: {error}") from error
    if result.returncode:
        raise PayloadError(_failure(arguments[0], result.returncode, result.stderr))
    return str(result.stdout)


def _archive(repository: Path, source: Source) -> bytes:
    command = ["git", "-C", str(repository), "archive", "--format=tar"]
    try:
        result = process.run(
            [*command, source.revision, "--", source.path],
            capture_output=True,
            timeout=GIT_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise PayloadError(f"git archive failed: {error}") from error
    if result.returncode or not isinstance(result.stdout, bytes):
        detail = result.stderr if isinstance(result.stderr, bytes) else b""
        raise PayloadError(
            _failure("archive", result.returncode, detail.decode("utf-8", "replace"))
        )
    return result.stdout


def _failure(step: str, returncode: int, stderr: str) -> str:
    lines = stderr.strip().splitlines()
    return f"git {step} failed: {lines[-1] if lines else f'exit {returncode}'}"
