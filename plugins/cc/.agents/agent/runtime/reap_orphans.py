r"""Reap agent processes whose session is gone.

A closed terminal does not always take its `claude` process with it. The survivors hold their full
heap forever: 21 of them were measured on one machine holding 9.43 GB of private commit, which is
what starved a container stack of memory for hours.

`SessionEnd` cannot fix this. An orphan is by definition a process that did not shut down cleanly,
so the exit hook is exactly the one that did not fire for it. Running on `SessionStart` is
guaranteed to run and is self-healing.

Age alone is not the signal — a session deliberately left open for two days is indistinguishable
from a zombie by age. The signal is that whatever owned the session is gone. Reap only when all of:

1. the owning parent process or the owning console host is gone, and
2. the process is older than a grace period, so a session still starting is never a target, and
3. the process is outside this session's own tree.

Both owners in (1) are needed, because the two ways a session is launched here fail differently. A
session started from a shell prompt is a child of that shell, and closing the tab kills the shell —
parent-gone catches it. A session started by a terminal launcher such as handoff-claude's is spawned
by `wt.exe` and so is a direct child of `WindowsTerminal.exe`, which outlives the tab it was closed
in; parent-gone never fires for those, and they are the ones this machine accumulates. What does die
with the tab is the `OpenConsole.exe` hosting it, which `ProcessConsoleHostProcess` names.

Condition 3 is the one that causes real damage if wrong, so the protected set is computed first and
is deliberately generous: this process, every ancestor, every descendant.

Nothing is killed by default. The `SessionStart` hook runs `--notice`, which only reports; the
destructive path needs `--apply` from a human or the `AGENT_REAP_ORPHANS` opt-in.
"""

import argparse
import collections
import json
import math
import os
import sys
import time
from pathlib import Path


STATE_DIRECTORY_ENV = "AGENT_STATE_DIRECTORY"
APPLY_ENV = "AGENT_REAP_ORPHANS"
NOTICE_FILE = "orphan-reaper-notice.json"
NOTICE_INTERVAL_SECONDS = 60 * 60
GRACE_SECONDS = 30 * 60
DEFAULT_PROCESS_NAMES = ("claude",)
TRUTHY = frozenset({"1", "true", "yes", "on"})

EXIT_OK = 0
EXIT_UNSUPPORTED = 2
EXIT_TERMINATION = 3

Process = collections.namedtuple(
    "Process", "pid parent_pid name started_at private_bytes console_host_pid terminal_pid"
)
Process.__new__.__defaults__ = (None, None, None)


def state_directory(environ=None, home=None):
    values = os.environ if environ is None else environ
    configured = values.get(STATE_DIRECTORY_ENV)
    if configured:
        return Path(configured)
    return (Path.home() if home is None else home) / ".agents-state"


def apply_is_permitted(environ=None):
    values = os.environ if environ is None else environ
    return (values.get(APPLY_ENV) or "").strip().casefold() in TRUTHY


def name_matches(name, targets):
    lowered = (name or "").casefold()
    return lowered in targets or Path(lowered).stem in targets


def index_by_pid(table):
    return {process.pid: process for process in table}


def children_by_parent(table):
    children = {}
    for process in table:
        children.setdefault(process.parent_pid, []).append(process.pid)
    return children


def valid_pid(value):
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def valid_started_at(value):
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and value > 0
    )


def has_valid_identity(process):
    return valid_pid(process.pid) and valid_started_at(process.started_at)


def own_ancestry_is_known(pid, table):
    """Whether this run can identify its own ancestry without guessing past an opaque row."""
    by_pid = index_by_pid(table)
    current = by_pid.get(pid)
    seen = set()
    while current is not None:
        if current.pid in seen or not valid_pid(current.parent_pid):
            return current.pid in seen or type(current.parent_pid) is int and current.parent_pid == 0
        seen.add(current.pid)
        parent = by_pid.get(current.parent_pid)
        if parent is None:
            return True
        current = parent
    return False


def own_tree(pid, table):
    """Every pid this run must never touch: itself, its ancestors, its descendants.

    The ancestor walk trusts the reported parent pid without checking that it is the genuine parent.
    A reused pid therefore protects an unrelated process, which is the harmless direction.
    """
    by_pid = index_by_pid(table)
    protected = {pid}
    current = by_pid.get(pid)
    while current is not None and current.parent_pid not in protected:
        protected.add(current.parent_pid)
        current = by_pid.get(current.parent_pid)

    children = children_by_parent(table)
    pending = [pid]
    while pending:
        current_pid = pending.pop()
        for child in children.get(current_pid, ()):
            if child not in protected:
                protected.add(child)
                pending.append(child)
    return protected


