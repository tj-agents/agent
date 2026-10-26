"""SessionStart hook: record this session's id, tab title, directory and host pid.

The launchers export `AGENT_CLI_TAB_TITLE` when they open a tab (`agent_cli.py`'s `launch_tab`); a
session started by hand exports nothing and records a null title. `peer_cli.py` reads these entries to
resolve a session from the tab title a user can see, and the reverse.

Codex runs hooks through a shell, so the parent pid is a transient shell rather than the CLI. The
recorded pid is the nearest `claude` or `codex` ancestor, with its OS start time so a reused pid is
never mistaken for the live host.

Contract: exit 0 always. Bookkeeping must never stop a session starting.
"""

import collections
import hashlib
import importlib.util
import json
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent


STATE_DIRECTORY_ENV = "AGENT_STATE_DIRECTORY"
TITLE_ENV = "AGENT_CLI_TAB_TITLE"
HOST_NAMES_ENV = "AGENT_CLI_HOST_NAMES"
DEFAULT_HOST_NAMES = frozenset({"claude", "codex"})
MAX_ANCESTOR_DEPTH = 10
OBSERVER_STORE_RECEIPT = ".tj-agents-observer-store.json"
OBSERVER_STORE_KIND = "tj-agents-agent-observer-store"
OBSERVER_STORE_VERSION = 1
OBSERVER_STORE_FILES = ("register_session.py", "session_store.py", "repository_identity.py")

FILETIME_EPOCH_DELTA = 11644473600
FILETIME_TICKS_PER_SECOND = 10_000_000

ProcessInfo = collections.namedtuple("ProcessInfo", "pid ppid name started_at")


def state_directory():
    override = os.environ.get(STATE_DIRECTORY_ENV)
    root = Path(override) if override else Path.home() / ".agents-state"
    return root / "cli-sessions"


def read_host_names():
    override = os.environ.get(HOST_NAMES_ENV)
    if not override:
        return DEFAULT_HOST_NAMES
    names = {part.strip().casefold() for part in override.split(",") if part.strip()}
    return frozenset(names) if names else DEFAULT_HOST_NAMES


def stem(name):
    if not name:
        return ""
    return Path(str(name)).stem.casefold()


def resolve_host_pid(start_pid, lookup, host_names, depth_cap=MAX_ANCESTOR_DEPTH):
    current = start_pid
    seen = set()
    for _ in range(depth_cap):
        if current is None or current in seen:
            return None
        seen.add(current)
        info = lookup(current)
        if info is None:
            return None
        if stem(info.name) in host_names:
            return info
        current = info.ppid
    return None


def resolve_recorded_pid(ppid, lookup=None, host_names=None, depth_cap=MAX_ANCESTOR_DEPTH):
    if host_names is None:
        host_names = read_host_names()
    if lookup is None:
        try:
            lookup = build_process_lookup()
        except Exception:
            lookup = None

    match = None
    if lookup is not None:
        try:
            match = resolve_host_pid(ppid, lookup, host_names, depth_cap)
        except Exception:
            match = None
    if match is not None:
        return match.pid, stem(match.name), match.started_at

    started = None
    if lookup is not None:
        try:
            info = lookup(ppid)
            if info is not None:
                started = info.started_at
        except Exception:
            started = None
    return ppid, None, started


def _windows_process_table():
    import ctypes
    from ctypes import wintypes

    MAX_PATH = 260
    TH32CS_SNAPPROCESS = 0x00000002
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    INVALID_HANDLE_VALUE = -1
    ERROR_NO_MORE_FILES = 18

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", ctypes.c_wchar * MAX_PATH),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
    kernel32.Process32FirstW.restype = wintypes.BOOL
    kernel32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
    kernel32.Process32NextW.restype = wintypes.BOOL
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.GetProcessTimes.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
    ]
    kernel32.GetProcessTimes.restype = wintypes.BOOL

    def started_at(pid):
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return None
        try:
            creation, exited, kernel, user = (wintypes.FILETIME() for _ in range(4))
            if not kernel32.GetProcessTimes(
                handle, ctypes.byref(creation), ctypes.byref(exited),
                ctypes.byref(kernel), ctypes.byref(user),
            ):
                return None
            ticks = (creation.dwHighDateTime << 32) | creation.dwLowDateTime
            if not ticks:
                return None
            return ticks / FILETIME_TICKS_PER_SECOND - FILETIME_EPOCH_DELTA
        finally:
            kernel32.CloseHandle(handle)

    snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snapshot in (None, 0) or ctypes.c_ssize_t(snapshot).value == INVALID_HANDLE_VALUE:
        raise OSError(ctypes.get_last_error(), "cannot snapshot the process table")
    entry = PROCESSENTRY32W()
    entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
    table = {}
    try:
        if not kernel32.Process32FirstW(snapshot, ctypes.byref(entry)):
            error = ctypes.get_last_error()
            if error == ERROR_NO_MORE_FILES:
                return table
            raise OSError(error, "cannot enumerate the process table")
        while True:
            pid = int(entry.th32ProcessID)
            table[pid] = ProcessInfo(
                pid=pid,
                ppid=int(entry.th32ParentProcessID),
                name=str(entry.szExeFile),
                started_at=started_at(pid),
            )
            if not kernel32.Process32NextW(snapshot, ctypes.byref(entry)):
                error = ctypes.get_last_error()
                if error == ERROR_NO_MORE_FILES:
                    break
                raise OSError(error, "cannot enumerate the process table")
    finally:
        kernel32.CloseHandle(snapshot)
    return table


