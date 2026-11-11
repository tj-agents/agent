"""Codex executable selection, daemon-attached launch arguments and thread liveness."""

import glob
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
from collections.abc import Mapping

from tj_cli import process

import agent_store
from launch_contract import Liveness
from launch_errors import LaunchError
from native_executable import is_script

IS_WINDOWS = os.name == "nt"
SESSION_ENVIRONMENT: tuple[str, ...] = ()
TAB_CLEARED: tuple[str, ...] = ("TERM",)
TAB_FORCED: dict[str, str] = {}
DAEMON_TIMEOUT_SECONDS = 10
_CODEX_VERSION = re.compile(r"(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?")


def record_session_environment(session_id: str, environ: Mapping[str, str]) -> None:
    return None


def registered_session(session_id: str) -> dict[str, object] | None:
    handle = f"codex:{session_id}"
    try:
        shown = agent_store.run("show", handle)
    except agent_store.AgentStoreError as error:
        if error.code == "not-found":
            return None
        raise
    session = shown.get("session")
    if not isinstance(session, dict):
        raise LaunchError(f"agent-store returned an invalid session for {handle}")
    launch_id = session.get("launch_id")
    if launch_id is None or session.get("state") == "ended":
        return None
    if (
        not isinstance(launch_id, str) or not launch_id
        or session.get("session") != handle
        or session.get("host") != "codex"
        or session.get("native_id") != session_id
    ):
        raise LaunchError(f"agent-store returned an invalid launch binding for {handle}")
    bound = agent_store.run("show", "--launch-id", launch_id)
    launch, session = bound.get("launch"), bound.get("session")
    if (
        not isinstance(launch, dict) or not isinstance(session, dict)
        or launch.get("launch_id") != launch_id
        or launch.get("host") != "codex" or launch.get("session") != handle
        or session.get("session") != handle or session.get("host") != "codex"
        or session.get("native_id") != session_id or session.get("launch_id") != launch_id
    ):
        raise LaunchError(f"agent-store launch and session bindings do not match for {handle}")
    return None if session.get("state") == "ended" else session


def parse_version(text):
    match = _CODEX_VERSION.search(text or "")
    if not match:
        return None
    return {
        "core": tuple(int(match.group(i)) for i in (1, 2, 3)),
        "prerelease": match.group(4) or "",
        "text": match.group(0),
    }


def compare_version(left, right):
    if left["core"] != right["core"]:
        return -1 if left["core"] < right["core"] else 1

    if bool(left["prerelease"]) != bool(right["prerelease"]):
        return -1 if left["prerelease"] else 1
    if not left["prerelease"]:
        return 0

    left_parts, right_parts = (
        left["prerelease"].split("."),
        right["prerelease"].split("."),
    )
    for i in range(max(len(left_parts), len(right_parts))):
        if i >= len(left_parts):
            return -1
        if i >= len(right_parts):
            return 1
        a, b = left_parts[i], right_parts[i]
        if a.isdigit() and b.isdigit():
            a, b = int(a), int(b)
        if a != b:
            return -1 if a < b else 1
    return 0


