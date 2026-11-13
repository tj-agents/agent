#!/usr/bin/env python3
"""Install and converge this machine's tj-agents environment (Python 3.12+).

A full install first fetches the marketplaces and brings each installed host's plugins to their pins,
then hands the rest of the install to the Agent package that update converged to, so `tj install`
always upgrades itself. It then runs an ordered list of steps, each with a check and an apply. A step whose check reports no drift is
left alone, so rerunning converges and writes nothing once the machine is current. `--check` reports
drift without writing anything and exits 1 when something would change. Installed launch paths use
the managed package closure, independently of source checkouts.
"""

from __future__ import annotations

import argparse
import codecs
import contextlib
from dataclasses import dataclass, field
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import secrets
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from types import ModuleType
from typing import Callable

RUNTIME = Path(__file__).resolve().parent
ROOT = RUNTIME.parents[2]
sys.path.insert(0, str(RUNTIME))

from tj_agent.host import Host
from codex import daemon as codex_daemon
from codex import hook_trust as codex_hook_trust
from codex import installed as codex_installed
from codex import launch as codex_launch
from codex import powershell as codex_powershell
from codex import projects as codex_projects
from codex import user_plugins as codex_user_plugins
import host_permissions
import jobs
import machine_state
import plugin_copies
import plugin_update
import required_plugins
import retired_state
import runtime_delivery
import native_runtime
import agent_store
import work_items_install
from tj_agent import content
from tj_cli import handy_install, process, worktrunk_install
from tree_digest import Digests

INSTALLER_TIMEOUT_SECONDS = 600
LOCKED = "(os error 5)"
LOCKED_ATTEMPTS = 3
LOCKED_RETRY_SECONDS = 2
RUNTIME_EXPORTS = (
    ".agents/agent/runtime",
    ".agents/agent/resources",
    ".agents/agent/utility/plugins/scripts",
)
LIFECYCLE_COMMANDS = {
    "start": "tj_start.py",
    "status": "tj_status.py",
    "recover": "tj_recover.py",
    "install": "install.py",
    "prepare": "tj_prepare.py",
    "session": "tj_session.py",
}
PROFILE_START = "# >>> tj-agents >>>"
PROFILE_END = "# <<< tj-agents <<<"
PROFILE_BACKUP = ".pre-tj-agents.bak"
PROFILE_NAMES = ("WindowsPowerShell", "PowerShell")
DEFAULT_GIT_BASH = r"C:\Program Files\Git\bin\bash.exe"
HIDDEN_BASH_PROBE = ("-c", "printf HIDDEN_BASH_OK; exit 73")
WINDOWS_ONLY = "Windows only: shell/ is the Windows PowerShell profile and its tools"


def read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps(value, indent=2, ensure_ascii=False) + "\n")
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def load_toml(path: Path) -> dict:
    try:
        import tomllib
    except ImportError:
        return {}

    try:
        with path.open("rb") as stream:
            return tomllib.load(stream)
    except (OSError, tomllib.TOMLDecodeError):
        return {}


def declarations(config: dict) -> list[tuple[Path, dict]]:
    found = [(Path.home(), config)]
    user_file = (Host.CODEX.configuration_root / "config.toml").resolve()
    for project in codex_projects.trusted(config):
        path = project / ".codex" / "config.toml"
        if path.resolve() != user_file:
            found.append((project, load_toml(path)))
    return found


def installed_plugin_roots(
    plugin: str, claude_config: Path, codex_config: dict
) -> list[Path]:
    roots = []
    prefix = plugin + "@"
    settings = read_json(claude_config / "settings.json").get("enabledPlugins")
    installed = read_json(claude_config / "plugins" / "installed_plugins.json").get(
        "plugins"
    )
    if isinstance(settings, dict) and isinstance(installed, dict):
        for identity, on in settings.items():
            entries = installed.get(identity)
            if on is True and identity.startswith(prefix) and isinstance(entries, list):
                roots.extend(
                    Path(entry["installPath"])
                    for entry in entries
                    if isinstance(entry, dict)
                    and isinstance(entry.get("installPath"), str)
                )
    plugins = codex_config.get("plugins")
    if isinstance(plugins, dict):
        for identity, settings in plugins.items():
            if (
                identity.startswith(prefix)
                and isinstance(settings, dict)
                and settings.get("enabled") is True
            ):
                roots.append(
                    Host.CODEX.configuration_root
                    / "plugins"
                    / "cache"
                    / identity.split("@", 1)[1]
                    / plugin
                    / "local"
                )
    return roots


def newest_existing(paths: list[Path]) -> Path | None:
    existing = [path for path in paths if path.is_file()]
    return max(existing, key=lambda path: path.stat().st_mtime) if existing else None


def console_python() -> str:
    if sys.version_info < (3, 12):
        raise InstallError("the installed runtime requires Python 3.12 or newer")
    base = getattr(sys, "_base_executable", None)
    if not isinstance(base, str) or not base or not Path(base).is_absolute():
        raise InstallError("an absolute base Python interpreter is required")
    executable = Path(base).resolve()
    if executable.name.lower() == "pythonw.exe":
        executable = executable.with_name("python.exe")
    if not executable.is_file():
        raise InstallError(f"the base Python interpreter is unavailable: {executable}")
    return str(executable)


class InstallError(RuntimeError):
    pass


