#!/usr/bin/env python3
"""Install and converge this machine's tj-agents environment (Python 3.11+).

Runs an ordered list of steps, each with a check and an apply. A step whose check reports no drift is
left alone, so rerunning converges and writes nothing once the machine is current. `--check` reports
drift without writing anything and exits 1 when something would change. Installed launch paths use
the managed package closure, independently of source checkouts.
"""

from __future__ import annotations

import argparse
import codecs
from dataclasses import dataclass, field
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import secrets
import shlex
import shutil
import subprocess
import sys
from types import ModuleType
from typing import Callable

RUNTIME = Path(__file__).resolve().parent
ROOT = RUNTIME.parents[2]
sys.path.insert(0, str(RUNTIME))

from host import Host
import launch_check
import plugin_autoupdate
import machine_events
import host_permissions
import required_plugins
import runtime_delivery

COMMAND_TIMEOUT_SECONDS = 60
INSTALLER_TIMEOUT_SECONDS = 600
TOPIC_REPOSITORY = "tj-agents/core"
RUNTIME_EXPORTS = (
    ".agents/agent/runtime",
    ".agents/agent/resources",
    ".agents/agent/utility/plugins/scripts",
    ".agents/agent/utility/peer/scripts",
    ".agents/agent/utility/close/scripts",
    ".agents/agent-store/runtime",
)
PROFILE_START = "# >>> tj-agents >>>"
PROFILE_END = "# <<< tj-agents <<<"
PROFILE_BACKUP = ".pre-tj-agents.bak"
PROFILE_NAMES = ("WindowsPowerShell", "PowerShell")
DEFAULT_GIT_BASH = r"C:\Program Files\Git\bin\bash.exe"
HIDDEN_BASH_PROBE = ("-c", "printf HIDDEN_BASH_OK; exit 73")
WINDOWS_ONLY = "Windows only: shell/ is the Windows PowerShell profile and its tools"


class InstallError(RuntimeError):
    pass


@dataclass
class Context:
    home: Path
    environ: dict
    windows: bool
    run: Callable = subprocess.run
    which: Callable = shutil.which
    documents: Path | None = None
    user_environment: object = None
    root: Path = ROOT
    project: Path | None = None
    cache: dict = field(default_factory=dict)
    onboard: bool = False

    def path_from(self, variable: str, default: Path) -> Path:
        configured = self.environ.get(variable)
        return Path(configured).expanduser() if configured else default

    @property
    def state_directory(self) -> Path:
        return self.path_from("AGENT_STATE_DIRECTORY", self.home / ".agents-state")

    @property
    def claude_home(self) -> Path:
        return self.path_from(Host.CLAUDE.home_variable, self.home / Host.CLAUDE.home_directory)

    @property
    def codex_home(self) -> Path:
        return self.path_from(Host.CODEX.home_variable, self.home / Host.CODEX.home_directory)

@dataclass(frozen=True)
class Step:
    name: str
    check: Callable[[Context], list[str]]
    apply: Callable[[Context], list[str]]
    skip: Callable[[Context], str | None] = lambda context: None
    required: bool = False


def documents_directory(home: Path) -> Path:
    if os.name != "nt":
        return home / "Documents"
    import ctypes
    import uuid

    class Guid(ctypes.Structure):
        _fields_ = [("data", ctypes.c_ubyte * 16)]

    folder = Guid.from_buffer_copy(uuid.UUID("{FDD39AD0-238F-46AF-ADB4-6C85480369C7}").bytes_le)
    path = ctypes.c_wchar_p()
    if ctypes.windll.shell32.SHGetKnownFolderPath(ctypes.byref(folder), 0, None, ctypes.byref(path)) != 0:
        return Path(os.environ.get("USERPROFILE") or home) / "Documents"
    try:
        return Path(path.value)
    finally:
        ctypes.windll.ole32.CoTaskMemFree(path)


class WindowsUserEnvironment:
    def get(self, name: str) -> str | None:
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
                value, _ = winreg.QueryValueEx(key, name)
        except FileNotFoundError:
            return None
        return value if isinstance(value, str) else None

    def set(self, name: str, value: str) -> None:
        import winreg
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, "Environment", 0,
                                winreg.KEY_SET_VALUE | winreg.KEY_QUERY_VALUE) as key:
            try:
                _, kind = winreg.QueryValueEx(key, name)
            except FileNotFoundError:
                kind = winreg.REG_SZ
            winreg.SetValueEx(key, name, 0, kind if kind in (winreg.REG_SZ, winreg.REG_EXPAND_SZ) else winreg.REG_SZ, value)

    def broadcast(self) -> None:
        import ctypes
        from ctypes import wintypes
        send = ctypes.windll.user32.SendMessageTimeoutW
        send.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPCWSTR, wintypes.UINT,
                         wintypes.UINT, ctypes.POINTER(ctypes.c_size_t)]
        send(wintypes.HWND(0xFFFF), 0x001A, 0, "Environment", 0x0002, 5000, ctypes.byref(ctypes.c_size_t()))


