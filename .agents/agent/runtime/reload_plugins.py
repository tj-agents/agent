#!/usr/bin/env python3
"""Refresh selected tj-agents plugins through native hosts (Python 3.9+)."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parent))

import agent_cli
from host import Host
from host_plugins import PLUGINS, Plugins
import launch_check
import plugin_refresh
from plugin_refresh import RefreshError
import required_plugins
import session_relaunch
from tj_agent.plugins import sources

ADOPTION = "Client adoption is separate from installed package verification; an armed restart resumes this same conversation automatically."


def calling_host() -> Host | None:
    return session_relaunch.caller_host()


def selection(plugins: Plugins, executable: str, project: Path) -> tuple[dict, list[str]]:
    return plugin_refresh.selected(*plugins.selection(executable, project))


def refresh(plugins: Plugins, executable: str, project: Path, marketplaces: dict, identities: list[str]) -> list[str]:
    plugins.refresh_marketplaces(executable, project, marketplaces)
    return plugins.refresh_plugins(executable, project, marketplaces, identities)


def prepare_required(project: Path, host: Host, executable: str | None = None,
                     inventory_options: tuple[str, ...] = (), updated: set[str] | None = None):
    try:
        return required_plugins.check(project, host, executable, inventory_options)
    except required_plugins.MissingRequiredPluginsError as error:
        repair_missing(host, error.project, error.missing, executable)
        roots = required_plugins.check(project, host, executable, inventory_options)
        if updated is not None:
            updated.update(error.missing)
        return roots


def repair_missing(host: Host, project: Path, missing, executable: str | None = None) -> None:
    plugins = PLUGINS[host]
    executable = (plugins.resolve_executable() if not executable
                  or Path(executable).suffix.casefold() in {".cmd", ".ps1", ".bat"} else executable)
    if not executable:
        raise RefreshError(f"Cannot repair required {host} plugins: executable is unavailable")
    marketplaces, enabled = selection(plugins, executable, project)
    undeclared = sorted(set(missing) - set(enabled))
    if undeclared:
        raise RefreshError("Required plugins are not enabled in this host's configuration: "
                           + ", ".join(undeclared) + ". Repair repository selection; no setting was enabled.")
    marketplaces = {name: source for name, source in marketplaces.items()
                    if any(identity.endswith("@" + name) for identity in missing)}
    plugins.refresh_marketplaces(executable, project, marketplaces)
    plugins.install_missing(executable, project, missing)


def verify(plugins: Plugins, executable: str, project: Path, marketplaces: dict, identities: list[str]) -> dict:
    with tempfile.TemporaryDirectory(prefix="tj-agents-verify-") as temporary:
        return verify_packages(plugins, executable, project, marketplaces, identities, Path(temporary))


def verify_packages(plugins: Plugins, executable: str, project: Path, marketplaces: dict,
                    identities: list[str], temporary: Path) -> dict:
    host = plugins.HOST
    print(f"Verifying {host} native installations and marketplace revisions...", file=sys.stderr, flush=True)
    inventory = json.loads(plugin_refresh.run([executable, "plugin", "list", "--json"], project))
    entries = plugins.installations(project, plugins.inventory_entries(inventory) or [])
    installed = {entry.get("id", entry.get("pluginId")) for entry in entries if entry.get("enabled") is True}
    problems = [f"Missing enabled native installation: {identity}" for identity in identities if identity not in installed]
    problems += plugins.entry_errors(entries, project)
    revisions = {}
    publishers = {}
    for name, repo in marketplaces.items():
        print(f"Verifying {name} against {repo} main...", file=sys.stderr, flush=True)
        remote = plugin_refresh.run(["git", "ls-remote", f"https://github.com/{repo}.git", "refs/heads/main"], project).split()
        if not remote or not re.fullmatch(r"[0-9a-f]{40}", remote[0]):
            raise RefreshError(f"Could not resolve main for {repo}")
        location = plugins.marketplace_location(executable, project, name)
        if not location:
            problems.append(f"Missing native marketplace checkout: {name}")
            continue
        local = plugin_refresh.run(["git", "-C", location, "rev-parse", "HEAD"], project).strip()
        revisions[name] = {"installed": local, "main": remote[0]}
        if local != remote[0]:
            problems.append(f"{name} checkout is at {local}, main is {remote[0]}")
        catalog = plugin_refresh.read_json(Path(location) / plugins.CATALOG)
        catalog_sources = {entry["name"]: entry.get("source") for entry in catalog.get("plugins", [])}
        for identity in identities:
            plugin, marketplace = identity.rsplit("@", 1)
            if marketplace != name:
                continue
            try:
                expected = sources.tree(catalog_sources.get(plugin), Path(location), project, temporary, publishers)
            except RefreshError as error:
                problems.append(f"{identity}: {error}")
                continue
            for entry in entries:
                if entry.get("id", entry.get("pluginId")) != identity:
                    continue
                install = plugins.install_path(entry, name, plugin)
                if plugin_refresh.package_tree(install) != expected:
                    problems.append(f"Native installed bytes differ from marketplace: {identity} ({install})")
    return {"host": host.value, "plugins": identities, "marketplaces": revisions, "problems": problems}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", type=Host, choices=list(Host))
    parser.add_argument("--project", type=Path, default=Path.cwd())
    parser.add_argument("--no-relaunch", action="store_true", help="Never relaunch the calling CLI session")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--all", action="store_true", help="Refresh every selected plugin, not only published changes")
    modes.add_argument("--verify", action="store_true", help="Refresh, then inspect native installations and remote main revisions")
    modes.add_argument("--verify-only", action="store_true", help="Inspect without changing plugins")
    parser.add_argument("--onboard", action="store_true", help="allow explicitly requested plugin enrollment")
    arguments = parser.parse_args(argv)
    if arguments.verify_only and arguments.onboard:
        parser.error("--verify-only cannot be used with --onboard")
    project = arguments.project.resolve()
    hosts = (arguments.host,) if arguments.host else tuple(Host)
    allowed = not arguments.no_relaunch
    caller = calling_host()
    updated: set[str] = set()

    def converge_plugin_setup() -> list[str]:
        executables = {host: None for host in hosts}
        return required_plugins.check_setup(project, hosts, executables) if arguments.verify_only \
            else required_plugins.converge_setup(project, hosts, executables, arguments.onboard)
    admission_errors = []
    if not (arguments.all or arguments.verify or arguments.verify_only):
        admitted = []
        for host in hosts:
            try:
                prepare_required(project, host, updated=updated)
            except (required_plugins.RequiredPluginsError, RuntimeError, OSError) as error:
                admission_errors.append({"host": host.value, "problems": [str(error)]})
            else:
                admitted.append(host)
        hosts = tuple(admitted)
        if not hosts:
            print(json.dumps(admission_errors, indent=2))
            return 1
        result = ("unconfigured" if admission_errors else
                  launch_check.check(project, update=lambda host, repositories, root:
                                     launch_check.update_host(host, repositories, root, updated),
                                     hosts=hosts, updated=updated))
        if result != "unconfigured":
            summary = {"check": result}
            reasons = {}
            if result in {"current", "updated", "offline"}:
                for host in hosts:
                    try:
                        reasons[host] = PLUGINS[host].ensure_hooks(project, running_session=host is caller)
                    except Exception as error:
                        summary["hook_readiness_error"] = str(error)
                        result = "failed"
            setup_errors = (converge_plugin_setup() if result not in {"failed", "busy"}
                            else [f"skipped because launch check is {result}"])
            summary["plugin setup"] = setup_errors
            failed = False
            for host in hosts:
                if (host is not caller or result == "failed" or setup_errors
                        or (reasons.get(host) and result == "offline")):
                    continue
                try:
                    adopted = PLUGINS[host].adopt(project, updated, reasons.get(host), allowed)
                except (OSError, ValueError, RuntimeError) as error:
                    summary["hook_readiness_error"] = str(error)
                    result = "failed"
                    continue
                if adopted:
                    summary[host.value] = adopted
                    failed = failed or adopted.get("relaunch") == "failed"
            print(json.dumps(summary, indent=2))
            if result == "updated":
                print(ADOPTION, file=sys.stderr)
            return int(result in {"failed", "offline", "busy"} or failed or bool(setup_errors))
    reports = []
    reasons = {}
    for host in hosts:
        plugins = PLUGINS[host]
        try:
            if not arguments.verify_only:
                prepare_required(project, host, updated=updated)
            executable = plugins.resolve_executable()
            marketplaces, identities = selection(plugins, executable, project)
            changed = [] if arguments.verify_only else refresh(plugins, executable, project, marketplaces, identities)
            updated.update(changed)
            report = (verify(plugins, executable, project, marketplaces, identities)
                      if arguments.verify or arguments.verify_only else {"host": host.value, "plugins": identities, "problems": []})
            if not arguments.verify_only and not report["problems"]:
                reasons[host] = plugins.ensure_hooks(project, executable, running_session=host is caller)
        except (RefreshError, agent_cli.LaunchError, OSError, ValueError, RuntimeError, KeyError, TypeError, subprocess.TimeoutExpired) as error:
            report = {"host": host.value, "problems": [str(error)]}
        reports.append(report)
    setup_errors = (converge_plugin_setup() if not any(report["problems"] for report in reports)
                    else ["skipped because plugin refresh or verification failed"])
    if setup_errors:
        reports.append({"host": "plugin setup", "plugins": [], "problems": setup_errors})
    if not arguments.verify_only:
        for host in hosts:
            if any(report["problems"] for report in reports):
                break
            if host is not caller:
                continue
            try:
                adopted = PLUGINS[host].adopt(project, updated, reasons.get(host), allowed)
            except (OSError, ValueError, RuntimeError) as error:
                reports.append({"host": f"{host} relaunch", "plugins": [], "problems": [str(error)]})
                continue
            if adopted:
                problems = ["relaunch failed: " + adopted["error"]] if adopted.get("relaunch") == "failed" else []
                reports.append({"host": f"{host} relaunch", "plugins": [], **adopted, "problems": problems})
    reports = admission_errors + reports
    print(json.dumps(reports, indent=2))
    for report in reports:
        for problem in report["problems"]:
            print(f"plugins: {report['host']}: {problem}", file=sys.stderr)
    print(ADOPTION, file=sys.stderr)
    return int(any(report["problems"] for report in reports))


if __name__ == "__main__":
    raise SystemExit(main())