@dataclass
class Context:
    home: Path
    environ: dict
    windows: bool
    run: Callable = process.run
    which: Callable = shutil.which
    sleep: Callable = time.sleep
    documents: Path | None = None
    user_environment: object = None
    root: Path = ROOT
    project: Path | None = None
    cache: dict = field(default_factory=dict)
    onboard: bool = False
    failures: list[str] = field(default_factory=list)

    def path_from(self, variable: str, default: Path) -> Path:
        configured = self.environ.get(variable)
        return Path(configured).expanduser() if configured else default

    @property
    def state_directory(self) -> Path:
        return self.path_from("AGENT_STATE_DIRECTORY", self.home / ".agents-state")

    @property
    def claude_home(self) -> Path:
        return self.path_from(
            Host.CLAUDE.home_variable, self.home / Host.CLAUDE.home_directory
        )

    @property
    def codex_home(self) -> Path:
        return self.path_from(
            Host.CODEX.home_variable, self.home / Host.CODEX.home_directory
        )


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

    folder = Guid.from_buffer_copy(
        uuid.UUID("{FDD39AD0-238F-46AF-ADB4-6C85480369C7}").bytes_le
    )
    path = ctypes.c_wchar_p()
    if (
        ctypes.windll.shell32.SHGetKnownFolderPath(
            ctypes.byref(folder), 0, None, ctypes.byref(path)
        )
        != 0
    ):
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

        with winreg.CreateKeyEx(
            winreg.HKEY_CURRENT_USER,
            "Environment",
            0,
            winreg.KEY_SET_VALUE | winreg.KEY_QUERY_VALUE,
        ) as key:
            try:
                _, kind = winreg.QueryValueEx(key, name)
            except FileNotFoundError:
                kind = winreg.REG_SZ
            winreg.SetValueEx(
                key,
                name,
                0,
                kind
                if kind in (winreg.REG_SZ, winreg.REG_EXPAND_SZ)
                else winreg.REG_SZ,
                value,
            )

    def broadcast(self) -> None:
        import ctypes
        from ctypes import wintypes

        send = ctypes.windll.user32.SendMessageTimeoutW
        send.argtypes = [
            wintypes.HWND,
            wintypes.UINT,
            wintypes.WPARAM,
            wintypes.LPCWSTR,
            wintypes.UINT,
            wintypes.UINT,
            ctypes.POINTER(ctypes.c_size_t),
        ]
        send(
            wintypes.HWND(0xFFFF),
            0x001A,
            0,
            "Environment",
            0x0002,
            5000,
            ctypes.byref(ctypes.c_size_t()),
        )


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
            return raw[len(bom) :].decode(encoding), encoding, True
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
    if text[finish : finish + 2] == "\r\n":
        return finish + 2
    if text[finish : finish + 1] == "\n":
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
        return [
            documents / name / "Microsoft.PowerShell_profile.ps1"
            for name in PROFILE_NAMES
        ]
    return [
        context.home / ".bashrc",
        context.home / ".zshrc",
        context.home / ".config" / "powershell" / "Microsoft.PowerShell_profile.ps1",
    ]


def powershell_block(loader: Path | None, newline: str) -> str:
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
    return newline.join([*lines, PROFILE_END])


def posix_block(runtime_bin: Path) -> str:
    return "\n".join(
        [
            PROFILE_START,
            f'export PATH={shlex.quote(str(runtime_bin))}:"$PATH"',
            PROFILE_END,
        ]
    )


def wired_profile(text: str, block: str, newline: str) -> str:
    spans = block_spans(text, PROFILE_START, PROFILE_END)
    for (_, finish), (begin, _) in zip(spans, spans[1:]):
        if begin < finish:
            raise InstallError("Overlapping managed profile blocks")
    if not spans:
        prefix = text.rstrip()
        return (prefix + newline * 2 if prefix else "") + block + newline
    result = text[: spans[0][0]] + block
    cursor = spans[0][1]
    for begin, finish in spans[1:]:
        result += text[cursor:begin]
        cursor = without_trailing_newline_span(text, finish)
    return result + text[cursor:]


def shell_profile_updates(
    paths: list[Path], runtime_bin: Path, loader: Path | None, windows: bool
) -> tuple[list[tuple[Path, str, str, bool]], list[str]]:
    targets = (
        paths if windows else [path for path in paths if path.is_file()] or paths[:1]
    )
    updates = []
    warnings = []
    for path in targets:
        try:
            current = read_profile(path)
        except UnicodeDecodeError as error:
            warnings.append(
                f"{path}: cannot decode as UTF-8 or UTF-16 ({error}); leaving this profile untouched"
            )
            continue
        powershell = path.suffix.casefold() == ".ps1"
        text, encoding, bom = (
            current if current is not None else ("", "utf-8", windows and powershell)
        )
        if powershell:
            newline = (
                "\r\n" if "\r\n" in text or ("\n" not in text and windows) else "\n"
            )
            block = powershell_block(loader, newline)
        else:
            newline = "\n"
            block = posix_block(runtime_bin)
        wired = wired_profile(text, block, newline)
        if wired != text:
            updates.append((path, wired, encoding, bom))
    return updates, warnings


def profile_loader(context: Context) -> Path | None:
    if not context.windows:
        return None
    return context.state_directory / "bin" / ".agents/agent/resources/shell/profile.ps1"


def runtime_bin(context: Context) -> Path:
    return context.state_directory / "bin"


def profile_updates(
    context: Context,
) -> tuple[list[tuple[Path, str, str, bool]], list[str]]:
    return shell_profile_updates(
        profile_paths(context),
        runtime_bin(context),
        profile_loader(context),
        context.windows,
    )


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
    return os.path.normcase(os.path.abspath(str(left))) == os.path.normcase(
        os.path.abspath(str(right))
    )


def hidden_bash_paths(context: Context) -> tuple[Path, Path]:
    return (
        context.root / ".agents/agent/resources/shell/hidden-bash.c",
        context.state_directory / "bin" / "hidden-bash.exe",
    )


def real_git_bash(context: Context, proxy: Path) -> str:
    environment = context.user_environment
    real = environment.get("TJ_AGENTS_REAL_GIT_BASH")
    if not real:
        existing = environment.get("CLAUDE_CODE_GIT_BASH_PATH")
        if existing and not same_path(existing, proxy):
            real = existing
    real = real or DEFAULT_GIT_BASH
    if same_path(real, proxy):
        raise InstallError(
            "The real Git Bash path cannot point to the hidden proxy itself"
        )
    if not Path(real).is_file():
        raise InstallError(
            f"Git Bash not found: {real}; set the user TJ_AGENTS_REAL_GIT_BASH variable to its path"
        )
    return real