PROFILE_BOMS = (
    (codecs.BOM_UTF16_LE, "utf-16-le"),
    (codecs.BOM_UTF16_BE, "utf-16-be"),
    (codecs.BOM_UTF8, "utf-8"),
)
PROFILE_BOM_BYTES = {encoding: bom for bom, encoding in PROFILE_BOMS}


def read_profile(path: Path) -> tuple[str, str, bool] | None:
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return None
    for bom, encoding in PROFILE_BOMS:
        if raw.startswith(bom):
            return raw[len(bom):].decode(encoding), encoding, True
    return raw.decode("utf-8"), "utf-8", False


def write_profile(path: Path, text: str, encoding: str, bom: bool) -> None:
    if path.exists():
        shutil.copyfile(path, path.with_name(path.name + PROFILE_BACKUP))
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".next")
    prefix = PROFILE_BOM_BYTES[encoding] if bom else b""
    temporary.write_bytes(prefix + text.encode(encoding))
    os.replace(temporary, path)


def without_trailing_newline_span(text: str, finish: int) -> int:
    if text[finish:finish + 2] == "\r\n":
        return finish + 2
    if text[finish:finish + 1] == "\n":
        return finish + 1
    return finish


def block_spans(text: str, start: str, end: str) -> list[tuple[int, int]]:
    if text.count(start) != text.count(end):
        raise InstallError(f"Malformed profile block markers {start!r} / {end!r}")
    spans = []
    position = 0
    while (begin := text.find(start, position)) >= 0:
        finish = text.find(end, begin + len(start))
        following = text.find(start, begin + len(start))
        if finish < 0 or 0 <= following < finish:
            raise InstallError(f"Malformed profile block markers {start!r} / {end!r}")
        spans.append((begin, finish + len(end)))
        position = finish + len(end)
    return spans


def profile_paths(context: Context) -> list[Path]:
    if context.windows:
        documents = context.documents or documents_directory(context.home)
        return [documents / name / "Microsoft.PowerShell_profile.ps1" for name in PROFILE_NAMES]
    return [context.home / ".bashrc", context.home / ".zshrc",
            context.home / ".config" / "powershell" / "Microsoft.PowerShell_profile.ps1"]


def powershell_block(python: str, script: Path, loader: Path | None, newline: str) -> str:
    def quoted(value: object) -> str:
        return "'" + str(value).replace("'", "''") + "'"

    lines = [PROFILE_START]
    if loader is not None:
        for name in ("TJ_AGENTS_REAL_GIT_BASH", "CLAUDE_CODE_GIT_BASH_PATH"):
            lines += [
                f"$tjAgentsBash = [Environment]::GetEnvironmentVariable('{name}', 'User')",
                f"if ($tjAgentsBash) {{ $env:{name} = $tjAgentsBash }}",
            ]
        lines += [
            f"$tjAgentsShell = {quoted(loader)}",
            "if (Test-Path -LiteralPath $tjAgentsShell) { . $tjAgentsShell }",
            'else { Write-Host "profile: tj-agents missing at $tjAgentsShell" -ForegroundColor Red }',
        ]
    for name in map(str, Host):
        lines += [
            f"function global:{name} {{",
            f"    $tjAgentsTarget = Get-Command {name} -CommandType Application, ExternalScript"
            " -ErrorAction SilentlyContinue | Select-Object -First 1",
            f"    if (-not $tjAgentsTarget) {{ Write-Error '{name} was not found on PATH'; return }}",
            f"    if (-not (Test-Path -LiteralPath {quoted(python)})) {{ Write-Error 'Standards verifier Python is unavailable'; $global:LASTEXITCODE = 1; return }}",
            f"    try {{ $env:{launch_check.HOST_VARIABLE} = '{name}';"
            f" & {quoted(python)} -I -B {quoted(script)} --host {name} --project (Get-Location).Path"
            " --target $tjAgentsTarget.Source --require-session-start -- @args }"
            f" finally {{ Remove-Item Env:{launch_check.HOST_VARIABLE} -ErrorAction SilentlyContinue }}",
            "}",
        ]
    return newline.join([*lines, PROFILE_END])


