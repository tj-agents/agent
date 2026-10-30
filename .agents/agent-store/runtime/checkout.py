"""Git checkout probing and repository identity for the session store (Python 3.9+)."""

from dataclasses import dataclass
import os
from pathlib import Path
import re
import subprocess
from typing import Optional
from urllib.parse import urlparse


GIT_TIMEOUT = 3
HOOK_GIT_TIMEOUT = 2

_SSH_SCP_RE = re.compile(r"^[^/@\s]+@(?P<host>[^:/\s]+):(?P<path>.+)$")


def _run_git(cwd, args, timeout):
    try:
        result = subprocess.run(
            ["git", "-C", str(cwd), *args],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return None, True
    except (OSError, subprocess.SubprocessError):
        return None, False
    if result.returncode != 0:
        return None, False
    output = result.stdout.strip()
    return (output or None), False


def git(cwd, *args, timeout=GIT_TIMEOUT):
    output, _timed_out = _run_git(cwd, args, timeout)
    return output


def _host_and_path(url):
    if url.startswith(("ssh://", "https://", "http://", "git://")):
        parsed = urlparse(url)
        host = (parsed.hostname or "").casefold()
        return (host or None), parsed.path
    match = _SSH_SCP_RE.match(url)
    if not match:
        return None, None
    return match.group("host").casefold(), match.group("path")


def normalise_remote(url):
    url = (url or "").strip()
    if not url:
        return None
    host, path = _host_and_path(url)
    if not host or not path:
        return None
    path = path.strip("/")
    if path.endswith(".git"):
        path = path[: -len(".git")]
    if not path:
        return None
    if host in ("ssh.dev.azure.com", "vs-ssh.visualstudio.com"):
        parts = [part for part in path.split("/") if part]
        if parts and parts[0] == "v3":
            parts = parts[1:]
        if len(parts) != 3:
            return None
        org, project, repository = parts
        host = "dev.azure.com"
        path = f"{org}/{project}/_git/{repository}"
    elif host.endswith(".visualstudio.com"):
        org = host[: -len(".visualstudio.com")]
        if not org or "." in org:
            return None
        parts = [part for part in path.split("/") if part]
        if parts and parts[0].casefold() == "defaultcollection":
            parts = parts[1:]
        if len(parts) == 2 and parts[0] == "_git":
            repository = parts[1]
            project = repository
        elif len(parts) == 3 and parts[1] == "_git":
            project, _git_segment, repository = parts
        else:
            return None
        host = "dev.azure.com"
        path = f"{org}/{project}/_git/{repository}"
    return f"{host}/{path}".casefold()


def repository_identity(cwd, timeout=GIT_TIMEOUT):
    origin = git(cwd, "config", "--get", "remote.origin.url", timeout=timeout)
    repository = normalise_remote(origin) if origin else None
    if repository:
        return repository
    common_dir = git(cwd, "rev-parse", "--git-common-dir", timeout=timeout)
    if not common_dir:
        return None
    path = Path(common_dir)
    if not path.is_absolute():
        path = Path(cwd) / path
    try:
        return str(path.resolve())
    except OSError:
        return None


def _branch_from_symbolic_ref(value):
    if not value:
        return None
    prefix = "refs/heads/"
    return value[len(prefix):] if value.startswith(prefix) else None


def _rev_parse_probe(cwd, timeout):
    output, timed_out = _run_git(
        cwd, ["rev-parse", "HEAD", "--git-common-dir", "--symbolic-full-name", "HEAD", "--show-toplevel"], timeout,
    )
    if output:
        lines = output.splitlines()
        if len(lines) >= 3:
            head = lines[0].strip() or None
            common_dir = lines[1].strip() or None
            branch = _branch_from_symbolic_ref(lines[2].strip())
            toplevel = os.path.normpath(lines[3].strip()) if len(lines) >= 4 and lines[3].strip() else None
            return head, branch, common_dir, toplevel
    if timed_out:
        return None, None, None, None
    common_dir = git(cwd, "rev-parse", "--git-common-dir", timeout=timeout)
    if not common_dir:
        return None, None, None, None
    symbolic = git(cwd, "symbolic-ref", "-q", "HEAD", timeout=timeout)
    return None, _branch_from_symbolic_ref(symbolic), common_dir, None


def _is_default_branch(cwd, common_dir, branch):
    if branch in ("main", "master"):
        return True
    if not branch or not common_dir:
        return False
    path = Path(common_dir)
    if not path.is_absolute():
        path = Path(cwd) / path
    try:
        ref = (path / "refs" / "remotes" / "origin" / "HEAD").read_text(encoding="utf-8").strip()
    except OSError:
        return False
    return ref == f"ref: refs/remotes/origin/{branch}"


@dataclass(frozen=True)
class Checkout:
    root: str
    repo: Optional[str]
    branch: Optional[str]
    head: Optional[str]
    is_default: bool

    @classmethod
    def probe(cls, cwd, timeout=HOOK_GIT_TIMEOUT):
        head, branch, common_dir, toplevel = _rev_parse_probe(cwd, timeout)
        repo = repository_identity(cwd, timeout)
        return cls(toplevel or str(cwd), repo, branch, head, _is_default_branch(cwd, common_dir, branch))

    @property
    def on_branch(self):
        return bool(self.repo and self.branch)