def _linux_boot_time():
    with open("/proc/stat", encoding="utf-8") as handle:
        for line in handle:
            if line.startswith("btime"):
                return float(line.split()[1])
    raise OSError("btime not found in /proc/stat")


def _linux_clock_ticks():
    try:
        return os.sysconf("SC_CLK_TCK")
    except (ValueError, OSError, AttributeError):
        return 100


def _linux_lookup(pid):
    try:
        raw = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError:
        raise
    try:
        name_start = raw.index("(")
        name_end = raw.rindex(")")
    except ValueError as error:
        raise ValueError("malformed /proc process identity") from error
    name = raw[name_start + 1:name_end]
    fields = raw[name_end + 2:].split()
    try:
        ppid = int(fields[1])
        starttime_ticks = int(fields[19])
    except (IndexError, ValueError) as error:
        raise ValueError("malformed /proc process fields") from error
    try:
        started_at = _linux_boot_time() + starttime_ticks / _linux_clock_ticks()
    except OSError:
        raise
    return ProcessInfo(pid=pid, ppid=ppid, name=name, started_at=started_at)


def _parse_ps_lstart(text):
    import datetime

    try:
        return datetime.datetime.strptime(text.strip(), "%a %b %d %H:%M:%S %Y").timestamp()
    except ValueError as error:
        raise ValueError("malformed ps process start time") from error