def posix_block(python: str, script: Path) -> str:
    def quoted(value: object) -> str:
        return "'" + str(value).replace("'", "'\\''") + "'"

    runtime_bin = script.parents[3] if len(script.parents) > 3 else script.parent
    return "\n".join([PROFILE_START, f'export PATH={quoted(runtime_bin)}:"$PATH"', *(
        f'{name}() {{ [ -x {quoted(python)} ] || {{ printf "%s\\n" "Standards verifier Python is unavailable" >&2; return 1; }}; '
        f'{launch_check.HOST_VARIABLE}={name} {quoted(python)} -I -B {quoted(script)} --host {name} --project "$PWD" --target "$(command -v {name})" --require-session-start -- "$@"; }}'
        for name in map(str, Host)), PROFILE_END])


def wired_profile(text: str, block: str, newline: str) -> str:
    spans = block_spans(text, PROFILE_START, PROFILE_END)
    for (_, finish), (begin, _) in zip(spans, spans[1:]):
        if begin < finish:
            raise InstallError("Overlapping managed profile blocks")
    if not spans:
        prefix = text.rstrip()
        return (prefix + newline * 2 if prefix else "") + block + newline
    result = text[:spans[0][0]] + block
    cursor = spans[0][1]
    for begin, finish in spans[1:]:
        result += text[cursor:begin]
        cursor = without_trailing_newline_span(text, finish)
    return result + text[cursor:]


def shell_profile_updates(paths: list[Path], python: str, script: Path, loader: Path | None,
                          windows: bool) -> tuple[list[tuple[Path, str, str, bool]], list[str]]:
    targets = paths if windows else [path for path in paths if path.is_file()] or paths[:1]
    updates = []
    warnings = []
    for path in targets:
        try:
            current = read_profile(path)
        except UnicodeDecodeError as error:
            warnings.append(f"{path}: cannot decode as UTF-8 or UTF-16 ({error}); leaving this profile untouched")
            continue
        powershell = path.suffix.casefold() == ".ps1"
        text, encoding, bom = current if current is not None else ("", "utf-8", windows and powershell)
        if powershell:
            newline = "\r\n" if "\r\n" in text or ("\n" not in text and windows) else "\n"
            block = powershell_block(python, script, loader, newline)
        else:
            newline = "\n"
            block = posix_block(python, script)
        wired = wired_profile(text, block, newline)
        if wired != text:
            updates.append((path, wired, encoding, bom))
    return updates, warnings


def profile_loader(context: Context) -> Path | None:
    if not context.windows:
        return None
    return context.state_directory / "bin" / ".agents/agent/resources/shell/profile.ps1"


def launch_script(context: Context) -> Path:
    return context.state_directory / "bin" / ".agents" / "agent" / "runtime" / "startup_binding.py"


def profile_updates(context: Context) -> tuple[list[tuple[Path, str, str, bool]], list[str]]:
    return shell_profile_updates(profile_paths(context), plugin_autoupdate.console_python(), launch_script(context),
                                 profile_loader(context), context.windows)


def check_profile(context: Context) -> list[str]:
    updates, warnings = profile_updates(context)
    for warning in warnings:
        print(f"warning shell profiles: {warning}", file=sys.stderr)
    return [f"wire {path}" for path, _, _, _ in updates]


def apply_profile(context: Context) -> list[str]:
    updates, warnings = profile_updates(context)
    for warning in warnings:
        print(f"warning shell profiles: {warning}", file=sys.stderr)
    changes = []
    for path, text, encoding, bom in updates:
        write_profile(path, text, encoding, bom)
        changes.append(f"wired {path}")
    return changes


def same_path(left: str | Path, right: str | Path) -> bool:
    return os.path.normcase(os.path.abspath(str(left))) == os.path.normcase(os.path.abspath(str(right)))


def hidden_bash_paths(context: Context) -> tuple[Path, Path]:
    return (context.root / ".agents/agent/resources/shell/hidden-bash.c",
            context.state_directory / "bin" / "hidden-bash.exe")


def real_git_bash(context: Context, proxy: Path) -> str:
    environment = context.user_environment
    real = environment.get("TJ_AGENTS_REAL_GIT_BASH")
    if not real:
        existing = environment.get("CLAUDE_CODE_GIT_BASH_PATH")
        if existing and not same_path(existing, proxy):
            real = existing
    real = real or DEFAULT_GIT_BASH
    if same_path(real, proxy):
        raise InstallError("The real Git Bash path cannot point to the hidden proxy itself")
    if not Path(real).is_file():
        raise InstallError(f"Git Bash not found: {real}; set the user TJ_AGENTS_REAL_GIT_BASH variable to its path")
    return real


