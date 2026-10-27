"""Reconcile merge policy and plugins from GitHub for one repository or every owned one (Python 3.11+)."""

from __future__ import annotations

import argparse
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import re
import subprocess
import sys

from host import Host
import plugin_autoupdate as plugins

OWNERS = ("tj-agents", "Concertable", "tomjseery")
TOPIC_VARIABLE = "MACHINE_EVENTS_TOPIC"
LOG = logging.getLogger("machine-events")
FIELDS = """nameWithOwner isArchived isFork
defaultBranchRef { name target { oid } }
policy: object(expression: "HEAD:.github/merge-policy.json") { oid }
marketplace: object(expression: "HEAD:.claude-plugin/marketplace.json") { oid }
catalog: object(expression: "HEAD:.agents/plugins/marketplace.json") { oid }
plugin: object(expression: "HEAD:.claude-plugin/plugin.json") { oid }
codexPlugin: object(expression: "HEAD:.codex-plugin/plugin.json") { oid }"""


def tommy_environment():
    environment = {key: value for key, value in os.environ.items()
                   if key not in {"TJ_AGENT", "GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN"}
                   and not key.startswith("GIT_CONFIG_")}
    environment["GH_HOST"] = "github.com"
    return environment


def github_cli(arguments, input=None, run=subprocess.run):
    environment = tommy_environment()
    shim = os.path.normcase(str(Path.home() / ".agents-state/bin/bot-identity"))
    path = os.pathsep.join(entry for entry in environment.get("PATH", "").split(os.pathsep)
                           if entry and os.path.normcase(os.path.abspath(entry)) != shim)
    executable = plugins.shutil.which("gh", path=path)
    if not executable:
        raise RuntimeError("GitHub CLI must be installed and signed in as tomjseery")
    result = run([executable, *arguments], input=input,
                 env=environment, capture_output=True, text=True, encoding="utf-8", errors="replace",
                 timeout=60, check=False, creationflags=plugins.NO_WINDOW)
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or "GitHub API failed")
    return result.stdout


def gh(arguments, payload=None, run=subprocess.run):
    output = github_cli(["api", *arguments], input=None if payload is None else json.dumps(payload), run=run)
    return json.loads(output) if output.strip() else None


def require_tommy(api=gh):
    if api(["user"])["login"].lower() != "tomjseery":
        raise RuntimeError("Machine events must run with Tommy's saved gh login (tomjseery)")


def valid_repository(value):
    return isinstance(value, str) and bool(re.fullmatch(
        r"(?:tj-agents|Concertable|tomjseery)/[A-Za-z0-9_][A-Za-z0-9_.-]{0,99}", value, re.I))


def parse_hint(envelope):
    if not isinstance(envelope, dict) or envelope.get("event") != "message":
        return None
    try:
        message = json.loads(envelope.get("message", ""))
    except (ValueError, TypeError):
        return None
    if not isinstance(message, dict) or type(message.get("v")) is not int or message["v"] != 1:
        return None
    return message["repo"] if valid_repository(message.get("repo")) else None


def repository_state(repository, api=gh):
    owner, name = repository.split("/")
    query = "query($owner:String!,$name:String!){repository(owner:$owner,name:$name){" + FIELDS + "}}"
    response = api(["graphql", "-f", "query=" + query, "-f", "owner=" + owner, "-f", "name=" + name])
    if response.get("errors"):
        raise RuntimeError("GitHub repository query failed")
    return response["data"]["repository"]


def catch_up_states(api=gh):
    for owner in OWNERS:
        cursor = None
        while True:
            query = ("query($owner:String!,$after:String){repositoryOwner(login:$owner){repositories("
                     "first:100,after:$after,ownerAffiliations:OWNER){nodes{" + FIELDS
                     + "}pageInfo{hasNextPage endCursor}}}}")
            arguments = ["graphql", "-f", "query=" + query, "-f", "owner=" + owner]
            if cursor:
                arguments += ["-f", "after=" + cursor]
            response = api(arguments)
            if response.get("errors"):
                raise RuntimeError("GitHub catch-up query failed")
            repositories = response["data"]["repositoryOwner"]["repositories"]
            yield from repositories["nodes"]
            page = repositories["pageInfo"]
            if not page["hasNextPage"]:
                break
            cursor = page["endCursor"]


def policy_script():
    config = plugins.load_codex_config()
    roots = [root for _, declaration in plugins.declarations(config)
             for root in plugins.installed_plugin_roots("github-actions", Host.CLAUDE.configuration_root, declaration)]
    relative = Path(".agents/github-actions/utility/merge-policy/scripts/merge_policy.py")
    candidates = [root / relative for root in roots]
    candidates += [root / "skills/merge-policy/scripts/merge_policy.py" for root in roots]
    script = plugins.newest_existing(candidates)
    if script is None:
        raise RuntimeError("Install the github-actions plugin containing merge-policy reconcile before setup")
    return script