def _ps_lookup(pid):
    import subprocess

    try:
        result = subprocess.run(
            ["ps", "-o", "ppid=,comm=,lstart=", "-p", str(pid)],
            capture_output=True, text=True, timeout=2, check=False,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise OSError("ps process lookup failed") from error
    if result.returncode != 0:
        if not result.stdout.strip() and not result.stderr.strip():
            return None
        raise OSError("ps process lookup failed: " + result.stderr.strip())
    line = result.stdout.strip()
    if not line:
        return None
    parts = line.split(None, 2)
    if len(parts) < 3:
        raise ValueError("malformed ps process identity")
    try:
        ppid = int(parts[0])
    except ValueError as error:
        raise ValueError("malformed ps parent pid") from error
    return ProcessInfo(pid=pid, ppid=ppid, name=parts[1], started_at=_parse_ps_lstart(parts[2]))


def build_process_lookup():
    if os.name == "nt":
        return _windows_process_table().get
    if sys.platform == "linux":
        return _linux_lookup
    return _ps_lookup


def process_table():
    """Return a best-effort process table for discovery, never a permission bypass."""
    if os.name == "nt":
        return _windows_process_table()
    if sys.platform == "linux":
        table = {}
        for path in Path("/proc").iterdir():
            if not path.name.isdigit():
                continue
            try:
                info = _linux_lookup(int(path.name))
            except (OSError, ValueError):
                continue
            if info is not None:
                table[info.pid] = info
        return table
    return {}


def read_payload():
    try:
        raw = sys.stdin.read()
    except (OSError, ValueError):
        return {}
    try:
        value = json.loads(raw) if raw.strip() else {}
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def terminal_identity(environ=None):
    """Return the exact terminal handle available to this session, never a title guess."""
    values = os.environ if environ is None else environ
    if values.get("TMUX_PANE"):
        tmux = values.get("TMUX", "")
        tmux_parts = tmux.split(",") if tmux else []
        socket = tmux_parts[0] if tmux_parts else None
        identity = {"kind": "tmux", "pane_id": values["TMUX_PANE"]}
        if socket:
            identity["socket"] = socket
        if len(tmux_parts) == 3 and tmux_parts[2]:
            identity["session_index"] = tmux_parts[2]
        return identity
    if values.get("KITTY_WINDOW_ID") and values.get("KITTY_LISTEN_ON"):
        return {"kind": "kitty", "window_id": values["KITTY_WINDOW_ID"],
                "listen_on": values["KITTY_LISTEN_ON"]}
    if (values.get("KONSOLE_DBUS_SERVICE") and values.get("KONSOLE_DBUS_SESSION")
            and values.get("KONSOLE_DBUS_WINDOW")):
        return {
            "kind": "konsole",
            "service": values["KONSOLE_DBUS_SERVICE"],
            "session": values["KONSOLE_DBUS_SESSION"],
            "window": values["KONSOLE_DBUS_WINDOW"],
        }
    if os.name == "nt" and values.get("WT_SESSION"):
        identity = {"kind": "windows-terminal", "session": values["WT_SESSION"]}
        if values.get("AGENT_WINDOWS_TERMINAL_TAB_ID"):
            identity["runtime_id"] = values["AGENT_WINDOWS_TERMINAL_TAB_ID"]
        return identity
    return None


def profile_identity(host):
    if host == "codex":
        return os.path.normcase(str(Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex").resolve()))
    if host == "claude":
        return os.path.normcase(str(Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude").resolve()))
    return None


def _staged_agent_root(root):
    try:
        receipt = json.loads((root / "source.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return (isinstance(receipt, dict) and receipt.get("repository") == "tj-agents/agent"
            and isinstance(receipt.get("revision"), str) and bool(receipt["revision"])
            and isinstance(receipt.get("sha256"), str) and len(receipt["sha256"]) == 64
            and isinstance(receipt.get("exports"), (list, dict)) and bool(receipt["exports"]))


def _observer_session_store_path():
    receipt_path = HERE / OBSERVER_STORE_RECEIPT
    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    files = receipt.get("files") if isinstance(receipt, dict) else None
    if (not isinstance(receipt, dict) or receipt.get("kind") != OBSERVER_STORE_KIND
            or receipt.get("version") != OBSERVER_STORE_VERSION
            or not isinstance(files, dict) or set(files) != set(OBSERVER_STORE_FILES)):
        return None
    for name in OBSERVER_STORE_FILES:
        digest = files.get(name)
        path = HERE / name
        try:
            actual = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            return None
        if (not isinstance(digest, str) or len(digest) != 64 or actual != digest):
            return None
    return HERE / "session_store.py"


def session_store_path():
    staged = _observer_session_store_path()
    if staged is not None:
        return staged
    for root in HERE.parents:
        markers = (root / ".git", root / ".codex-plugin" / "plugin.json", root / ".claude-plugin" / "plugin.json")
        runtime = root / ".agents" / "agent-store" / "runtime"
        path = runtime / "session_store.py"
        if any(marker.exists() for marker in markers) or _staged_agent_root(root):
            return path if path.is_file() else None
    return None


def _load_session_store():
    path = session_store_path()
    if path is None:
        return None
    try:
        spec = importlib.util.spec_from_file_location("register_session_session_store", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    except Exception:
        return None


def _record_session_start(host, session_id, cwd, title, pid, pid_started_at, preserve_turn=False):
    store = _load_session_store()
    if store is None:
        return
    try:
        connection = store.connect()
    except Exception:
        return
    try:
        handle = store.session_handle(host, session_id)
        if preserve_turn and store.get_session(connection, handle) is not None:
            store.attach_session_client(connection, handle, pid, pid_started_at, cwd)
        else:
            store.start_session(connection, host, session_id, cwd, title=title, pid=pid, pid_started_at=pid_started_at)
    except Exception:
        pass
    finally:
        connection.close()


def record(data, lookup=None, host_names=None, client=None):
    session = data.get("session_id") or data.get("sessionId")
    if not session:
        return None
    if client is None:
        pid, host, pid_started_at = resolve_recorded_pid(
            os.getppid(), lookup=lookup, host_names=host_names,
        )
    else:
        pid, host, pid_started_at = client["pid"], client["host"], client["pid_started_at"]
    cwd = data.get("cwd") or os.getcwd()
    title = (client.get("title") if client is not None else os.environ.get(TITLE_ENV)) or None
    entry = {
        "session_id": str(session),
        "title": title,
        "cwd": cwd,
        "pid": pid,
        "started_at": time.time(),
    }
    if pid_started_at is not None:
        entry["pid_started_at"] = pid_started_at
    if host is not None:
        entry["host"] = host
        entry["profile"] = profile_identity(host)
    terminal = client.get("terminal") if client is not None else terminal_identity()
    if terminal is not None:
        entry["terminal"] = terminal
    if client is not None:
        entry["client_identity"] = client["client_identity"]
        entry["launch_cwd"] = client.get("launch_cwd", cwd)
        for key in ("client_message_id", "native_turn_id", "native_rollout", "native_cwd", "recording_path", "backend", "profile"):
            if key in client:
                entry[key] = client[key]
    destination = state_directory() / f"{session}.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = destination.with_name(f".{destination.name}.{os.getpid()}.tmp")
    staging.write_text(json.dumps(entry, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(staging, destination)
    if host in ("claude", "codex"):
        try:
            _record_session_start(host, str(session), cwd, title, pid, pid_started_at,
                                  **({"preserve_turn": True} if client is not None else {}))
        except Exception:
            pass
    return entry


def main():
    try:
        record(read_payload())
    except Exception:  # noqa: BLE001 - bookkeeping must never stop a session starting
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