def hidden_bash_plan(context: Context) -> tuple[bool, dict[str, str]]:
    source, proxy = hidden_bash_paths(context)
    stale = not proxy.is_file() or proxy.stat().st_mtime < source.stat().st_mtime
    wanted = {"TJ_AGENTS_REAL_GIT_BASH": real_git_bash(context, proxy), "CLAUDE_CODE_GIT_BASH_PATH": str(proxy)}
    variables = {name: value for name, value in wanted.items() if context.user_environment.get(name) != value}
    return stale, variables


def check_hidden_bash(context: Context) -> list[str]:
    stale, variables = hidden_bash_plan(context)
    return (["build shell/hidden-bash.exe"] if stale else []) + [f"set user {name}" for name in variables]


def hidden_bash_stale_asides(proxy: Path) -> list[Path]:
    return sorted(proxy.parent.glob(f"{proxy.stem}.stale-*{proxy.suffix}"))


def install_hidden_bash(build: Path, proxy: Path) -> None:
    for stale in hidden_bash_stale_asides(proxy):
        try:
            stale.unlink()
        except OSError:
            pass
    aside = None
    if proxy.exists():
        aside = proxy.with_name(f"{proxy.stem}.stale-{secrets.token_hex(4)}{proxy.suffix}")
        os.replace(proxy, aside)
    try:
        os.replace(build, proxy)
    except OSError:
        if aside is not None:
            os.replace(aside, proxy)
        raise


def gui_flags(context: Context, compiler: str) -> list[str]:
    target = context.run([compiler, "-dumpmachine"], capture_output=True, text=True, encoding="utf-8",
                         errors="replace", timeout=INSTALLER_TIMEOUT_SECONDS, check=False)
    if target.returncode:
        raise InstallError(f"{compiler} -dumpmachine failed: {(target.stderr or target.stdout).strip()}")
    return ["-Wl,/subsystem:windows"] if "msvc" in target.stdout else ["-municode", "-mwindows"]


def build_hidden_bash(context: Context, real: str) -> None:
    source, proxy = hidden_bash_paths(context)
    proxy.parent.mkdir(parents=True, exist_ok=True)
    compiler = context.which("clang.exe") or context.which("gcc.exe")
    if not compiler:
        raise InstallError("clang.exe or gcc.exe is required to build the Windows GUI Bash proxy")
    build = proxy.with_name("hidden-bash.build.exe")
    try:
        compiled = context.run([compiler, "-O2", *gui_flags(context, compiler), str(source), "-o", str(build)],
                               capture_output=True, text=True, encoding="utf-8", errors="replace",
                               timeout=INSTALLER_TIMEOUT_SECONDS, check=False)
        if compiled.returncode or not build.is_file():
            raise InstallError(f"Failed to build {build}: {compiled.stderr.strip()}")
        probe = context.run([str(build), *HIDDEN_BASH_PROBE], capture_output=True, text=True, encoding="utf-8",
                            errors="replace", timeout=10, check=False, creationflags=plugin_autoupdate.NO_WINDOW,
                            env={**context.environ, "TJ_AGENTS_REAL_GIT_BASH": real})
        if probe.returncode != 73 or probe.stdout != "HIDDEN_BASH_OK":
            raise InstallError("The hidden Bash proxy did not preserve Bash output and exit status")
        install_hidden_bash(build, proxy)
    finally:
        build.unlink(missing_ok=True)


def apply_hidden_bash(context: Context) -> list[str]:
    stale, variables = hidden_bash_plan(context)
    changes = []
    if stale:
        build_hidden_bash(context, real_git_bash(context, hidden_bash_paths(context)[1]))
        changes.append("built shell/hidden-bash.exe")
    for name, value in variables.items():
        context.user_environment.set(name, value)
        changes.append(f"set user {name}={value}")
    if variables:
        context.user_environment.broadcast()
    return changes


def run_installer(context: Context, command: list[str]) -> subprocess.CompletedProcess:
    return context.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace",
                       timeout=INSTALLER_TIMEOUT_SECONDS, check=False, creationflags=plugin_autoupdate.NO_WINDOW)


def output_lines(result: subprocess.CompletedProcess) -> list[str]:
    return [line.strip() for line in (result.stdout or "").splitlines() if line.strip()]


def codex_agents_installer(context: Context) -> Path | None:
    config = plugin_autoupdate.load_toml(context.codex_home / "config.toml")
    roots = [root for _, declaration in plugin_autoupdate.declarations(config)
             for root in plugin_autoupdate.installed_plugin_roots("engineering", context.claude_home, declaration)]
    return plugin_autoupdate.newest_existing([root / "scripts" / "install-codex-agents.ps1" for root in roots])


def skip_codex_agents(context: Context) -> str | None:
    if not context.which("pwsh"):
        return "pwsh is not installed; install PowerShell 7 to deliver Codex profile agents"
    if not context.codex_home.is_dir():
        return f"Codex is not set up ({context.codex_home} is missing)"
    if codex_agents_installer(context) is None:
        return "no installed engineering plugin ships scripts/install-codex-agents.ps1"
    return None


