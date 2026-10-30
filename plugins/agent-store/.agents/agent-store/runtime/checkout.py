"""Git checkout probing for the session store (Python 3.9+)."""

from dataclasses import dataclass
import os
from pathlib import Path
import subprocess
import sys
from typing import Optional

_RUNTIME = str(Path(__file__).resolve().parent)
sys.path.insert(0, _RUNTIME)
try:
    from agent_store_loader import load_sibling
finally:
    sys.path.remove(_RUNTIME)

_identity = load_sibling(__file__, "repository_identity")

GIT_TIMEOUT = 3
HOOK_GIT_TIMEOUT = 2
DEFAULT_BRANCHES = ("main", "master")
_ORIGIN_HEAD_PREFIX = "ref: refs/remotes/origin/"


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


def identify_repository(cwd, timeout=GIT_TIMEOUT):
    return _identity.repository_identity(
        cwd, lambda directory, *arguments: git(directory, *arguments, timeout=timeout),
    )


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


def _origin_default_branch(cwd, common_dir):
    if not common_dir:
        return None
    path = Path(common_dir)
    if not path.is_absolute():
        path = Path(cwd) / path
    try:
        ref = (path / "refs" / "remotes" / "origin" / "HEAD").read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not ref.startswith(_ORIGIN_HEAD_PREFIX):
        return None
    return ref[len(_ORIGIN_HEAD_PREFIX):] or None


def _is_default_branch(cwd, common_dir, branch):
    if not branch:
        return False
    default = _origin_default_branch(cwd, common_dir)
    return branch in DEFAULT_BRANCHES if default is None else branch == default


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
        repo = identify_repository(cwd, timeout)
        return cls(toplevel or str(cwd), repo, branch, head, _is_default_branch(cwd, common_dir, branch))

    @property
    def on_branch(self):
        return bool(self.repo and self.branch)

    @property
    def work_branch(self):
        return None if not self.branch or self.is_default else self.branch