def parent_is_gone(process, by_pid):
    """A pid is reused the moment it is free, so a live pid is the parent only if it predates the child."""
    parent = by_pid.get(process.parent_pid)
    if parent is None:
        return True
    if parent.started_at is None or process.started_at is None:
        return False
    return parent.started_at > process.started_at


def console_is_gone(process, by_pid):
    """Whether the console host that owned this process has exited.

    Deliberately presence-only. The reuse check that works for a parent is invalid here: a console
    host is created *for* the process and routinely starts a moment after it, so comparing start
    times marks every console-attached process as orphaned. A reused host pid therefore spares an
    orphan until the next session, which is the harmless direction.

    Host 0 means the process has no console at all, which is unknowable rather than orphaned.
    """
    if not process.console_host_pid:
        return False
    return process.console_host_pid not in by_pid


def terminal_is_gone(process, by_pid):
    """Linux session ownership is a second signal after init/subreaper reparenting.

    A terminal close can reparent its child to init or a subreaper, so parent liveness alone is not
    ownership.  `/proc/<pid>/stat` records the session leader; when that verified terminal/session
    owner is absent from the same snapshot, the agent is a candidate.  Missing information is unknown,
    never evidence for termination.
    """
    if not process.terminal_pid or process.terminal_pid == process.pid:
        return False
    return process.terminal_pid not in by_pid


def candidates(table, self_pid, now, names=DEFAULT_PROCESS_NAMES, grace=GRACE_SECONDS):
    """Named processes old enough to judge and outside this session's own tree."""
    if not own_ancestry_is_known(self_pid, table):
        return []
    targets = {str(name).casefold() for name in names}
    protected = own_tree(self_pid, table)
    return [
        process
        for process in table
        if name_matches(process.name, targets)
        and process.pid not in protected
        and has_valid_identity(process)
        and now - process.started_at >= grace
    ]


def orphans_among(candidates_, table):
    by_pid = index_by_pid(table)
    orphans = [
        process
        for process in candidates_
        if parent_is_gone(process, by_pid) or console_is_gone(process, by_pid) or terminal_is_gone(process, by_pid)
    ]
    return sorted(orphans, key=lambda process: process.started_at)


def find_orphans(table, self_pid, now, names, grace, enrich=None):
    matched = candidates(table, self_pid, now, names, grace)
    if enrich is not None:
        matched = enrich(matched)
    return orphans_among(matched, table)


def human_bytes(value):
    if not value:
        return "0 B"
    size = float(value)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.2f} {unit}"
        size /= 1024
    return f"{size:.2f} TB"


def human_age(seconds):
    if seconds < 3600:
        return f"{int(seconds // 60)}m"
    if seconds < 86400:
        return f"{seconds / 3600:.1f}h"
    return f"{seconds / 86400:.1f}d"


def reclaimable(orphans):
    return sum(process.private_bytes or 0 for process in orphans)


MAX_PATH = 260
TH32CS_SNAPPROCESS = 0x00000002
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
PROCESS_VM_READ = 0x0010
PROCESS_TERMINATE = 0x0001
PROCESS_CONSOLE_HOST_INFORMATION = 49
CONSOLE_HOST_PID_MASK = ~3
INVALID_HANDLE_VALUE = -1
FILETIME_EPOCH_DELTA = 11644473600
FILETIME_TICKS_PER_SECOND = 10_000_000


def _kernel32():
    import ctypes
    from ctypes import wintypes

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
    kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel32.TerminateProcess.restype = wintypes.BOOL
    kernel32.GetProcessTimes.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
    ]
    kernel32.GetProcessTimes.restype = wintypes.BOOL
    return kernel32


