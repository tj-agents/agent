#!/usr/bin/env python3
"""Update one host's tj-agents plugins to their marketplace pins for tj install."""

from __future__ import annotations

import re
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Protocol

sys.path.insert(0, str(Path(__file__).resolve().parent))

import marketplace_head
import required_plugins
import standards
from claude import installed as claude_installed
from codex import catalog as codex_catalog
from codex import installed as codex_installed
from tj_agent.host import Host
from tj_cli import process
from tj_cli.git import repository_file
from tree_digest import Digests

MARKETPLACES = ("tj-agents", "agents")
TIMEOUT_SECONDS = 600
CONCURRENT_STEPS = 4
DIAGNOSTIC_LINES = 8
REVISION = re.compile(r"[0-9a-f]{40}")


class Report:
    def __init__(self) -> None:
        self.failures: list[str] = []

    @staticmethod
    def run(command: list[str]) -> subprocess.CompletedProcess[str]:
        return process.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=TIMEOUT_SECONDS,
            check=False,
        )

    def step(
        self, host: Host, label: str, command: list[str]
    ) -> subprocess.CompletedProcess[str] | None:
        try:
            result = self.run(command)
        except (OSError, subprocess.TimeoutExpired) as error:
            self.fail(host, label, str(error))
            return None
        if result.returncode:
            detail = (result.stderr.strip() or result.stdout.strip()).splitlines()
            self.fail(
                host,
                label,
                "\n".join(detail[-DIAGNOSTIC_LINES:])
                if detail
                else f"exit {result.returncode}",
            )
            return None
        print(f"{host}: {label} ok")
        return result

    def steps(
        self, host: Host, groups: Sequence[Sequence[tuple[str, list[str]]]]
    ) -> None:
        with ThreadPoolExecutor(max_workers=CONCURRENT_STEPS) as pool:
            list(pool.map(partial(self.sequence, host), groups))

    def sequence(self, host: Host, commands: Sequence[tuple[str, list[str]]]) -> None:
        for label, command in commands:
            self.step(host, label, command)

    def fail(self, host: Host, label: str, detail: str) -> None:
        self.failures.append(f"{host}: {label}: {detail}")
        print(f"{host}: {label} FAILED: {detail}", file=sys.stderr)


class Installed(Protocol):
    RELOAD: str

    def update_marketplaces(
        self, report: Report, marketplaces: tuple[str, ...]
    ) -> None: ...

    def current(self, marketplace: str, plugin: str, digests: Digests) -> bool: ...

    def refresh(
        self,
        report: Report,
        marketplaces: tuple[str, ...],
        current: Callable[[str, str], bool],
    ) -> None: ...

    def activate(self, report: Report) -> None: ...

    def marketplace_root(self, marketplace: str) -> Path: ...

    def refresh_identities(self, marketplaces: tuple[str, ...]) -> set[str]: ...


INSTALLED: Mapping[Host, Installed] = {
    Host.CLAUDE: claude_installed,
    Host.CODEX: codex_installed,
}


def current(host: Host, digests: Digests, marketplace: str, plugin: str) -> bool:
    return INSTALLED[host].current(marketplace, plugin, digests)


def already_current(host: Host, digests: Digests) -> bool:
    identities = {
        identity
        for identity in INSTALLED[host].refresh_identities(MARKETPLACES)
        if identity.rpartition("@")[2] in MARKETPLACES
    }
    return bool(identities) and all(
        current(host, digests, identity.rpartition("@")[2], identity.rpartition("@")[0])
        for identity in sorted(identities)
    )


def prefetch_standards(host: Host) -> list[object] | None:
    directory = Path.cwd()
    project = required_plugins.project_root(directory)
    if repository_file(project, ".agents/repo-capabilities.local.json").is_file():
        return None
    required = required_plugins.selected(project)
    if not required:
        return None
    return list(required_plugins.inventory_rows(directory, host, identities=required))