def npm_global_prefix():
    """The npm global install prefix, or None when npm is unavailable or reports nothing."""
    try:
        result = process.run(
            ["npm", "config", "get", "prefix"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    prefix = result.stdout.strip()
    return prefix or None


def candidate_paths(
    which=shutil.which, local_app_data=None, npm_prefix=npm_global_prefix
):
    name = "codex.exe" if IS_WINDOWS else "codex"
    candidates = []

    shim = which("codex")

    if not shim and not IS_WINDOWS:
        prefix = npm_prefix()
        if prefix:
            direct = Path(prefix) / "bin" / name
            if direct.is_file():
                shim = str(direct)

    if shim and (
        (IS_WINDOWS and Path(shim).suffix.casefold() == ".exe")
        or (not IS_WINDOWS and not is_script(os.path.realpath(shim)))
    ):
        candidates.append(shim)
        candidates.append(os.path.realpath(shim))

    if shim:
        roots = [
            Path(shim).parent / "node_modules" / "@openai" / "codex",
            Path(os.path.realpath(shim)).parent.parent,
        ]
        for root in dict.fromkeys(roots):
            pattern = str(
                root
                / "node_modules"
                / "@openai"
                / "codex-*"
                / "vendor"
                / "*"
                / "bin"
                / name
            )
            candidates.extend(sorted(glob.glob(pattern)))

    if IS_WINDOWS:
        local_app_data = local_app_data or os.environ.get("LOCALAPPDATA")
        if local_app_data:
            desktop = Path(local_app_data) / "OpenAI" / "Codex" / "bin"
            candidates.extend(
                sorted(str(p) for p in desktop.rglob("codex.exe") if p.is_file())
            )

    return list(dict.fromkeys(candidates))


def resolve_executable(minimum="0.154.0", candidates=None):
    """The newest native Codex executable that answers --version, as (path, version)."""
    paths = candidate_paths() if candidates is None else candidates
    found = []
    for path in paths:
        try:
            result = process.run(
                [path, "--version"], capture_output=True, text=True, timeout=30
            )
        except (OSError, subprocess.SubprocessError):
            continue
        version = parse_version(result.stdout + result.stderr)
        if version:
            found.append((path, version))

    if not found:
        raise LaunchError(
            "No native Codex executable answered --version. Looked beside the `codex` shim on PATH for the npm "
            "package's vendored binary"
            + (", and under the desktop app cache." if IS_WINDOWS else ".")
        )

    best = found[0]
    for candidate in found[1:]:
        if compare_version(candidate[1], best[1]) > 0:
            best = candidate

    floor = parse_version(minimum)
    if floor is None:
        raise LaunchError(f"The minimum Codex version is not a version: {minimum}")
    if compare_version(best[1], floor) < 0:
        inventory = "\n".join(f"  {version['text']}\t{path}" for path, version in found)
        raise LaunchError(
            f"The newest native Codex executable found is {best[1]['text']}, below the required {minimum}.\n"
            "The model roster is gated on the CLI version, so launching this build would silently drop newer models.\n"
            "Update with: npm install -g @openai/codex@latest\n"
            f"Found:\n{inventory}"
        )
    return best


def executable() -> str:
    return resolve_executable()[0]


def start_arguments(directory, title, prompt, model, effort):
    arguments = ["--cd", str(directory)]
    if model:
        arguments += ["--model", model]
    if effort:
        arguments += ["-c", f"model_reasoning_effort={effort}"]
    return [*arguments, prompt]


def resume_arguments(directory, title, session_id):
    return ["--cd", str(directory), "resume", session_id]


def daemon(timeout=DAEMON_TIMEOUT_SECONDS):
    from codex import hook_trust

    return hook_trust.AppServer(executable(), Path.cwd(), timeout=timeout, existing=True)


def liveness(*, strict: bool = False) -> dict[str, Liveness]:
    """Threads loaded in the shared app-server daemon, by thread id; an absent daemon has none."""
    try:
        if strict:
            result = process.run(
                [executable(), "app-server", "daemon", "version"],
                capture_output=True, text=True, encoding="utf-8",
                timeout=DAEMON_TIMEOUT_SECONDS, check=False,
            )
            if result.returncode:
                detail = result.stderr.strip() or result.stdout.strip()
                if "failed to connect to" in detail and any(
                    f"(os error {code})" in detail for code in (2, 111, 10061)
                ):
                    return {}
                raise LaunchError(f"Cannot determine Codex daemon state: {detail}")
            if not isinstance(json.loads(result.stdout), dict):
                raise LaunchError("Codex daemon version returned invalid JSON")
        with daemon() as server:
            loaded, cursor = [], None
            while True:
                page = server.request("thread/loaded/list", {"cursor": cursor})
                loaded += page["data"]
                cursor = page.get("nextCursor")
                if not cursor:
                    break
            statuses = {thread: server.request("thread/read", {"threadId": thread})["thread"]["status"]["type"]
                        for thread in loaded}
    except (
        OSError, subprocess.SubprocessError, RuntimeError, LaunchError,
        ValueError, KeyError, TypeError,
    ) as error:
        if strict:
            raise LaunchError(f"Cannot determine Codex liveness: {error}") from error
        return {}
    return {thread: Liveness(alive=status != "notLoaded", activity=status) for thread, status in statuses.items()}


def respawn(background):
    raise LaunchError("Codex has no background sessions")


def name_session(session_id, title):
    with daemon() as server:
        server.request("thread/name/set", {"threadId": session_id, "name": title})