def hidden_bash_inputs(context: Context, real: str) -> dict[str, str]:
    source, _ = hidden_bash_paths(context)
    compiler = context.which("clang.exe") or context.which("gcc.exe") or ""
    return {
        "source": hashlib.sha256(source.read_bytes()).hexdigest(),
        "compiler": compiler,
        "real": real,
    }


def hidden_bash_receipt(proxy: Path) -> Path:
    return proxy.with_suffix(".json")


def hidden_bash_current(context: Context, proxy: Path, real: str) -> bool:
    receipt = read_json(hidden_bash_receipt(proxy))
    return (
        proxy.is_file()
        and receipt.get("inputs") == hidden_bash_inputs(context, real)
        and receipt.get("proxy") == hashlib.sha256(proxy.read_bytes()).hexdigest()
    )


def hidden_bash_plan(context: Context) -> tuple[bool, dict[str, str]]:
    _, proxy = hidden_bash_paths(context)
    real = real_git_bash(context, proxy)
    stale = not hidden_bash_current(context, proxy, real)
    wanted = {"TJ_AGENTS_REAL_GIT_BASH": real, "CLAUDE_CODE_GIT_BASH_PATH": str(proxy)}
    variables = {
        name: value
        for name, value in wanted.items()
        if context.user_environment.get(name) != value
    }
    return stale, variables


def global_git_config(context: Context) -> Path:
    return context.path_from("GIT_CONFIG_GLOBAL", context.home / ".gitconfig")