def _console_host_pid(kernel32, pid):
    """The pid of the conhost/OpenConsole hosting `pid`, 0 when it has no console, None when unknown.

    `ProcessConsoleHostProcess` is not in the documented information-class set, so an unexpected
    status is reported as unknown rather than guessed at. The low two bits are flags, not pid.
    """
    import ctypes
    from ctypes import wintypes

    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return None
    try:
        ntdll = ctypes.WinDLL("ntdll", use_last_error=True)
        value = ctypes.c_size_t(0)
        written = wintypes.ULONG(0)
        status = ntdll.NtQueryInformationProcess(
            handle,
            PROCESS_CONSOLE_HOST_INFORMATION,
            ctypes.byref(value),
            ctypes.sizeof(value),
            ctypes.byref(written),
        )
        if status != 0:
            return None
        return int(value.value) & CONSOLE_HOST_PID_MASK
    except (OSError, AttributeError):
        return None
    finally:
        kernel32.CloseHandle(handle)


def _process_entry_type():
    import ctypes
    from ctypes import wintypes

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

    return PROCESSENTRY32W


def _memory_counters_type():
    import ctypes
    from ctypes import wintypes

    class PROCESS_MEMORY_COUNTERS_EX(ctypes.Structure):
        _fields_ = [
            ("cb", wintypes.DWORD),
            ("PageFaultCount", wintypes.DWORD),
            ("PeakWorkingSetSize", ctypes.c_size_t),
            ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t),
            ("PeakPagefileUsage", ctypes.c_size_t),
            ("PrivateUsage", ctypes.c_size_t),
        ]

    return PROCESS_MEMORY_COUNTERS_EX


def _filetime_to_epoch(filetime):
    ticks = (filetime.dwHighDateTime << 32) | filetime.dwLowDateTime
    if not ticks:
        return None
    return ticks / FILETIME_TICKS_PER_SECOND - FILETIME_EPOCH_DELTA


def _started_at(kernel32, pid):
    """When `pid` started, or None when the process cannot be opened or queried."""
    import ctypes
    from ctypes import wintypes

    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return None
    try:
        creation, exited, kernel, user = (wintypes.FILETIME() for _ in range(4))
        if not kernel32.GetProcessTimes(
            handle,
            ctypes.byref(creation),
            ctypes.byref(exited),
            ctypes.byref(kernel),
            ctypes.byref(user),
        ):
            return None
        return _filetime_to_epoch(creation)
    finally:
        kernel32.CloseHandle(handle)


def _private_bytes(kernel32, pid):
    """Private commit for `pid`, or None. Read only for candidates - the handle is expensive."""
    import ctypes

    handle = kernel32.OpenProcess(
        PROCESS_QUERY_LIMITED_INFORMATION | PROCESS_VM_READ, False, pid
    )
    if not handle:
        return None
    try:
        counters_type = _memory_counters_type()
        counters = counters_type()
        counters.cb = ctypes.sizeof(counters_type)
        psapi = ctypes.WinDLL("psapi", use_last_error=True)
        if not psapi.GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb):
            return None
        return int(counters.PrivateUsage)
    except OSError:
        return None
    finally:
        kernel32.CloseHandle(handle)


def windows_process_table():
    import ctypes

    kernel32 = _kernel32()
    entry_type = _process_entry_type()
    snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snapshot in (None, 0) or ctypes.c_ssize_t(snapshot).value == INVALID_HANDLE_VALUE:
        raise OSError(ctypes.get_last_error(), "cannot snapshot the process table")
    entry = entry_type()
    entry.dwSize = ctypes.sizeof(entry_type)
    table = []
    try:
        if not kernel32.Process32FirstW(snapshot, ctypes.byref(entry)):
            return table
        while True:
            pid = int(entry.th32ProcessID)
            table.append(
                Process(
                    pid=pid,
                    parent_pid=int(entry.th32ParentProcessID),
                    name=str(entry.szExeFile),
                    started_at=_started_at(kernel32, pid),
                    private_bytes=None,
                )
            )
            if not kernel32.Process32NextW(snapshot, ctypes.byref(entry)):
                return table
    finally:
        kernel32.CloseHandle(snapshot)


def _linux_boot_time(proc_root=Path("/proc")):
    with (proc_root / "stat").open(encoding="utf-8") as handle:
        for line in handle:
            if line.startswith("btime "):
                return float(line.split()[1])
    raise OSError("/proc/stat has no btime")


def _opaque_process(pid):
    return Process(pid, None, None, None, None, None, None)


