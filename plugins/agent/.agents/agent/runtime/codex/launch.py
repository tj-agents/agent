"""Codex executable selection, owned native launches and thread liveness."""

import argparse
import glob
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
from collections.abc import Mapping, Sequence

from tj_cli import process
from codex import observer, processes, startup
from tj_agent.host import Host

import agent_store
from launch_contract import Confirmation, Liveness, PreparedLaunch
from launch_errors import LaunchError
from native_executable import is_script

IS_WINDOWS = os.name == "nt"
SESSION_ENVIRONMENT: tuple[str, ...] = ()
TAB_CLEARED: tuple[str, ...] = ("TERM",)
TAB_FORCED: dict[str, str] = {}
DAEMON_TIMEOUT_SECONDS = 10
_CODEX_VERSION = re.compile(r"(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?")


def record_session_environment(
    session_id: str, environ: Mapping[str, str], *, cleared: tuple[str, ...] = ()
) -> None:
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


def path_executables():
    """Every `codex` on PATH in PATH order, so a stale copy early on PATH cannot hide a newer one later."""
    found = []
    for directory in os.environ.get("PATH", "").split(os.pathsep):
        if directory and (hit := shutil.which("codex", path=directory)):
            found.append(hit)
    return found


def candidate_paths(
    on_path=path_executables, local_app_data=None, npm_prefix=npm_global_prefix
):
    name = "codex.exe" if IS_WINDOWS else "codex"
    candidates = []

    shims = on_path()

    if not shims and not IS_WINDOWS:
        prefix = npm_prefix()
        if prefix:
            direct = Path(prefix) / "bin" / name
            if direct.is_file():
                shims = [str(direct)]

    for shim in shims:
        if (IS_WINDOWS and Path(shim).suffix.casefold() == ".exe") or (
            not IS_WINDOWS and not is_script(os.path.realpath(shim))
        ):
            candidates.append(shim)
            candidates.append(os.path.realpath(shim))
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


def _running_daemon(executable: str) -> tuple[str, str | None] | None:
    command = [executable, "app-server", "daemon", "version"]
    try:
        result = process.run(command, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError) as error:
        raise LaunchError(
            f"Could not check the default Codex daemon: {error}"
        ) from error
    if result.returncode:
        raise LaunchError(
            f"Could not check the default Codex daemon: {result.stderr.strip() or result.returncode}"
        )
    try:
        detail = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise LaunchError(
            "The Codex daemon version command returned invalid JSON"
        ) from error
    if not isinstance(detail, dict) or not isinstance(detail.get("status"), str):
        raise LaunchError("The Codex daemon version command returned no status")
    if detail["status"] != "running":
        return None
    version = detail.get("appServerVersion")
    if not isinstance(version, str) or parse_version(version) is None:
        raise LaunchError("The running Codex daemon did not report its version")
    installed = detail.get("managedCodexPath")
    return version, installed if isinstance(installed, str) and Path(
        installed
    ).is_absolute() else None