def git_longpaths(context: Context) -> bool:
    result = context.run(
        [
            "git",
            "config",
            "--file",
            str(global_git_config(context)),
            "--type=bool",
            "--get",
            "core.longpaths",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        check=False,
    )
    return result.returncode == 0 and result.stdout.strip() == "true"


def check_git_longpaths(context: Context) -> list[str]:
    return (
        []
        if git_longpaths(context)
        else [f"set core.longpaths in {global_git_config(context)}"]
    )


def apply_git_longpaths(context: Context) -> list[str]:
    if git_longpaths(context):
        return []
    context.run(
        [
            "git",
            "config",
            "--file",
            str(global_git_config(context)),
            "core.longpaths",
            "true",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        check=True,
    )
    return [f"core.longpaths is true in {global_git_config(context)}"]


def check_hidden_bash(context: Context) -> list[str]:
    stale, variables = hidden_bash_plan(context)
    return (["build shell/hidden-bash.exe"] if stale else []) + [
        f"set user {name}" for name in variables
    ]


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
        aside = proxy.with_name(
            f"{proxy.stem}.stale-{secrets.token_hex(4)}{proxy.suffix}"
        )
        os.replace(proxy, aside)
    try:
        os.replace(build, proxy)
    except OSError:
        if aside is not None:
            os.replace(aside, proxy)
        raise


def gui_flags(context: Context, compiler: str) -> list[str]:
    target = context.run(
        [compiler, "-dumpmachine"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=INSTALLER_TIMEOUT_SECONDS,
        check=False,
    )
    if target.returncode:
        raise InstallError(
            f"{compiler} -dumpmachine failed: {(target.stderr or target.stdout).strip()}"
        )
    return (
        ["-Wl,/subsystem:windows"]
        if "msvc" in target.stdout
        else ["-municode", "-mwindows"]
    )


def build_hidden_bash(context: Context, real: str) -> None:
    source, proxy = hidden_bash_paths(context)
    proxy.parent.mkdir(parents=True, exist_ok=True)
    compiler = context.which("clang.exe") or context.which("gcc.exe")
    if not compiler:
        raise InstallError(
            "clang.exe or gcc.exe is required to build the Windows GUI Bash proxy"
        )
    build = proxy.with_name("hidden-bash.build.exe")
    try:
        compiled = context.run(
            [
                compiler,
                "-O2",
                *gui_flags(context, compiler),
                str(source),
                "-o",
                str(build),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=INSTALLER_TIMEOUT_SECONDS,
            check=False,
        )
        if compiled.returncode or not build.is_file():
            raise InstallError(f"Failed to build {build}: {compiled.stderr.strip()}")
        probe = context.run(
            [str(build), *HIDDEN_BASH_PROBE],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
            check=False,
            env={**context.environ, "TJ_AGENTS_REAL_GIT_BASH": real},
        )
        if probe.returncode != 73 or probe.stdout != "HIDDEN_BASH_OK":
            raise InstallError(
                "The hidden Bash proxy did not preserve Bash output and exit status"
            )
        install_hidden_bash(build, proxy)
        write_json(
            hidden_bash_receipt(proxy),
            {
                "inputs": hidden_bash_inputs(context, real),
                "proxy": hashlib.sha256(proxy.read_bytes()).hexdigest(),
            },
        )
    finally:
        build.unlink(missing_ok=True)


def apply_hidden_bash(context: Context) -> list[str]:
    stale, variables = hidden_bash_plan(context)
    changes = []
    if stale:
        build_hidden_bash(
            context, real_git_bash(context, hidden_bash_paths(context)[1])
        )
        changes.append("built shell/hidden-bash.exe")
    for name, value in variables.items():
        context.user_environment.set(name, value)
        changes.append(f"set user {name}={value}")
    if variables:
        context.user_environment.broadcast()
    return changes


def run_installer(context: Context, command: list[str]) -> subprocess.CompletedProcess:
    for attempt in range(LOCKED_ATTEMPTS):
        result = context.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=INSTALLER_TIMEOUT_SECONDS,
            check=False,
        )
        if not result.returncode or LOCKED not in f"{result.stdout}{result.stderr}":
            return result
        if attempt + 1 < LOCKED_ATTEMPTS:
            context.sleep(LOCKED_RETRY_SECONDS)
    return result


def output_lines(result: subprocess.CompletedProcess) -> list[str]:
    return [line.strip() for line in (result.stdout or "").splitlines() if line.strip()]


def codex_agents_installer(context: Context) -> Path | None:
    config = load_toml(context.codex_home / "config.toml")
    roots = [
        root
        for _, declaration in declarations(config)
        for root in installed_plugin_roots(
            "engineering", context.claude_home, declaration
        )
    ]
    return newest_existing(
        [root / "scripts" / "install-codex-agents.ps1" for root in roots]
    )


def skip_codex_agents(context: Context) -> str | None:
    user_path = (
        context.user_environment.get("Path")
        if context.user_environment is not None
        else None
    )
    if not codex_powershell.executable(
        context.which("pwsh"), context.environ, user_path, context.windows
    ):
        if context.windows:
            return "unpackaged PowerShell 7 is required; add its directory to process or User PATH to deliver Codex profile agents"
        return "pwsh is not installed; install PowerShell 7 to deliver Codex profile agents"
    if not context.codex_home.is_dir():
        return f"Codex is not set up ({context.codex_home} is missing)"
    if codex_agents_installer(context) is None:
        return "no installed engineering plugin ships scripts/install-codex-agents.ps1"
    return None


def codex_agents_command(context: Context, flag: str) -> list[str]:
    user_path = (
        context.user_environment.get("Path")
        if context.user_environment is not None
        else None
    )
    executable = codex_powershell.executable(
        context.which("pwsh"), context.environ, user_path, context.windows
    )
    if executable is None:
        raise InstallError(
            "PowerShell 7 is unavailable for Codex profile agent delivery"
        )
    return [
        executable,
        "-NoProfile",
        "-NonInteractive",
        "-File",
        str(codex_agents_installer(context)),
        "-CodexHome",
        str(context.codex_home),
        flag,
    ]


def check_codex_agents(context: Context) -> list[str]:
    result = run_installer(context, codex_agents_command(context, "-Verify"))
    if result.returncode == 0:
        return []
    detail = (result.stderr or result.stdout).strip().splitlines()
    return [detail[-1] if detail else "Codex profile agents need delivery"]


def apply_codex_agents(context: Context) -> list[str]:
    result = run_installer(context, codex_agents_command(context, "-Apply"))
    if result.returncode:
        raise InstallError(
            f"Codex profile agent delivery failed: {(result.stderr or result.stdout).strip()}"
        )
    lines = output_lines(result)
    return [lines[-1] if lines else "delivered Codex profile agents"]


def permission_sync(context: Context) -> ModuleType:
    directory = context.root / ".agents" / "agent" / "utility" / "plugins" / "scripts"
    path = directory / "harness_permissions_sync.py"
    if not path.is_file():
        raise InstallError(f"harness_permissions_sync.py is unavailable in {directory}")
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))
    spec = importlib.util.spec_from_file_location(
        "tj_agents_harness_permissions_sync", path
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def permissions_environment(context: Context) -> dict:
    return {
        **context.environ,
        Host.CLAUDE.home_variable: str(context.claude_home),
        Host.CODEX.home_variable: str(context.codex_home),
        "AGENT_STATE_DIRECTORY": str(context.state_directory),
    }


def synchronize_permissions(context: Context, mode: str) -> list[str]:
    changes, warnings = permission_sync(context).synchronize(
        permissions_environment(context), mode
    )
    for warning in warnings:
        print(f"warning harness permissions: {warning}", file=sys.stderr)
    return changes


def runtime_receipt(root: Path) -> dict:
    try:
        contents = runtime_delivery.closure(root, list(RUNTIME_EXPORTS))
    except (OSError, runtime_delivery.DeliveryError) as error:
        raise InstallError(
            f"the Agent runtime in {root} is unavailable: {error}"
        ) from error
    digest = content.digest(contents)
    return {
        "repository": runtime_delivery.REPOSITORY,
        "revision": f"local:{digest}",
        "exports": list(RUNTIME_EXPORTS),
        "sha256": digest,
        "files": {
            name: hashlib.sha256(body).hexdigest() for name, body in contents.items()
        },
    }


def install_runtime(directory: Path, root: Path) -> None:
    errors = runtime_delivery.apply(directory, root, runtime_receipt(root))
    if errors:
        raise InstallError("; ".join(errors))


def check_runtime(context: Context) -> list[str]:
    directory = context.state_directory / "bin"
    changes = [
        f"install Agent runtime: {error}"
        for error in runtime_delivery.check(
            directory, context.root, runtime_receipt(context.root)
        )
    ]
    if not context.windows:
        alias = python_alias_status(context, directory)
        if alias:
            changes.append(alias)
    return changes


PYTHON_ALIAS_MARKER = "# tj-agents-managed-python-alias "


def python_alias(interpreter: str | None = None) -> str:
    interpreter = console_python() if interpreter is None else interpreter
    marker = json.dumps(
        {"interpreter": interpreter}, separators=(",", ":"), sort_keys=True
    )
    return (
        "#!/bin/sh\n"
        + PYTHON_ALIAS_MARKER
        + marker
        + "\nexec "
        + shlex.quote(interpreter)
        + ' "$@"\n'
    )


def managed_python_alias(value: str) -> str | None:
    prefix = "#!/bin/sh\n" + PYTHON_ALIAS_MARKER
    if not value.startswith(prefix):
        return None
    marker, separator, _ = value[len(prefix) :].partition("\n")
    if not separator:
        return None
    try:
        metadata = json.loads(marker)
    except json.JSONDecodeError:
        return None
    interpreter = (
        metadata.get("interpreter")
        if isinstance(metadata, dict) and set(metadata) == {"interpreter"}
        else None
    )
    if (
        not isinstance(interpreter, str)
        or not interpreter
        or python_alias(interpreter) != value
    ):
        return None
    return interpreter


def python_alias_path(context: Context, directory: Path) -> Path:
    for path in (context.state_directory, directory):
        if os.path.lexists(path) and runtime_delivery.is_reparse(path):
            raise InstallError(f"Claude hook interpreter parent is a link: {path}")
    alias = directory / "python"
    if os.path.lexists(alias) and runtime_delivery.is_reparse(alias):
        raise InstallError(f"Claude hook interpreter is a link: {alias}")
    if alias.exists():
        if not alias.is_file():
            raise InstallError(f"Claude hook interpreter is not a file: {alias}")
    return alias


def python_alias_status(context: Context, directory: Path) -> str | None:
    alias = python_alias_path(context, directory)
    if not alias.exists():
        return f"install Claude hook interpreter {alias}"
    value = alias.read_text(encoding="utf-8")
    if managed_python_alias(value) is None:
        return f"refuse unowned Claude hook interpreter {alias}"
    if value != python_alias():
        return f"install Claude hook interpreter {alias}"
    if os.name != "nt" and not os.access(alias, os.X_OK):
        return f"install Claude hook interpreter {alias}"
    return None


def install_python_alias(context: Context, directory: Path) -> None:
    alias = python_alias_path(context, directory)
    status = python_alias_status(context, directory)
    if status is None:
        return
    if status.startswith("refuse "):
        raise InstallError(status)
    if not alias.exists():
        try:
            with alias.open("x", encoding="utf-8", newline="\n") as stream:
                stream.write(python_alias())
        except FileExistsError:
            return install_python_alias(context, directory)
    value = alias.read_text(encoding="utf-8")
    if managed_python_alias(value) is None:
        raise InstallError(f"refuse unowned Claude hook interpreter {alias}")
    if value != python_alias():
        temporary = alias.with_name(f".{alias.name}.{secrets.token_hex(12)}.tmp")
        created = False
        try:
            with temporary.open("x", encoding="utf-8", newline="\n") as stream:
                created = True
                stream.write(python_alias())
            alias = python_alias_path(context, directory)
            if alias.read_text(encoding="utf-8") != value:
                raise InstallError(f"refuse changed Claude hook interpreter {alias}")
            os.replace(temporary, alias)
        finally:
            if created and temporary.exists():
                temporary.unlink()
    if alias.read_text(encoding="utf-8") != python_alias():
        raise InstallError(f"refuse changed Claude hook interpreter {alias}")
    alias.chmod(0o755)


def apply_runtime(context: Context) -> list[str]:
    directory = context.state_directory / "bin"
    install_runtime(directory, context.root)
    if not context.windows:
        install_python_alias(context, directory)
    return [f"installed the Agent runtime in {directory}"]


def check_agent_store(context: Context) -> list[str]:
    pending = agent_store.pending()
    return [pending] if pending else []


def apply_agent_store(context: Context) -> list[str]:
    return [
        f"installed agent-store {agent_store.install()} into {agent_store.executable().parent}"
    ]


def uv_executable(context: Context) -> str:
    uv: str | None = context.which("uv")
    if not uv:
        raise InstallError(
            "uv is required to install the tj item CLI: "
            "https://docs.astral.sh/uv/getting-started/installation/"
        )
    return uv


def tj_item_drift(context: Context) -> list[str]:
    return work_items_install.drift(
        work_items_install.default_tool_directory(context.environ, context.windows),
        work_items_install.Pins.load(),
        work_items_install.discovered_profiles(),
    )


def check_tj_item(context: Context) -> list[str]:
    uv_executable(context)
    return tj_item_drift(context)


def apply_tj_item(context: Context) -> list[str]:
    uv = uv_executable(context)
    pins = work_items_install.Pins.load()
    profiles = work_items_install.discovered_profiles()
    command = work_items_install.install_command(uv, pins, profiles)
    result = run_installer(context, command)
    if result.returncode:
        detail = (result.stderr or result.stdout).strip()
        raise InstallError(f"uv tool install {pins.tool.package} failed: {detail}")
    remaining = tj_item_drift(context)
    if remaining:
        raise InstallError("; ".join(remaining))
    installed = (pins.tool, *profiles)
    return [f"installed {pin.package} {pin.revision[:12]}" for pin in installed]


def cli_installer(module: ModuleType, arguments: list[str]) -> tuple[int, list[str]]:
    output = io.StringIO()
    with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
        code = module.main(arguments)
    lines = output.getvalue().splitlines()
    return code, [line.strip() for line in lines if line.strip()]


def cli_tool_step(name: str, module: ModuleType) -> Step:
    def check(context: Context) -> list[str]:
        code, lines = cli_installer(module, ["--check"])
        return [] if code == 0 else lines or [f"install {name}"]

    def apply(context: Context) -> list[str]:
        code, lines = cli_installer(module, [])
        if code:
            raise InstallError("; ".join(lines) or f"the {name} installer failed")
        return lines

    return Step(name, check, apply)


def command_shim_name(command: str, windows: bool) -> str:
    return f"tj-{command}.cmd" if windows else f"tj-{command}"


def command_shim_text(python: str, script: Path, windows: bool) -> str:
    if windows:
        return f'@"{python}" -B "{script}" %*\r\n'
    return f'#!/bin/sh\nexec {shlex.quote(python)} -B {shlex.quote(str(script))} "$@"\n'


def windows_shim_binding(text: str) -> tuple[str, str] | None:
    suffix = '" %*\r\n'
    if not (text.startswith('@"') and text.endswith(suffix)):
        return None
    python, separator, script = text[2 : -len(suffix)].partition('" -B "')
    return (python, script) if separator else None


def posix_shim_binding(text: str) -> tuple[str, str] | None:
    prefix, suffix = "#!/bin/sh\nexec ", ' "$@"\n'
    if not (text.startswith(prefix) and text.endswith(suffix)):
        return None
    try:
        tokens = shlex.split(text[len(prefix) : -len(suffix)])
    except ValueError:
        return None
    return (tokens[0], tokens[2]) if len(tokens) == 3 and tokens[1] == "-B" else None


def parsed_shim_binding(windows: bool, text: str) -> tuple[str, str] | None:
    return windows_shim_binding(text) if windows else posix_shim_binding(text)


def independent_interpreter(context: Context, path: Path) -> bool:
    if not path.is_absolute():
        return False
    try:
        resolved = path.resolve(strict=True)
        root = context.root.resolve(strict=True)
    except OSError:
        return False
    if not resolved.is_file() or resolved == root or root in resolved.parents:
        return False
    return not any(
        ancestor.name == ".venv" or (ancestor / "pyvenv.cfg").is_file()
        for ancestor in resolved.parents
    )


def lifecycle_binding(context: Context, path: Path, script: Path) -> str | None:
    if not path.is_file():
        return None
    try:
        text = path.read_text(encoding="utf-8", newline="")
    except (OSError, UnicodeDecodeError):
        return None
    binding = parsed_shim_binding(context.windows, text)
    if binding is None:
        return None
    interpreter, bound_script = binding
    if bound_script != str(script):
        return None
    candidate = Path(interpreter)
    if not independent_interpreter(context, candidate):
        return None
    return interpreter


def lifecycle_paths(context: Context) -> dict[Path, Path]:
    directory = agent_store.executable().parent
    scripts = runtime_bin(context) / ".agents" / "agent" / "runtime"
    return {
        directory / command_shim_name(command, context.windows): scripts / script
        for command, script in LIFECYCLE_COMMANDS.items()
    }


def lifecycle_shims(context: Context) -> dict[Path, str]:
    fallback: str | None = None
    shims: dict[Path, str] = {}
    for path, script in lifecycle_paths(context).items():
        interpreter = lifecycle_binding(context, path, script)
        if interpreter is None:
            if fallback is None:
                fallback = console_python()
            interpreter = fallback
        shims[path] = command_shim_text(interpreter, script, context.windows)
    return shims


def shim_current(path: Path, text: str) -> bool:
    return path.is_file() and path.read_bytes() == text.encode("utf-8")


def check_lifecycle_commands(context: Context) -> list[str]:
    return [
        f"write {path}"
        for path, text in lifecycle_shims(context).items()
        if not shim_current(path, text)
    ]


def apply_lifecycle_commands(context: Context) -> list[str]:
    changes = []
    for path, text in lifecycle_shims(context).items():
        if shim_current(path, text):
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="")
        path.chmod(0o755)
        changes.append(f"wrote {path}")
    return changes


def windows_only(context: Context) -> str | None:
    return None if context.windows else WINDOWS_ONLY


def codex_config_path(context: Context) -> Path:
    return context.codex_home / "config.toml"


def codex_daemon_text(context: Context) -> tuple[str, str]:
    path = codex_config_path(context)
    text = path.read_text(encoding="utf-8-sig") if path.is_file() else ""
    return text, codex_daemon.enabled_text(text)


def check_codex_daemon(context: Context) -> list[str]:
    text, enabled = codex_daemon_text(context)
    return (
        []
        if enabled == text
        else [
            f"let Codex launches start its shared daemon in {codex_config_path(context)}"
        ]
    )


def apply_codex_daemon(context: Context) -> list[str]:
    text, enabled = codex_daemon_text(context)
    if enabled == text:
        return []
    write_profile(codex_config_path(context), enabled, "utf-8", False)
    return [f"removed {codex_daemon.FEATURE} = false from {codex_config_path(context)}"]


def skip_codex_hook_trust(context: Context) -> str | None:
    return (
        None
        if context.codex_home.is_dir()
        else f"Codex is not set up ({context.codex_home} is missing)"
    )


def check_codex_hook_trust(context: Context) -> list[str]:
    return codex_hook_trust.pending(context.state_directory)


def apply_codex_hook_trust(context: Context) -> list[str]:
    trusted = codex_hook_trust.trust_installed(
        codex_launch.executable(),
        context.state_directory,
        codex_installed.MARKETPLACE_OVERRIDES,
    )
    return [f"trusted {len(trusted)} tj-agents Codex hook(s)"]


def native_permissions_plan(context: Context) -> dict[Path, str]:
    mode = host_permissions.permissions(plugin_setup_project(context))
    return host_permissions.plan(
        context.claude_home / "settings.json", codex_config_path(context), mode
    )


def check_native_permissions(context: Context) -> list[str]:
    return [
        f"update native permission defaults in {path}"
        for path in native_permissions_plan(context)
    ]


def apply_native_permissions(context: Context) -> list[str]:
    targets = native_permissions_plan(context)
    desktop = codex_config_path(context).parent / ".codex-global-state.json"
    if desktop in targets:
        if host_permissions.desktop_running():
            raise InstallError(
                "Close Codex Desktop, run install --step 'native permission defaults' "
                "from a terminal, then reopen the app to load the configured defaults"
            )
        targets = native_permissions_plan(context)
    for path, text in targets.items():
        write_profile(path, text, "utf-8", False)
    return [f"updated native permission defaults in {path}" for path in targets]


def plugin_setup_project(context: Context) -> Path:
    return context.project or Path.cwd().resolve()


def plugin_setup_executables(context: Context) -> dict[Host, str | None]:
    return {host: context.which(host.value) for host in Host}


def check_plugin_setup(context: Context) -> list[str]:
    if native_runtime.pending():
        return check_native_cli(context)
    return required_plugins.check_setup(
        plugin_setup_project(context), tuple(Host), plugin_setup_executables(context)
    )


def apply_plugin_setup(context: Context) -> list[str]:
    errors = required_plugins.converge_setup(
        plugin_setup_project(context),
        tuple(Host),
        plugin_setup_executables(context),
        context.onboard,
    )
    if errors:
        raise InstallError("; ".join(errors))
    return []


SCOPED_MARKETPLACE = "@tj-agents"


def claude_user_enabled(context: Context) -> list[str]:
    enabled = read_json(context.claude_home / "settings.json").get("enabledPlugins")
    if not isinstance(enabled, dict):
        return []
    return sorted(
        identity
        for identity, value in enabled.items()
        if identity.endswith(SCOPED_MARKETPLACE) and value is True
    )


def codex_user_enabled(context: Context) -> list[str]:
    path = codex_config_path(context)
    text = path.read_text(encoding="utf-8-sig") if path.is_file() else ""
    return sorted(
        identity
        for identity in codex_user_plugins.enabled(text)
        if identity.endswith(SCOPED_MARKETPLACE)
    )


def check_plugin_scope(context: Context) -> list[str]:
    """tj-agents plugins load only where a repository selects them, never for every project."""
    return [
        f"stop enabling {identity} for every {host} project"
        for host, identities in (
            (Host.CLAUDE, claude_user_enabled(context)),
            (Host.CODEX, codex_user_enabled(context)),
        )
        for identity in identities
    ]


def apply_plugin_scope(context: Context) -> list[str]:
    changes = []
    for identity in claude_user_enabled(context):
        result = context.run(
            ["claude", "plugin", "disable", identity, "--scope", "user"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
            check=False,
        )
        if result.returncode:
            raise InstallError(
                f"claude plugin disable {identity}: {(result.stderr or result.stdout).strip()}"
            )
        changes.append(f"claude: {identity} loads only where a repository selects it")
    identities = codex_user_enabled(context)
    if identities:
        path = codex_config_path(context)
        text = path.read_text(encoding="utf-8-sig")
        path.write_text(codex_user_plugins.without(text, identities), encoding="utf-8")
        changes.extend(
            f"codex: {identity} loads only where a repository selects it"
            for identity in identities
        )
    return changes


def check_plugin_copies(context: Context) -> list[str]:
    return plugin_copies.drift(context.state_directory)


def apply_plugin_copies(context: Context) -> list[str]:
    converged, changes, errors = [], [], []
    for source in plugin_copies.sources():
        try:
            target = required_plugins.declaration(source.root, source.identity)
            result = context.run(
                [sys.executable, "-B", str(target.script), "converge"],
                cwd=source.root,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=INSTALLER_TIMEOUT_SECONDS,
                check=False,
            )
        except (
            required_plugins.PluginSetupError,
            OSError,
            subprocess.SubprocessError,
        ) as error:
            errors.append(f"{source.identity}: {error}")
            continue
        if result.returncode:
            detail = required_plugins.bounded(result)
            errors.append(f"{source.identity}: converge failed: {detail}")
            continue
        converged.append(source)
        changes.append(
            f"converged {source.identity} copies from {source.revision[:12]}"
        )
    plugin_copies.record(context.state_directory, converged)
    if errors:
        raise InstallError("; ".join(errors))
    return changes


def retired_state_options(context: Context) -> dict:
    return {
        "windows": context.windows,
        "home": context.home,
        "run": context.run,
        "environ": context.environ,
    }


def check_retired_state(context: Context) -> list[str]:
    return [
        f"remove {item}"
        for item in retired_state.present(
            context.state_directory, **retired_state_options(context)
        )
    ]


def apply_retired_state(context: Context) -> list[str]:
    removal = retired_state.remove(
        context.state_directory, **retired_state_options(context)
    )
    if removal.failures:
        raise InstallError("; ".join(removal.failures))
    return [f"removed {item}" for item in removal.removed]


def check_native_cli(context: Context) -> list[str]:
    pending = native_runtime.pending(revision=native_runtime.NATIVE_REVISION)
    if pending:
        return ["install or update native CLI binaries: " + ", ".join(pending)]
    return []


def apply_native_cli(context: Context) -> list[str]:
    changed = native_runtime.prepare(revision=native_runtime.NATIVE_REVISION)
    context.environ["PATH"] = native_runtime.search_path(
        context.environ.get("PATH", os.defpath)
    )
    return (
        ["installed native CLI binaries: " + ", ".join(changed)]
        if changed
        else ["native CLI available on PATH"]
    )


STEPS = (
    Step("retired state", check_retired_state, apply_retired_state),
    Step("Git long paths", check_git_longpaths, apply_git_longpaths, windows_only),
    Step("native CLI", check_native_cli, apply_native_cli, required=True),
    Step(
        "native permission defaults", check_native_permissions, apply_native_permissions
    ),
    Step("Codex shared daemon", check_codex_daemon, apply_codex_daemon),
    Step("agent-store CLI", check_agent_store, apply_agent_store),
    Step("tj item CLI", check_tj_item, apply_tj_item),
    cli_tool_step("worktrunk", worktrunk_install),
    cli_tool_step("Handy", handy_install),
    Step("launch-check runtime", check_runtime, apply_runtime),
    Step("tj lifecycle commands", check_lifecycle_commands, apply_lifecycle_commands),
    Step("shell profiles", check_profile, apply_profile),
    Step(
        "hidden Git Bash for Claude", check_hidden_bash, apply_hidden_bash, windows_only
    ),
    Step(
        "Codex profile agents",
        check_codex_agents,
        apply_codex_agents,
        skip_codex_agents,
    ),
    Step(
        "harness permissions",
        lambda context: synchronize_permissions(context, "check"),
        lambda context: synchronize_permissions(context, "apply"),
    ),
    Step("plugin setup", check_plugin_setup, apply_plugin_setup),
    Step("plugin scope", check_plugin_scope, apply_plugin_scope),
    Step(
        "Codex hook trust",
        check_codex_hook_trust,
        apply_codex_hook_trust,
        skip_codex_hook_trust,
    ),
    Step("plugin copies", check_plugin_copies, apply_plugin_copies),
)


def run_steps(
    steps, context: Context, check_only: bool, out=sys.stdout, err=sys.stderr
) -> int:
    print(f"package root: {context.root}", file=out)
    drift = False
    failed = False
    failed_steps = set()
    for step in steps:
        if "native CLI" in failed_steps:
            print(f"n/a    {step.name}: native CLI prerequisite unavailable", file=out)
            continue
        if step.name == "plugin setup" and failed_steps & {"harness permissions"}:
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
                if step.name == "native CLI":
                    failed_steps.add(step.name)
                for item in pending:
                    print(f"drift  {step.name}: {item}", file=out)
                continue
            for change in step.apply(context) or pending:
                print(f"apply  {step.name}: {change}", file=out)
        except Exception as error:  # noqa: BLE001
            print(f"failed {step.name}: {error}", file=err)
            context.failures.append(f"{step.name}: {error}")
            failed = True
            failed_steps.add(step.name)
            if step.required:
                return 2
    if failed:
        return 2
    return 1 if drift else 0


def selected_steps(names: set[str]) -> tuple[Step, ...]:
    selected = set(names)
    if selected & {"shell profiles", "tj lifecycle commands"}:
        selected.add("launch-check runtime")
    if "plugin setup" in selected:
        selected.add("harness permissions")
    return tuple(
        step for step in STEPS if not selected or step.required or step.name in selected
    )


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


def plugin_hosts(context: Context) -> list[Host]:
    return [host for host in Host if context.which(host.value)]


def update_plugins(
    context: Context, out=sys.stdout, err=sys.stderr
) -> dict[Host, plugin_update.Outcome]:
    """Bring every installed host's tj-agents plugins to their marketplace pins."""
    digests = Digests(context.state_directory)
    outcomes = {}
    for host in plugin_hosts(context):
        if not plugin_update.INSTALLED[host].refresh_identities(
            plugin_update.MARKETPLACES
        ):
            print(
                f"n/a    {host} plugins: none installed yet; plugin setup installs the selected ones",
                file=out,
            )
            continue
        outcome = plugin_update.update(host, digests)
        for failure in outcome.failures:
            print(f"failed plugins: {failure}", file=err)
            context.failures.append(f"plugins: {failure}")
        outcomes[host] = outcome
    return outcomes


def converged_installer(context: Context) -> Path | None:
    """The installer of the Agent package the plugin update converged to, when it is not this one."""
    for host in plugin_hosts(context):
        package = plugin_update.agent_package(host, context.state_directory)
        if package is None:
            continue
        installer = package / ".agents" / "agent" / "runtime" / "install.py"
        if (
            installer.is_file()
            and installer.resolve() != (RUNTIME / "install.py").resolve()
        ):
            return installer
        return None
    return None


def published_path(context: Context, host: Host) -> Path:
    return context.state_directory / f"published-{host.value}.txt"


def publish_update(
    context: Context, outcomes: dict[Host, plugin_update.Outcome]
) -> int:
    """Tell running tj run tabs of a host to reload when its plugins moved or its published revision is stale.

    `tj run`'s launch check compares the marketplace HEAD with the last published revision, so a host whose
    published revision is behind is republished even when nothing else changed.
    """
    code = 0
    for host, outcome in outcomes.items():
        if outcome.failures or outcome.revision is None:
            continue
        record = published_path(context, host)
        published = (
            record.read_text(encoding="utf-8").strip() if record.is_file() else None
        )
        if not outcome.changed and published == outcome.revision:
            continue
        command = ["tj", "update", "--host", host.value, "--revision", outcome.revision]
        try:
            result = context.run(
                command, capture_output=True, text=True, timeout=30, check=False
            )
        except (OSError, subprocess.SubprocessError) as error:
            context.failures.append(f"{host} update: {error}")
            code = 2
            continue
        if result.returncode:
            context.failures.append(
                f"{host} update: {result.stderr.strip() or f'exit {result.returncode}'}"
            )
            code = 2
            continue
        record.parent.mkdir(parents=True, exist_ok=True)
        record.write_text(outcome.revision, encoding="utf-8")
        print(
            f"apply  {host} update: published {outcome.revision[:12]} to running tj run tabs"
        )
    return code


def pinned_installer(state: Path) -> Path | None:
    if ROOT.resolve() != (state / "bin").resolve():
        return None
    pinned = machine_state.packaged("install.py")
    return None if pinned.resolve() == (RUNTIME / "install.py").resolve() else pinned


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
    if sys.version_info < (3, 12):
        print("install requires Python 3.12 or newer", file=sys.stderr)
        return 2
    arguments = list(argv if argv is not None else sys.argv[1:])
    parser = argparse.ArgumentParser(
        prog="tj install", description=__doc__, allow_abbrev=False
    )
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument(
        "--check",
        action="store_true",
        help="report drift, write nothing, exit 1 on drift",
    )
    modes.add_argument(
        "--onboard",
        action="store_true",
        help="allow explicitly requested plugin enrollment",
    )
    parser.add_argument(
        "--step",
        action="append",
        choices=[step.name for step in STEPS],
        help="run named steps with their dependencies and required preconditions; repeat to select several",
    )
    parser.add_argument("--converged", action="store_true", help=argparse.SUPPRESS)
    try:
        parsed = parser.parse_args(arguments)
    except SystemExit as error:
        if error.code:
            jobs.record(
                jobs.state_directory(),
                jobs.Job.INSTALL,
                [f"install rejected its arguments: {shlex.join(arguments)}"],
            )
        raise
    context = machine_context()
    installer = pinned_installer(context.state_directory)
    if installer is not None:
        return process.run(
            [sys.executable, "-B", str(installer), *arguments]
        ).returncode
    context.onboard = parsed.onboard
    native_runtime.expose()
    context.environ["PATH"] = native_runtime.search_path(
        context.environ.get("PATH", os.defpath)
    )
    steps = selected_steps(set(parsed.step or ()))
    if parsed.check:
        return run_steps(steps, context, True)
    full = not parsed.step and not parsed.converged
    outcomes: dict[Host, plugin_update.Outcome] = {}
    successor = None
    with jobs.held(context.state_directory, jobs.Job.INSTALL) as acquired:
        if not acquired:
            print("install: another machine install is running", file=sys.stderr)
            return 2
        if full:
            # Native plugin commands run through tj_hook, which the native CLI step delivers.
            native = tuple(step for step in STEPS if step.required)
            if run_steps(native, context, False):
                jobs.record(context.state_directory, jobs.Job.INSTALL, context.failures)
                return 2
            outcomes = update_plugins(context)
            successor = converged_installer(context)
        if successor is None:
            code = run_steps(steps, context, False)
        jobs.record(context.state_directory, jobs.Job.INSTALL, context.failures)
    if successor is not None:
        print(
            f"install: continuing with the converged Agent package at {successor.parents[3]}"
        )
        code = process.run(
            [sys.executable, "-B", str(successor), *arguments, "--converged"]
        ).returncode
    if not code:
        code = publish_update(context, outcomes)
    return 2 if context.failures and not code else code


if __name__ == "__main__":
    raise SystemExit(main())