def linux_process_table(proc_root=None, boot=None, ticks=None):
    """A conservative `/proc` snapshot that preserves unreadable owners as opaque rows.

    A failure to inspect the snapshot itself returns ``None``. A process that vanishes while its
    stat file is read is omitted; malformed or inaccessible stat data remains present but unknown.
    """
    proc_root = Path("/proc") if proc_root is None else proc_root
    try:
        boot = _linux_boot_time(proc_root) if boot is None else boot
        ticks = os.sysconf("SC_CLK_TCK") if ticks is None else ticks
        if not valid_started_at(boot) or not valid_started_at(ticks):
            return None
        entries = proc_root.iterdir()
    except (OSError, ValueError):
        return None
    table = []
    try:
        for child in entries:
            if not child.name.isdigit():
                continue
            pid = int(child.name)
            try:
                raw = (child / "stat").read_text(encoding="utf-8")
                close = raw.rindex(")")
                name = raw[raw.index("(") + 1:close]
                fields = raw[close + 2:].split()
                # state, ppid, pgrp, session, tty_nr, ... starttime (field 22).
                parent, session, started_ticks = int(fields[1]), int(fields[3]), int(fields[19])
                started_at = boot + started_ticks / ticks
                if not valid_pid(parent) and parent != 0:
                    raise ValueError("invalid parent pid")
                if not valid_pid(session) or not valid_started_at(started_at):
                    raise ValueError("invalid process identity")
                table.append(Process(pid, parent, name, started_at, None, None, session))
            except FileNotFoundError:
                if child.exists():
                    table.append(_opaque_process(pid))
            except (OSError, ValueError, IndexError):
                table.append(_opaque_process(pid))
    except OSError:
        return None
    return table


def read_process_table():
    if sys.platform == "linux":
        try:
            return linux_process_table()
        except (OSError, ValueError):
            return None
    if os.name != "nt":
        return None
    try:
        return windows_process_table()
    except OSError:
        return None


def with_details(processes):
    """Fill in the per-candidate facts that are too expensive to read for the whole table."""
    if os.name != "nt" or not processes:
        return list(processes)
    try:
        kernel32 = _kernel32()
    except OSError:
        return list(processes)
    return [
        process._replace(
            private_bytes=_private_bytes(kernel32, process.pid),
            console_host_pid=_console_host_pid(kernel32, process.pid),
        )
        for process in processes
    ]


def terminate(process):
    """Terminate one process, refusing if the pid no longer names the process that was classified.

    Classification and termination are separate passes, and Windows reuses a pid the moment it is
    free, so the start time is re-read through a fresh handle rather than trusted from the snapshot.
    """
    if not has_valid_identity(process):
        return "cannot confirm identity"
    if sys.platform == "linux":
        fresh_table = linux_process_table()
        if fresh_table is None:
            return "cannot confirm identity"
        current = next((item for item in fresh_table if item.pid == process.pid), None)
        if current is None or not has_valid_identity(current):
            return "cannot confirm identity"
        if abs(current.started_at - process.started_at) > 1:
            return "pid was reused"
        try:
            os.kill(process.pid, 15)
        except OSError as error:
            return "termination refused ({})".format(error)
        return None
    if os.name != "nt":
        return "unsupported platform"
    import ctypes

    kernel32 = _kernel32()
    current = _started_at(kernel32, process.pid)
    if not valid_started_at(current):
        return "cannot confirm identity"
    if abs(current - process.started_at) > 1:
        return "pid was reused"
    handle = kernel32.OpenProcess(PROCESS_TERMINATE, False, process.pid)
    if not handle:
        return f"cannot open for termination (error {ctypes.get_last_error()})"
    try:
        if not kernel32.TerminateProcess(handle, 1):
            return f"termination refused (error {ctypes.get_last_error()})"
    finally:
        kernel32.CloseHandle(handle)
    return None


def render_report(orphans, now, applied, failures, stream):
    if not orphans:
        print("no orphaned agent processes", file=stream)
        return
    verb = "reaped" if applied else "would reap"
    print(
        f"{verb} {len(orphans) - len(failures)} of {plural(len(orphans), 'orphaned process')}",
        file=stream,
    )
    for process in orphans:
        age = human_age(now - process.started_at)
        detail = f"  {process.pid:<8} {process.name:<16} {age:>7}  {human_bytes(process.private_bytes):>10}"
        if process.pid in failures:
            print(f"{detail}  FAILED {failures[process.pid]}", file=stream)
        else:
            print(detail, file=stream)
    print(f"{'reclaimed' if applied else 'reclaimable'}: {human_bytes(reclaimable(orphans))}", file=stream)
    if not applied:
        print(f"re-run with --apply to reap them: {Path(__file__).resolve()}", file=stream)