def codex_agents_command(context: Context, flag: str) -> list[str]:
    return [context.which("pwsh"), "-NoProfile", "-NonInteractive", "-File", str(codex_agents_installer(context)),
            "-CodexHome", str(context.codex_home), flag]


def check_codex_agents(context: Context) -> list[str]:
    result = run_installer(context, codex_agents_command(context, "-Verify"))
    if result.returncode == 0:
        return []
    detail = (result.stderr or result.stdout).strip().splitlines()
    return [detail[-1] if detail else "Codex profile agents need delivery"]


def apply_codex_agents(context: Context) -> list[str]:
    result = run_installer(context, codex_agents_command(context, "-Apply"))
    if result.returncode:
        raise InstallError(f"Codex profile agent delivery failed: {(result.stderr or result.stdout).strip()}")
    lines = output_lines(result)
    return [lines[-1] if lines else "delivered Codex profile agents"]


def permission_sync(context: Context) -> ModuleType:
    directory = context.root / ".agents" / "agent" / "utility" / "plugins" / "scripts"
    path = directory / "harness_permissions_sync.py"
    if not path.is_file():
        raise InstallError(f"harness_permissions_sync.py is unavailable in {directory}")
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))
    spec = importlib.util.spec_from_file_location("tj_agents_harness_permissions_sync", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def permissions_environment(context: Context) -> dict:
    return {**context.environ, Host.CLAUDE.home_variable: str(context.claude_home), Host.CODEX.home_variable: str(context.codex_home),
            "AGENT_STATE_DIRECTORY": str(context.state_directory)}


def synchronize_permissions(context: Context, mode: str) -> list[str]:
    changes, warnings = permission_sync(context).synchronize(permissions_environment(context), mode)
    for warning in warnings:
        print(f"warning harness permissions: {warning}", file=sys.stderr)
    return changes


def enable_claude_autoupdate(config: Path, apply: bool = True) -> list[str]:
    settings = plugin_autoupdate.read_json(config / "settings.json")
    enabled = settings.get("enabledPlugins")
    if not isinstance(enabled, dict):
        return []
    wanted = {identity.rsplit("@", 1)[1] for identity, on in enabled.items() if on is True and "@" in identity}
    path = config / "plugins" / "known_marketplaces.json"
    known = plugin_autoupdate.read_json(path)
    changed = sorted(name for name in wanted
                     if isinstance(known.get(name), dict) and known[name].get("autoUpdate") is not True)
    if changed and apply:
        for name in changed:
            known[name]["autoUpdate"] = True
        plugin_autoupdate.write_json(path, known)
    return changed


def remote_topic(api: Callable) -> str | None:
    values = api([f"repos/{TOPIC_REPOSITORY}/actions/variables?per_page=100"])
    return next((item["value"] for item in values["variables"] if item["name"] == machine_events.TOPIC_VARIABLE), None)


def configure_topic(directory: Path, api: Callable = machine_events.gh) -> str:
    endpoint = f"repos/{TOPIC_REPOSITORY}/actions/variables"
    remote = remote_topic(api)
    saved = plugin_autoupdate.read_json(directory / "machine-events.json").get("topic")
    topic = machine_events.topic_value(remote or saved or secrets.token_hex(24))
    if not remote:
        api(["--method", "POST", endpoint, "--input", "-"],
            {"name": machine_events.TOPIC_VARIABLE, "value": topic})
    if saved != topic:
        plugin_autoupdate.write_json(directory / "machine-events.json", {"topic": topic})
    return topic


def set_topic_secret(repository: str, topic: str) -> None:
    machine_events.github_cli(["secret", "set", machine_events.TOPIC_VARIABLE, "--repo", repository], input=topic)


def configure_publishers(topic: str, repositories, api: Callable = machine_events.gh,
                         secret: Callable = set_topic_secret, dry_run: bool = False) -> int:
    changed = 0
    for repository in repositories:
        if repository.get("isArchived") or repository.get("isFork"):
            continue
        name = repository["nameWithOwner"]
        if not machine_events.valid_repository(name):
            raise ValueError("Unsupported publisher repository")
        endpoint = f"repos/{name}/actions/variables"
        values = api([endpoint + "?per_page=100"])
        existing = next((item for item in values["variables"]
                         if item["name"] == machine_events.TOPIC_VARIABLE), None)
        different = existing is None or existing["value"] != topic
        stored = api([f"repos/{name}/actions/secrets?per_page=100"])
        saved = next((item for item in stored["secrets"]
                      if item["name"] == machine_events.TOPIC_VARIABLE), None)
        stale = bool(existing and saved and existing.get("updated_at", "") > saved.get("updated_at", ""))
        if dry_run:
            changed += int(different or saved is None or stale)
            continue
        if different:
            if existing:
                endpoint += "/" + machine_events.TOPIC_VARIABLE
            api(["--method", "PATCH" if existing else "POST", endpoint, "--input", "-"],
                {"name": machine_events.TOPIC_VARIABLE, "value": topic})
        if different or saved is None or stale:
            secret(name, topic)
            changed += 1
    return changed


def publisher_repositories(context: Context) -> list:
    if "repositories" not in context.cache:
        context.cache["repositories"] = list(machine_events.catch_up_states(api=machine_events.gh))
    return context.cache["repositories"]


def check_channel(context: Context) -> list[str]:
    directory = context.state_directory
    remote = remote_topic(machine_events.gh)
    saved = plugin_autoupdate.read_json(directory / "machine-events.json").get("topic")
    pending = [] if remote else [f"create the MACHINE_EVENTS_TOPIC variable on {TOPIC_REPOSITORY}"]
    if remote and saved != remote:
        pending.append(f"save the shared topic in {directory / 'machine-events.json'}")
    topic = remote or saved
    if not topic:
        return pending + ["configure the repository publishers"]
    stale = configure_publishers(topic, publisher_repositories(context), api=machine_events.gh, dry_run=True)
    if stale:
        pending.append(f"configure {stale} repository publishers")
    return pending


def apply_channel(context: Context) -> list[str]:
    topic = configure_topic(context.state_directory, api=machine_events.gh)
    configured = configure_publishers(topic, publisher_repositories(context), api=machine_events.gh,
                                      secret=set_topic_secret)
    return [f"channel configured for {configured} repository publishers"]


def check_autoupdate(context: Context) -> list[str]:
    return [f"enable auto-update for Claude marketplace {name}"
            for name in enable_claude_autoupdate(context.claude_home, apply=False)]


def apply_autoupdate(context: Context) -> list[str]:
    return [f"Claude marketplace now auto-updates: {name}" for name in enable_claude_autoupdate(context.claude_home)]


def runtime_receipt(root: Path) -> dict:
    try:
        contents = runtime_delivery.closure(root, list(RUNTIME_EXPORTS))
    except (OSError, runtime_delivery.DeliveryError) as error:
        raise InstallError(f"the Agent runtime in {root} is unavailable: {error}") from error
    digest = runtime_delivery.digest(contents)
    return {"repository": runtime_delivery.REPOSITORY, "revision": f"local:{digest}", "exports": list(RUNTIME_EXPORTS),
            "sha256": digest, "files": {name: hashlib.sha256(body).hexdigest() for name, body in contents.items()}}


def install_runtime(directory: Path, root: Path) -> Path:
    errors = runtime_delivery.apply(directory, root, runtime_receipt(root))
    if errors:
        raise InstallError("; ".join(errors))
    return directory / ".agents" / "agent" / "runtime" / "startup_binding.py"


def check_runtime(context: Context) -> list[str]:
    directory = context.state_directory / "bin"
    changes = [f"install Agent runtime: {error}"
               for error in runtime_delivery.check(directory, context.root, runtime_receipt(context.root))]
    if not context.windows:
        alias = python_alias_status(context, directory)
        if alias:
            changes.append(alias)
    return changes


PYTHON_ALIAS_MARKER = '# tj-agents-managed-python-alias '


def python_alias(interpreter: str | None = None) -> str:
    interpreter = sys.executable if interpreter is None else interpreter
    marker = json.dumps({'interpreter': interpreter}, separators=(',', ':'), sort_keys=True)
    return '#!/bin/sh\n' + PYTHON_ALIAS_MARKER + marker + '\nexec ' + shlex.quote(interpreter) + ' "$@"\n'


def managed_python_alias(value: str) -> str | None:
    prefix = '#!/bin/sh\n' + PYTHON_ALIAS_MARKER
    if not value.startswith(prefix):
        return None
    marker, separator, _ = value[len(prefix):].partition('\n')
    if not separator:
        return None
    try:
        metadata = json.loads(marker)
    except json.JSONDecodeError:
        return None
    interpreter = metadata.get('interpreter') if isinstance(metadata, dict) and set(metadata) == {'interpreter'} else None
    if not isinstance(interpreter, str) or not interpreter or python_alias(interpreter) != value:
        return None
    return interpreter


def python_alias_path(context: Context, directory: Path) -> Path:
    for path in (context.state_directory, directory):
        if os.path.lexists(path) and runtime_delivery.is_reparse(path):
            raise InstallError(f"Claude hook interpreter parent is a link: {path}")
    alias = directory / 'python'
    if os.path.lexists(alias) and runtime_delivery.is_reparse(alias):
        raise InstallError(f"Claude hook interpreter is a link: {alias}")
    if alias.exists():
        if not alias.is_file():
            raise InstallError(f"Claude hook interpreter is not a file: {alias}")
    return alias


def python_alias_status(context: Context, directory: Path) -> str | None:
    alias = python_alias_path(context, directory)
    if not alias.exists():
        return f'install Claude hook interpreter {alias}'
    value = alias.read_text(encoding='utf-8')
    if managed_python_alias(value) is None:
        return f'refuse unowned Claude hook interpreter {alias}'
    if value != python_alias():
        return f'install Claude hook interpreter {alias}'
    if os.name != 'nt' and not os.access(alias, os.X_OK):
        return f'install Claude hook interpreter {alias}'
    return None


def install_python_alias(context: Context, directory: Path) -> None:
    alias = python_alias_path(context, directory)
    status = python_alias_status(context, directory)
    if status is None:
        return
    if status.startswith('refuse '):
        raise InstallError(status)
    if not alias.exists():
        try:
            with alias.open('x', encoding='utf-8', newline='\n') as stream:
                stream.write(python_alias())
        except FileExistsError:
            return install_python_alias(context, directory)
    value = alias.read_text(encoding='utf-8')
    if managed_python_alias(value) is None:
        raise InstallError(f'refuse unowned Claude hook interpreter {alias}')
    if value != python_alias():
        temporary = alias.with_name(f'.{alias.name}.{secrets.token_hex(12)}.tmp')
        created = False
        try:
            with temporary.open('x', encoding='utf-8', newline='\n') as stream:
                created = True
                stream.write(python_alias())
            alias = python_alias_path(context, directory)
            if alias.read_text(encoding='utf-8') != value:
                raise InstallError(f'refuse changed Claude hook interpreter {alias}')
            os.replace(temporary, alias)
        finally:
            if created and temporary.exists():
                temporary.unlink()
    if alias.read_text(encoding='utf-8') != python_alias():
        raise InstallError(f'refuse changed Claude hook interpreter {alias}')
    alias.chmod(0o755)


def apply_runtime(context: Context) -> list[str]:
    directory = context.state_directory / "bin"
    install_runtime(directory, context.root)
    if not context.windows:
        install_python_alias(context, directory)
    return [f"installed the Agent runtime in {directory}"]


def stale_repositories(context: Context) -> list[str]:
    state = plugin_autoupdate.read_json(context.state_directory / "machine-events-state.json")
    stale = []
    for current in publisher_repositories(context):
        name = current.get("nameWithOwner")
        branch = current.get("defaultBranchRef")
        if current.get("isArchived") or current.get("isFork") or not branch or not machine_events.valid_repository(name):
            continue
        saved = state.get(name)
        if not isinstance(saved, dict) or saved.get("revision") != branch["target"]["oid"]:
            stale.append(name)
    return stale


def check_catch_up(context: Context) -> list[str]:
    return [f"catch up {name}" for name in stale_repositories(context)]


def apply_catch_up(context: Context) -> list[str]:
    changes = []
    try:
        if launch_check.mark_current():
            changes.append("launch checks start from the newest published event")
    except (OSError, ValueError) as error:
        changes.append(f"event channel unreachable ({type(error).__name__}); the first launch replays recent events")
    if machine_events.main([]):
        raise InstallError("catch-up left failures; see machine-events.log")
    return changes + ["plugins and merge policies are current"]


def check_preconditions(context: Context) -> list[str]:
    machine_events.require_tommy(api=machine_events.gh)
    return []


def windows_only(context: Context) -> str | None:
    return None if context.windows else WINDOWS_ONLY


def nothing(context: Context) -> list[str]:
    return []


def codex_config_path(context: Context) -> Path:
    return context.codex_home / "config.toml"


def native_permissions_plan(context: Context) -> dict[Path, str]:
    mode = host_permissions.permissions(plugin_setup_project(context))
    return host_permissions.plan(context.claude_home / "settings.json", codex_config_path(context), mode)


def check_native_permissions(context: Context) -> list[str]:
    return [f"update native permission defaults in {path}" for path in native_permissions_plan(context)]


def apply_native_permissions(context: Context) -> list[str]:
    targets = native_permissions_plan(context)
    desktop = codex_config_path(context).parent / ".codex-global-state.json"
    if desktop in targets:
        if host_permissions.desktop_running():
            raise InstallError("Close Codex Desktop, run install --step 'native permission defaults' "
                               "from a terminal, then reopen the app to load the configured defaults")
        targets = native_permissions_plan(context)
    for path, text in targets.items():
        write_profile(path, text, "utf-8", False)
    return [f"updated native permission defaults in {path}" for path in targets]


def plugin_setup_project(context: Context) -> Path:
    return context.project or Path.cwd().resolve()


def plugin_setup_executables(context: Context) -> dict[Host, str | None]:
    return {host: context.which(host.value) for host in Host}


def check_plugin_setup(context: Context) -> list[str]:
    return required_plugins.check_setup(plugin_setup_project(context), tuple(Host), plugin_setup_executables(context))


def apply_plugin_setup(context: Context) -> list[str]:
    errors = required_plugins.converge_setup(plugin_setup_project(context), tuple(Host), plugin_setup_executables(context), context.onboard)
    if errors:
        raise InstallError("; ".join(errors))
    return []


STEPS = (
    Step("preconditions", check_preconditions, nothing, required=True),
    Step("native permission defaults", check_native_permissions, apply_native_permissions),
    Step("event channel and publishers", check_channel, apply_channel),
    Step("Claude marketplace auto-update", check_autoupdate, apply_autoupdate),
    Step("launch-check runtime", check_runtime, apply_runtime),
    Step("shell profiles", check_profile, apply_profile),
    Step("hidden Git Bash for Claude", check_hidden_bash, apply_hidden_bash, windows_only),
    Step("Codex profile agents", check_codex_agents, apply_codex_agents, skip_codex_agents),
    Step("plugin and merge-policy catch-up", check_catch_up, apply_catch_up),
    Step("harness permissions", lambda context: synchronize_permissions(context, "check"),
         lambda context: synchronize_permissions(context, "apply")),
    Step("plugin setup", check_plugin_setup, apply_plugin_setup),
)


def run_steps(steps, context: Context, check_only: bool, out=sys.stdout, err=sys.stderr) -> int:
    print(f"package root: {context.root}", file=out)
    drift = False
    failed = False
    failed_steps = set()
    for step in steps:
        if step.name == "plugin setup" and failed_steps & {"plugin and merge-policy catch-up", "harness permissions"}:
            print("n/a    plugin setup: prerequisite install step failed", file=out)
            continue
        try:
            reason = step.skip(context)
            if reason:
                print(f"n/a    {step.name}: {reason}", file=out)
                continue
            pending = step.check(context)
            if not pending:
                print(f"ok     {step.name}", file=out)
                continue
            if check_only:
                drift = True
                for item in pending:
                    print(f"drift  {step.name}: {item}", file=out)
                continue
            for change in step.apply(context) or pending:
                print(f"apply  {step.name}: {change}", file=out)
        except Exception as error:  # noqa: BLE001
            print(f"failed {step.name}: {error}", file=err)
            failed = True
            failed_steps.add(step.name)
            if step.required:
                return 2
    if failed:
        return 2
    return 1 if drift else 0


def selected_steps(names: set[str]) -> tuple[Step, ...]:
    selected = set(names)
    if "shell profiles" in selected:
        selected.add("launch-check runtime")
    if "plugin setup" in selected:
        selected.update({"plugin and merge-policy catch-up", "harness permissions"})
    return tuple(step for step in STEPS if not selected or step.required or step.name in selected)


def repair(name: str, project: Path) -> list[str]:
    step = next((step for step in STEPS if step.name == name), None)
    if step is None:
        raise InstallError(f"unknown install step {name!r}")
    context = machine_context()
    context.project = project
    for required in STEPS:
        if required.required:
            required.check(context)
    return step.apply(context)


def machine_context() -> Context:
    home = Path.home()
    windows = os.name == "nt"
    return Context(
        home=home,
        project=Path.cwd().resolve(),
        environ=dict(os.environ),
        windows=windows,
        documents=documents_directory(home) if windows else None,
        user_environment=WindowsUserEnvironment() if windows else None,
    )


def main(argv: list[str] | None = None) -> int:
    if sys.version_info < (3, 11):
        print("install requires Python 3.11 or newer", file=sys.stderr)
        return 2
    arguments = list(argv if argv is not None else sys.argv[1:])
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--check", action="store_true", help="report drift, write nothing, exit 1 on drift")
    modes.add_argument("--onboard", action="store_true", help="allow explicitly requested plugin enrollment")
    parser.add_argument("--step", action="append", choices=[step.name for step in STEPS],
                        help="run named steps with their dependencies and required preconditions; repeat to select several")
    parsed = parser.parse_args(arguments)
    context = machine_context()
    context.onboard = parsed.onboard
    return run_steps(selected_steps(set(parsed.step or ())), context, parsed.check)


if __name__ == "__main__":
    raise SystemExit(main())