def reconcile_policy(repository, run=subprocess.run):
    result = run([plugins.console_python(), "-B", str(policy_script()), "reconcile", "--repo", repository],
                 env=tommy_environment(), capture_output=True, text=True, encoding="utf-8", errors="replace",
                 timeout=180, check=False, creationflags=plugins.NO_WINDOW)
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or "merge-policy reconciliation failed")


def refresh_plugins():
    from claude import plugins as claude_plugins
    from codex import plugins as codex_plugins

    arguments = ["--no-sweep", "--retry-failures"]
    managed_runtime = plugins.state_directory() / "bin" / ".agents" / "agent" / "runtime"
    if Path(plugins.__file__).resolve().parent != managed_runtime.resolve():
        arguments.append("--no-self-update")
    result = plugins.main(arguments)
    claude_plugins.update_registered()
    config = plugins.load_codex_config()
    executable = None
    for project, declared in plugins.declarations(config):
        if plugins.enabled_identities(declared):
            executable = executable or codex_plugins.resolve_executable()
            codex_plugins.reconcile(project, executable=executable)
    return result


class Reconciler:
    def __init__(self, state=None, refresh=None, policy=reconcile_policy):
        self.state = state if state is not None else {}
        self.refresh = refresh or refresh_plugins
        self.policy = policy

    def repository(self, current, refresh_plugins=True):
        if not current or current.get("isArchived") or current.get("isFork"):
            return
        repository = current.get("nameWithOwner")
        if not valid_repository(repository) or not current.get("defaultBranchRef"):
            return
        revision = current["defaultBranchRef"]["target"]["oid"]
        old = self.state.get(repository, {})
        policy = (current.get("policy") or {}).get("oid")
        catalog = sorted(oid for name in ("marketplace", "catalog", "plugin", "codexPlugin")
                        if (oid := (current.get(name) or {}).get("oid")))
        if old.get("revision") == revision:
            return
        if policy and policy != old.get("policy"):
            self.policy(repository)
        if catalog and (catalog != old.get("catalog") or repository.lower().startswith("tj-agents/")) and refresh_plugins:
            if self.refresh() != 0:
                raise RuntimeError("plugin refresh failed")
        self.state[repository] = {"revision": revision, "policy": policy, "catalog": catalog}

    def catch_up(self, states):
        if self.refresh() != 0:
            raise RuntimeError("plugin catch-up failed")
        failures = []
        for current in states:
            try:
                self.repository(current, refresh_plugins=False)
            except Exception:
                LOG.exception("Catch-up failed for %s", current.get("nameWithOwner"))
                failures.append(current["nameWithOwner"])
        return failures


def topic_value(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", value):
        raise ValueError("Machine event topic must contain 32-128 letters, digits, underscores or hyphens")
    return value


def main(argv=None):
    if sys.version_info < (3, 11):
        raise RuntimeError("machine_events requires Python 3.11 or newer")
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--repo")
    mode.add_argument("--watch", action="store_true")
    parser.add_argument("--state-directory", type=Path)
    parser.add_argument("--claude-home", type=Path)
    parser.add_argument("--codex-home", type=Path)
    parser.add_argument("--search-path")
    arguments = parser.parse_args(argv)
    if arguments.state_directory:
        os.environ["AGENT_STATE_DIRECTORY"] = str(arguments.state_directory.resolve())
    for host in Host:
        home = getattr(arguments, host.value + "_home")
        if home:
            os.environ[host.home_variable] = str(home.resolve())
    if arguments.search_path is not None:
        os.environ["PATH"] = arguments.search_path
    directory = plugins.state_directory()
    directory.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(directory / "machine-events.log", maxBytes=262144, backupCount=2,
                                  encoding="utf-8")
    LOG.addHandler(handler)
    LOG.setLevel(logging.INFO)
    lock = plugins.acquire_lock(directory / ("machine-events-watch.lock" if arguments.watch else "machine-events.lock"))
    if lock is None:
        LOG.warning("Machine-event reconciliation is already running")
        LOG.removeHandler(handler)
        handler.close()
        return 0 if arguments.watch else 1
    try:
        state_file = directory / "machine-events-state.json"
        reconciler = Reconciler(plugins.read_json(state_file))
        require_tommy()
        if arguments.watch:
            import event_watch

            return event_watch.run(directory)
        failures = []
        if arguments.repo:
            if not valid_repository(arguments.repo):
                raise ValueError("Unsupported repository")
            reconciler.repository(repository_state(arguments.repo))
        else:
            failures = reconciler.catch_up(catch_up_states())
        plugins.write_json(state_file, reconciler.state)
        return int(bool(failures))
    finally:
        lock.close()
        LOG.removeHandler(handler)
        handler.close()


if __name__ == "__main__":
    raise SystemExit(main())