def notice_is_due(path, now, interval=NOTICE_INTERVAL_SECONDS):
    try:
        last = float(json.loads(path.read_text(encoding="utf-8")).get("last_notice", 0))
    except (OSError, ValueError, TypeError, AttributeError):
        return True
    return not 0 <= now - last < interval


def record_notice(path, now):
    path.parent.mkdir(parents=True, exist_ok=True)
    staging = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    staging.write_text(json.dumps({"last_notice": now}) + "\n", encoding="utf-8")
    os.replace(staging, path)


def plural(count, singular, suffix="es"):
    return f"{count} {singular}" if count == 1 else f"{count} {singular}{suffix}"


def notice_text(orphans, now, applied, failures):
    oldest = human_age(now - orphans[0].started_at)
    total = human_bytes(reclaimable(orphans))
    if applied:
        reaped = plural(len(orphans) - len(failures), "agent process")
        text = f"orphan reaper: reaped {reaped} whose session is gone, freeing {total}."
        if failures:
            text += f" {plural(len(failures), 'process')} could not be terminated."
        return text
    return (
        f"orphan reaper: {plural(len(orphans), 'agent process')} whose session is gone "
        f"{'is' if len(orphans) == 1 else 'are'} still resident "
        f"(oldest {oldest}, {total} private commit). Reap them with: "
        f'python -B "{Path(__file__).resolve()}" --apply'
    )


def emit_session_context(text, stream):
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "SessionStart",
                    "additionalContext": text,
                }
            },
            ensure_ascii=True,
        ),
        file=stream,
    )


def reap(orphans):
    failures = {}
    for process in orphans:
        failure = terminate(process)
        if failure:
            failures[process.pid] = failure
    return failures


def run_notice(
    table, self_pid, now, names, grace,
    environ=None, home=None, stream=sys.stdout, enrich=None,
):
    """One line when orphans are resident, silence otherwise. Contract: exit 0 whatever happens."""
    marker = state_directory(environ, home) / NOTICE_FILE
    if not notice_is_due(marker, now):
        return EXIT_OK
    orphans = find_orphans(table, self_pid, now, names, grace, enrich or with_details)
    if not orphans:
        record_notice(marker, now)
        return EXIT_OK
    applied = apply_is_permitted(environ)
    failures = reap(orphans) if applied else {}
    emit_session_context(notice_text(orphans, now, applied, failures), stream)
    record_notice(marker, now)
    return EXIT_OK


def run_report(table, self_pid, now, names, grace, apply_mode, stream=sys.stdout, enrich=None):
    orphans = find_orphans(table, self_pid, now, names, grace, enrich or with_details)
    failures = reap(orphans) if apply_mode and orphans else {}
    render_report(orphans, now, apply_mode, failures, stream)
    return EXIT_TERMINATION if failures else EXIT_OK


def parse_args(argv):
    parser = argparse.ArgumentParser(description="Reap agent processes whose session is gone.")
    parser.add_argument("--notice", action="store_true", help="SessionStart hook mode")
    parser.add_argument("--apply", action="store_true", help="terminate, rather than report")
    parser.add_argument(
        "--process",
        action="append",
        metavar="NAME",
        help=f"process name to reap, repeatable (default: {', '.join(DEFAULT_PROCESS_NAMES)})",
    )
    parser.add_argument(
        "--grace-minutes",
        type=float,
        default=GRACE_SECONDS / 60,
        help="minimum age before a process can be reaped",
    )
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    names = args.process or list(DEFAULT_PROCESS_NAMES)
    grace = max(0.0, args.grace_minutes) * 60
    now = time.time()

    table = read_process_table()
    if table is None:
        if args.notice:
            return EXIT_OK
        print("the process table is unavailable on this platform", file=sys.stderr)
        return EXIT_UNSUPPORTED

    if args.notice:
        try:
            return run_notice(table, os.getpid(), now, names, grace)
        except Exception:  # noqa: BLE001 - bookkeeping must never stop a session starting
            return EXIT_OK
    return run_report(table, os.getpid(), now, names, grace, args.apply)


if __name__ == "__main__":
    raise SystemExit(main())