def update_standards(
    report: Report, host: Host, prefetched: Future[list[object] | None] | None = None
) -> None:
    directory = Path.cwd()
    project = required_plugins.project_root(directory)
    if repository_file(project, ".agents/repo-capabilities.local.json").is_file():
        return
    try:
        entries = prefetched.result() if prefetched is not None else None
        roots = (
            required_plugins.check(directory, host, entries=entries)
            if entries is not None
            else required_plugins.check(directory, host)
        )
        if roots is not None and standards.update(project, roots, check=True):
            print(
                f"{host}: left {project} unchanged; from that checkout run "
                f"tj prepare --host {host.value} --write "
                "to update repository standards",
                file=sys.stderr,
            )
    except (
        OSError,
        ValueError,
        required_plugins.RequiredPluginsError,
        standards.StandardsError,
    ) as error:
        report.fail(host, "repository standards", str(error))


def marketplace_revision(host: Host) -> str | None:
    root = INSTALLED[host].marketplace_root(required_plugins.MARKETPLACE_NAME)
    try:
        result = Report.run(["git", "-C", str(root), "rev-parse", "HEAD"])
    except (OSError, subprocess.TimeoutExpired):
        return None
    revision = result.stdout.strip()
    return revision if not result.returncode and REVISION.fullmatch(revision) else None


def behind_remote(host: Host, marketplace: str, environ: Mapping[str, str]) -> bool:
    """Whether this host's `marketplace` clone may be behind its remote HEAD."""
    return marketplace_head.behind(
        INSTALLED[host].marketplace_root(marketplace), environ
    )


def agent_package(host: Host, state: Path) -> Path | None:
    package: Path | None
    if host is Host.CLAUDE:
        package = claude_installed.pinned_install("tj-agents", "agent")
        return package
    build = codex_catalog.recorded(state).get("agent@tj-agents")
    if build is None:
        return None
    package = codex_catalog.root("tj-agents", "agent", build.version)
    return package


@dataclass(frozen=True, slots=True)
class Outcome:
    failures: list[str]
    changed: bool
    revision: str | None


def update(
    host: Host,
    digests: Digests,
    environ: Mapping[str, str],
    *,
    converge_runtime: Callable[[], str | None] | None = None,
) -> Outcome:
    """Fetch the marketplaces, then bring this host's plugins to their pins."""
    report = Report()
    installed = INSTALLED[host]
    before = marketplace_revision(host)
    with ThreadPoolExecutor(max_workers=CONCURRENT_STEPS) as pool:
        pins = pool.submit(already_current, host, digests)
        inventory = pool.submit(prefetch_standards, host)
        behind = list(
            pool.map(lambda name: behind_remote(host, name, environ), MARKETPLACES)
        )
        pins_current = pins.result()
    reuse_inventory = pins_current and not any(behind)
    installed.update_marketplaces(
        report,
        tuple(name for name, stale in zip(MARKETPLACES, behind, strict=True) if stale),
    )
    if report.failures:
        if host is Host.CODEX:
            codex_installed.trust_hooks(report)
        return Outcome(report.failures, False, before)
    if any(behind):
        pins_current = already_current(host, digests)
    if pins_current:
        print(f"{host}: plugins already match their pins")
        if host is Host.CODEX:
            codex_installed.trust_hooks(report)
    else:
        installed.refresh(report, MARKETPLACES, partial(current, host, digests))
    digests.save()
    if not report.failures:
        update_standards(report, host, inventory if reuse_inventory else None)
    if (
        not report.failures
        and converge_runtime is not None
        and (problem := converge_runtime())
    ):
        report.fail(host, "native commands and launch runtime", problem)
    revision = marketplace_revision(host)
    # Activation repeats until it succeeds once for the plugins at this revision.
    activated = digests.path.parent / f"activated-{host}.txt"
    try:
        settled = pins_current and activated.read_text(encoding="utf-8") == revision
    except OSError:
        settled = False
    if not report.failures and not settled:
        installed.activate(report)
        if not report.failures and revision is not None:
            activated.parent.mkdir(parents=True, exist_ok=True)
            activated.write_text(revision, encoding="utf-8")
    if revision is None and not report.failures:
        report.fail(
            host, "marketplace revision", "the tj-agents marketplace has no commit"
        )
    return Outcome(report.failures, not pins_current or revision != before, revision)