def _executable_version(path: str) -> str | None:
    try:
        result = process.run(
            [path, "--version"], capture_output=True, text=True, timeout=30
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout + result.stderr if result.returncode == 0 else None


def resolve_executable(
    minimum=startup.MINIMUM_VERSION, candidates=None, *, match_daemon: bool = False
):
    """The newest native Codex executable that answers --version, as (path, version)."""
    paths = candidate_paths() if candidates is None else candidates
    found = []
    for path in paths:
        version = parse_version(_executable_version(path))
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

    if minimum is None:
        return best
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
    if match_daemon and (daemon := _running_daemon(best[0])) is not None:
        active, installed = daemon
        for candidate in found:
            if (
                candidate[1]["text"] == active
                and compare_version(candidate[1], floor) >= 0
            ):
                return candidate
        if installed is not None:
            version = parse_version(_executable_version(installed))
            if (
                version
                and version["text"] == active
                and compare_version(version, floor) >= 0
            ):
                return installed, version
        raise LaunchError(
            f"The default Codex daemon is running {active}, but no compatible executable "
            f"at or above {minimum} is available. Check the managed daemon package with "
            "codex app-server daemon version."
        )
    return best


def executable() -> str:
    return resolve_executable()[0]


def managed_arguments(arguments: Sequence[str]) -> tuple[str, ...]:
    options = tuple(arguments)
    before_prompt = options[: options.index("--")] if "--" in options else options
    attached = False
    for index, value in enumerate(before_prompt):
        key, separator, target = value.partition("=")
        if key in {"--no-daemon", "--dangerously-bypass-hook-trust", "--remote-auth-token-env"}:
            raise LaunchError(f"Managed Codex requires its local daemon; {key} is incompatible")
        if key == "--remote":
            if not separator:
                target = before_prompt[index + 1] if index + 1 < len(before_prompt) else ""
            if target != "unix://":
                raise LaunchError("Managed Codex requires the native default local daemon (--remote unix://)")
            attached = True
    return options if attached else (*startup.MANAGED_OPTIONS, *options)


def require_stopped_thread(thread_id: str) -> None:
    observed = liveness(strict=True).get(thread_id)
    if observed is not None and observed.alive:
        raise LaunchError(f"Codex {thread_id} is already running; use its existing conversation")


def prepare_foreground(
    executable: str, directory: Path, arguments: Sequence[str], cli: str,
    environ: Mapping[str, str],
) -> PreparedLaunch:
    options = managed_arguments(arguments)
    delimiter = options.index("--") if "--" in options else len(options)
    literal = options[delimiter + 1:]
    parser = argparse.ArgumentParser(add_help=False, allow_abbrev=False, exit_on_error=False)
    parser.add_argument("-C", "--cd")
    parser.add_argument("-m", "--model")
    parser.add_argument("-c", "--config", action="append", default=[])
    parser.add_argument("--remote")
    parser.add_argument("values", nargs="*")
    try:
        parsed, extra = parser.parse_known_intermixed_args(options[:delimiter])
    except argparse.ArgumentError as error:
        raise LaunchError(f"Cannot bind this managed foreground Codex command: {error}") from error
    if parsed.values and parsed.values[0] in {"exec", "e", "review"}:
        return PreparedLaunch((executable, *options))
    if extra:
        raise LaunchError(f"Unsupported managed foreground Codex option: {extra[0]}")
    command = ["--cd", str(directory)]
    if parsed.values[:1] == ["resume"]:
        if len(parsed.values) != 2 or parsed.model or parsed.config or literal:
            raise LaunchError("Managed foreground resume requires an exact native UUID; use tj recover for session selection")
        thread_id = startup.native_id(parsed.values[1])
        require_stopped_thread(thread_id)
        command.extend(("resume", thread_id))
    else:
        if parsed.values[:1] == ["fork"]:
            raise LaunchError(f"Managed foreground Codex does not support the {parsed.values[0]} subcommand")
        prompt = [*parsed.values, *literal]
        if len(prompt) > 1 or len(parsed.config) > 1:
            raise LaunchError("Managed foreground Codex accepts one prompt and one reasoning override")
        if parsed.model is not None:
            command.extend(("--model", parsed.model))
        if parsed.config:
            command.extend(("-c", parsed.config[0]))
        command.extend(prompt)
    return startup.prepare(directory, directory.name or "Codex", command, cli, executable, environ)


def prepare(directory: Path, title: str, arguments: list[str], cli: str, environ: Mapping[str, str]) -> PreparedLaunch:
    native = str(resolve_executable(minimum=startup.MINIMUM_VERSION, match_daemon=True)[0])
    if arguments[:3] == ["--cd", str(directory), "resume"] and len(arguments) == 4:
        thread_id = startup.native_id(arguments[3])
        require_stopped_thread(thread_id)
    return startup.prepare(directory, title, arguments, cli, native, environ)


def abandon(prepared: PreparedLaunch, launch_id: str, dispatched: bool) -> None:
    startup.abandon(prepared, launch_id, dispatched)


def confirm(prepared: PreparedLaunch, arguments: list[str], launch_id: str) -> Confirmation:
    return startup.confirm(prepared, arguments, launch_id)


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
    """Combine native process evidence with loaded shared-daemon threads."""
    observed: dict[str, Liveness] = {}
    try:
        observed = processes.inventory()
        if strict:
            result = process.run(
                [executable(), "app-server", "daemon", "version"],
                capture_output=True, text=True, encoding="utf-8",
                timeout=DAEMON_TIMEOUT_SECONDS, check=False,
            )
            if result.returncode:
                detail = result.stderr.strip() or result.stdout.strip()
                if "failed to connect to" in detail:
                    try:
                        observer.socket_path(Host.CODEX.configuration_root).stat()
                    except FileNotFoundError:
                        return observed
                    if any(f"(os error {code})" in detail for code in (2, 111, 10061)):
                        return observed
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
        return observed
    for thread, status in statuses.items():
        if status != "notLoaded":
            # A loaded daemon thread and an embedded frontend can coexist. Keep
            # both observations: reloading the daemon does not reload that process.
            previous = observed.get(thread)
            observed[thread] = Liveness(
                alive=True,
                activity=status,
                process_ids=previous.process_ids if previous else None,
                launch_ids=previous.launch_ids if previous else None,
                processes_reloadable=previous.processes_reloadable if previous else False,
            )
    return observed


def respawn(background):
    raise LaunchError("Codex has no background sessions")


def name_session(session_id, title):
    with startup.connection(executable(), Path.cwd()) as server:
        server.request("thread/name/set", {"threadId": session_id, "name": title})
