"""Shared verified-session and cleanup primitives for peer-cli (Python 3.9+)."""

import csv
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path

import register_session


IDENTITY_TOLERANCE_SECONDS = 2.0
SUBPROCESS_TIMEOUT_SECONDS = 20
DEFAULT_TIMEOUTS = {
    "AGENT_FINISH_SPAWN_TIMEOUT_SECONDS": 15.0,
    "AGENT_FINISH_ARMED_TIMEOUT_SECONDS": 10.0,
    "AGENT_FINISH_ACCEPT_TIMEOUT_SECONDS": 60.0,
    "AGENT_FINISH_REAPER_TIMEOUT_SECONDS": 600.0,
}
MAX_TIMEOUTS = {
    "AGENT_FINISH_SPAWN_TIMEOUT_SECONDS": 60.0,
    "AGENT_FINISH_ARMED_TIMEOUT_SECONDS": 60.0,
    "AGENT_FINISH_ACCEPT_TIMEOUT_SECONDS": 300.0,
    "AGENT_FINISH_REAPER_TIMEOUT_SECONDS": 3600.0,
}
FINAL_RESULT_STATUSES = frozenset({
    "cancelled", "not-accepted", "failed", "timeout", "succeeded",
    "branch-preserved", "session-closed",
})
TASK_SCHEDULER_NAMESPACE = "http://schemas.microsoft.com/windows/2004/02/mit/task"
SCHEDULER_DIAGNOSTIC_LIMIT = 1000

ET.register_namespace("", TASK_SCHEDULER_NAMESPACE)


class Refusal(RuntimeError):
    pass


def _update_own_end(entry, operation, reason):
    try:
        store = register_session._load_session_store()
        if store is None or not entry.get("host") or not entry.get("session_id"):
            return
        connection = store.connect()
        try:
            getattr(store, operation)(connection, store.session_handle(entry["host"], entry["session_id"]), reason)
        finally:
            connection.close()
    except Exception:
        pass


def delete_own_session(session_id):
    try:
        store = register_session._load_session_store()
        if store is None:
            return
        connection = store.connect()
        try:
            store.delete_session(connection, session_id)
        finally:
            connection.close()
    except Exception:
        pass


def _record_own_end(entry, reason):
    _update_own_end(entry, "end_session", reason)


def _reopen_own_end(entry, reason):
    _update_own_end(entry, "reopen_session", reason)


def awaiting_input_activity(entry, expected_activity=None):
    if not valid_entry(entry) or not verified_live(entry["pid"], entry["pid_started_at"]):
        raise Refusal("registered host identity is not live")
    try:
        store = register_session._load_session_store()
        if store is None:
            raise Refusal("session turn state is unavailable")
        connection = store.connect()
        try:
            row = store.get_session(
                connection, store.session_handle(entry["host"], entry["session_id"]),
            )
        finally:
            connection.close()
    except Refusal:
        raise
    except Exception as error:
        raise Refusal("cannot read session turn state") from error
    if not isinstance(row, dict):
        raise Refusal("session turn state is missing")
    if (row.get("host") != entry["host"] or row.get("session_id") != entry["session_id"]
            or row.get("pid") != entry["pid"]
            or row.get("pid_started_at") != entry["pid_started_at"]
            or not nonempty_string(row.get("cwd"))
            or path_key(row["cwd"]) != path_key(entry["cwd"])):
        raise Refusal("session turn state does not match this attachment")
    if row.get("ended_at") is not None:
        raise Refusal("session is already ended")
    if row.get("turn_state") != "awaiting_input":
        raise Refusal("session is not awaiting input")
    activity = row.get("last_activity_at")
    if not finite_positive(activity):
        raise Refusal("session activity state is invalid")
    if expected_activity is not None and activity != expected_activity:
        raise Refusal("session activity changed before close")
    return activity


def state_directory():
    return Path(os.environ.get("AGENT_STATE_DIRECTORY") or Path.home() / ".agents-state")


def now():
    return time.time()


def finite_positive(value):
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value) and value > 0)


def nonempty_string(value):
    return isinstance(value, str) and bool(value.strip())


def bounded_timeout(name):
    default = DEFAULT_TIMEOUTS[name]
    try:
        value = float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default
    if not math.isfinite(value) or value <= 0 or value > MAX_TIMEOUTS[name]:
        return default
    return value


def path_key(value):
    path = Path(value).resolve()
    text = path.as_posix().rstrip("/")
    return text.casefold() if os.name == "nt" else text


def under_or_equal(candidate, root):
    candidate_key, root_key = path_key(candidate), path_key(root)
    return candidate_key == root_key or candidate_key.startswith(root_key + "/")


def read_json(path):
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def write_json(path, value, exclusive=False):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = (json.dumps(value, sort_keys=True) + "\n").encode("utf-8")
    if exclusive:
        try:
            descriptor = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            return False
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
        return True
    descriptor, temporary = tempfile.mkstemp(prefix="." + path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return True


def process_info(pid):
    state, info = process_state(pid)
    return info if state == "present" else None


def valid_identity(pid, started_at):
    return (isinstance(pid, int) and not isinstance(pid, bool) and pid > 0
            and finite_positive(started_at))


def process_state(pid):
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return "unknown", None
    try:
        info = register_session.build_process_lookup()(pid)
    except (OSError, ValueError, OverflowError):
        return "unknown", None
    except Exception:
        return "unknown", None
    if info is None:
        return "absent", None
    if not valid_identity(info.pid, info.started_at):
        return "unknown", None
    return "present", info


def verified_live(pid, started_at):
    if not valid_identity(pid, started_at):
        return False
    state, current = process_state(pid)
    return (state == "present" and
            abs(current.started_at - started_at) <= IDENTITY_TOLERANCE_SECONDS)


def identity_status(pid, started_at):
    """Classify a recorded process without treating failed inspection as exit."""
    if not valid_identity(pid, started_at):
        return "unknown"
    state, current = process_state(pid)
    if state == "absent":
        return "exited"
    if state != "present":
        return "unknown"
    if abs(current.started_at - started_at) > IDENTITY_TOLERANCE_SECONDS:
        return "reused"
    if sys.platform == "linux" and process_is_zombie(pid):
        return "exited"
    return "live"


def process_is_zombie(pid):
    if sys.platform != "linux":
        return False
    try:
        raw = Path("/proc") / str(pid) / "stat"
        fields = raw.read_text(encoding="utf-8").rsplit(")", 1)[1].split()
    except (OSError, IndexError):
        return False
    return bool(fields and fields[0] == "Z")


def verified_exited(pid, started_at):
    return identity_status(pid, started_at) == "exited"


def qualified_windows_wrapper(entry):
    """Return only the shell launcher demonstrably attached to this host."""
    if os.name != "nt" or not valid_entry(entry):
        return None
    host = process_info(entry["pid"])
    if (host is None or abs(host.started_at - entry["pid_started_at"])
            > IDENTITY_TOLERANCE_SECONDS):
        return None
    try:
        table = register_session._windows_process_table()
    except (OSError, ValueError):
        return None
    wrapper = table.get(host.ppid)
    shell_names = {"pwsh", "powershell", "cmd", "bash", "sh", "zsh", "fish", "nu"}
    if (wrapper is None or register_session.stem(wrapper.name) not in shell_names
            or not valid_identity(wrapper.pid, wrapper.started_at)
            or wrapper.started_at > host.started_at
            or host.started_at - wrapper.started_at > 10.0):
        return None
    console_names = {"conhost", "openconsole"}
    for child in table.values():
        if child.ppid != wrapper.pid or child.pid == host.pid:
            continue
        if register_session.stem(child.name) not in console_names:
            return None
    try:
        command = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
             "(Get-CimInstance -Class Win32_Process -Filter 'ProcessId = %d').CommandLine" % wrapper.pid],
            capture_output=True, text=True, encoding="utf-8", check=False,
            timeout=SUBPROCESS_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    host_name = re.escape(register_session.stem(host.name))
    if command.returncode or not re.search(r"(?<![A-Za-z0-9_.-])" + host_name + r"(?:\.exe)?(?![A-Za-z0-9_.-])", command.stdout, re.IGNORECASE):
        return None
    return {"pid": wrapper.pid, "started_at": wrapper.started_at}


def entries():
    directory = state_directory() / "cli-sessions"
    for path in directory.glob("*.json") if directory.is_dir() else ():
        entry = read_json(path)
        if entry:
            yield path, entry


def valid_entry(entry):
    return (isinstance(entry, dict)
            and nonempty_string(entry.get("cwd"))
            and nonempty_string(entry.get("session_id"))
            and nonempty_string(entry.get("host"))
            and valid_identity(entry.get("pid"), entry.get("pid_started_at"))
            and finite_positive(entry.get("started_at")))


def liveness(entry):
    if not valid_entry(entry):
        return None
    status = identity_status(entry["pid"], entry["pid_started_at"])
    return True if status == "live" else False if status in {"exited", "reused"} else None


def attached_to(cwd, entry):
    return any(nonempty_string(root) and under_or_equal(cwd, root)
               for root in (entry.get("cwd"), entry.get("launch_cwd")))


def own_host_and_entry(cwd=None):
    cwd = Path(cwd or os.getcwd()).resolve()
    host_names = register_session.read_host_names()
    seen = set()
    current = os.getppid()
    nearest = None
    for _ in range(register_session.MAX_ANCESTOR_DEPTH):
        if current in seen:
            break
        seen.add(current)
        info = process_info(current)
        if info is None:
            break
        if register_session.stem(info.name) in host_names:
            nearest = info
            break
        current = info.ppid
    if nearest is None:
        raise Refusal("current process is not attached to a recognized registered host")
    matches = []
    for _path, entry in entries():
        if not valid_entry(entry) or not attached_to(cwd, entry):
            continue
        if (entry["pid"] == nearest.pid
                and entry["host"] == register_session.stem(nearest.name)
                and entry["pid_started_at"] == nearest.started_at
                and verified_live(entry["pid"], entry["pid_started_at"])):
            matches.append(entry)
    if matches:
        latest = max(item["started_at"] for item in matches)
        recent = [item for item in matches if item["started_at"] == latest]
        if len(recent) == 1:
            return recent[0]
        raise Refusal("multiple equally recent verified registry attachments match this host")
    raise Refusal(
        "cannot resolve a verified registry attachment for this host and current directory; "
        "do not force-close the host"
    )


def same_session_process(left, right):
    return (
        isinstance(left, dict)
        and isinstance(right, dict)
        and nonempty_string(left.get("session_id"))
        and left.get("session_id") == right.get("session_id")
        and valid_identity(left.get("pid"), left.get("pid_started_at"))
        and left.get("pid") == right.get("pid")
        and left.get("pid_started_at") == right.get("pid_started_at")
    )


def git(cwd, *arguments):
    try:
        result = subprocess.run(
            ["git", "-C", str(cwd), *arguments], text=True, encoding="utf-8",
            capture_output=True, check=False, timeout=SUBPROCESS_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return 1, "", "git command failed: " + str(error)
    return result.returncode, result.stdout.strip(), result.stderr.strip()


def worktree_from_cwd(cwd=None):
    code, output, _error = git(cwd or os.getcwd(), "rev-parse", "--show-toplevel")
    if code:
        raise Refusal("the current directory is not inside a Git worktree")
    return Path(output).resolve()


def scheduler_diagnostic(completed):
    parts = []
    for name in ("stderr", "stdout"):
        value = getattr(completed, name, "")
        if value:
            parts.append(name + "=" + " ".join(str(value).split()))
    detail = "; ".join(parts)
    if len(detail) > SCHEDULER_DIAGNOSTIC_LIMIT:
        detail = detail[:SCHEDULER_DIAGNOSTIC_LIMIT] + "..."
    suffix = ": " + detail if detail else ""
    return "exit " + str(completed.returncode) + suffix


def task_xml_element(name, text=None, **attributes):
    element = ET.Element("{" + TASK_SCHEDULER_NAMESPACE + "}" + name, attributes)
    if text is not None:
        element.text = text
    return element


def windows_task_xml(user, command, working_directory):
    task = task_xml_element("Task", version="1.3")
    registration = ET.SubElement(task, "{" + TASK_SCHEDULER_NAMESPACE + "}RegistrationInfo")
    registration.append(task_xml_element("Author", user))
    principals = ET.SubElement(task, "{" + TASK_SCHEDULER_NAMESPACE + "}Principals")
    principal = ET.SubElement(
        principals, "{" + TASK_SCHEDULER_NAMESPACE + "}Principal", {"id": "CurrentUser"},
    )
    principal.append(task_xml_element("UserId", user))
    principal.append(task_xml_element("LogonType", "InteractiveToken"))
    principal.append(task_xml_element("RunLevel", "LeastPrivilege"))
    settings = ET.SubElement(task, "{" + TASK_SCHEDULER_NAMESPACE + "}Settings")
    settings.append(task_xml_element("AllowStartOnDemand", "true"))
    settings.append(task_xml_element("MultipleInstancesPolicy", "IgnoreNew"))
    settings.append(task_xml_element("DisallowStartIfOnBatteries", "false"))
    settings.append(task_xml_element("StopIfGoingOnBatteries", "false"))
    settings.append(task_xml_element("ExecutionTimeLimit", "PT0S"))
    settings.append(task_xml_element("Hidden", "true"))
    actions = ET.SubElement(task, "{" + TASK_SCHEDULER_NAMESPACE + "}Actions", {"Context": "CurrentUser"})
    execute = ET.SubElement(actions, "{" + TASK_SCHEDULER_NAMESPACE + "}Exec")
    execute.append(task_xml_element("Command", command[0]))
    execute.append(task_xml_element("Arguments", subprocess.list2cmdline(command[1:])))
    execute.append(task_xml_element("WorkingDirectory", str(working_directory)))
    return ET.tostring(task, encoding="utf-16", xml_declaration=True)


def current_windows_user(kwargs, timeout):
    completed = subprocess.run(
        ["whoami.exe", "/user", "/fo", "csv", "/nh"], **kwargs, check=False, timeout=timeout,
    )
    rows = list(csv.reader(completed.stdout.splitlines()))
    user = rows[0][-1].strip() if len(rows) == 1 and rows[0] else ""
    if completed.returncode or re.fullmatch(r"S-1-\d+(?:-\d+)+", user) is None:
        raise Refusal(
            "could not determine the current user for detached cleanup observer ("
            + scheduler_diagnostic(completed) + ")"
        )
    return user


def start_observer(arguments):
    return start_detached(Path(__file__).with_name("finish_reaper.py"), arguments)


def start_detached(script, arguments, extra=()):
    """Run a staged script outside the host's process tree, so it outlives the host closing."""
    staging = state_directory() / "merge-cleanup" / "observers" / uuid.uuid4().hex
    staging.mkdir(parents=True, exist_ok=False)
    here = Path(__file__)
    for path in (Path(script), here, here.with_name("register_session.py"), *map(Path, extra)):
        shutil.copy2(path, staging / path.name)
    store = register_session.session_store_path()
    if store is not None:
        for name in ("session_store.py", "repository_identity.py"):
            shutil.copy2(store.with_name(name), staging / name)
    command = [sys.executable, str(staging / Path(script).name), *arguments]
    kwargs = {"cwd": str(state_directory()), "stdin": subprocess.DEVNULL,
              "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    timeout = bounded_timeout("AGENT_FINISH_SPAWN_TIMEOUT_SECONDS")
    if os.name == "nt":
        task = "agent-finish-reaper-" + uuid.uuid4().hex
        scheduler_kwargs = {
            "cwd": str(state_directory()), "stdin": subprocess.DEVNULL,
            "capture_output": True, "text": True, "encoding": "utf-8", "errors": "replace",
        }
        descriptor, xml_path = tempfile.mkstemp(
            prefix=".agent-finish-reaper-", suffix=".xml", dir=str(state_directory()),
        )
        registered = False
        try:
            user = current_windows_user(scheduler_kwargs, timeout)
            stream = os.fdopen(descriptor, "wb")
            descriptor = None
            with stream:
                stream.write(windows_task_xml(user, command, state_directory()))
            registered = True
            create = subprocess.run(
                ["schtasks.exe", "/create", "/tn", task, "/xml", xml_path, "/f"],
                **scheduler_kwargs, check=False, timeout=timeout,
            )
            if create.returncode:
                raise Refusal(
                    "could not create a detached cleanup observer ("
                    + scheduler_diagnostic(create) + ")"
                )
            run = subprocess.run(["schtasks.exe", "/run", "/tn", task], **scheduler_kwargs,
                                 check=False, timeout=timeout)
            if run.returncode:
                raise Refusal(
                    "could not start a detached cleanup observer ("
                    + scheduler_diagnostic(run) + ")"
                )
        except subprocess.TimeoutExpired as error:
            raise Refusal("timed out starting detached cleanup observer") from error
        finally:
            if descriptor is not None:
                os.close(descriptor)
            Path(xml_path).unlink(missing_ok=True)
            if registered:
                try:
                    subprocess.run(["schtasks.exe", "/delete", "/tn", task, "/f"],
                                   **scheduler_kwargs, check=False, timeout=timeout)
                except subprocess.TimeoutExpired:
                    pass
        return None
    return subprocess.Popen(command, start_new_session=True, **kwargs).pid


def wait_for_record(path, timeout):
    deadline = now() + timeout
    while now() < deadline:
        record = read_json(path)
        if record:
            return record
        time.sleep(.05)
    return None


def cancel_observer(result_path, reason):
    write_json(str(result_path) + ".cancelled", {"cancelled": now(), "reason": reason})


def binding(host_pid, host_start, worktree, session_id, invocation_id, cleanup=None):
    value = {
        "host_pid": host_pid,
        "host_start": host_start,
        "worktree": str(Path(worktree).resolve()),
        "session_id": session_id,
        "invocation_id": invocation_id,
    }
    if cleanup:
        value.update(cleanup)
    return value


def record_matches_binding(record, expected):
    return isinstance(record, dict) and all(record.get(key) == value for key, value in expected.items())


def valid_observer_start(record, observer_pid, host_pid, host_start, worktree, session_id,
                         invocation_id=None, cleanup=None, minimum_started=None):
    expected = binding(host_pid, host_start, worktree, session_id, invocation_id, cleanup)
    if (not valid_identity(host_pid, host_start) or not nonempty_string(session_id)
            or not nonempty_string(invocation_id)):
        return False
    if not record_matches_binding(record, expected):
        return False
    if record.get("status") in FINAL_RESULT_STATUSES:
        return False
    if not finite_positive(record.get("started")) or record["started"] > now() + IDENTITY_TOLERANCE_SECONDS:
        return False
    if minimum_started is not None and record["started"] + IDENTITY_TOLERANCE_SECONDS < minimum_started:
        return False
    if not valid_identity(record.get("reaper_pid"), record.get("reaper_started_at")):
        return False
    if observer_pid is not None and record["reaper_pid"] != observer_pid:
        return False
    return verified_live(record["reaper_pid"], record["reaper_started_at"])


def valid_acknowledgement(record, expected, observer_record):
    if not record_matches_binding(record, expected):
        return False
    if not finite_positive(record.get("accepted")) or record["accepted"] < observer_record["started"]:
        return False
    if record["accepted"] > now() + IDENTITY_TOLERANCE_SECONDS:
        return False
    return (record.get("reaper_pid") == observer_record.get("reaper_pid")
            and record.get("reaper_started_at") == observer_record.get("reaper_started_at")
            and record.get("observer_pid") == observer_record.get("reaper_pid")
            and record.get("observer_started_at") == observer_record.get("reaper_started_at"))


def valid_armed(record, expected, observer_record, accepted):
    if not record_matches_binding(record, expected):
        return False
    if not finite_positive(record.get("armed")) or record["armed"] < accepted["accepted"]:
        return False
    if record["armed"] > now() + IDENTITY_TOLERANCE_SECONDS:
        return False
    return (record.get("reaper_pid") == observer_record.get("reaper_pid")
            and record.get("reaper_started_at") == observer_record.get("reaper_started_at")
            and record.get("observer_pid") == observer_record.get("reaper_pid")
            and record.get("observer_started_at") == observer_record.get("reaper_started_at")
            and verified_live(record["observer_pid"], record["observer_started_at"]))


def record_close_refusal(entry, script, arguments, error):
    if not valid_entry(entry):
        return None
    script = Path(script).resolve()
    value = {
        "argv": [sys.executable, "-B", str(script), *arguments],
        "cwd": os.getcwd(),
        "resolved_target": {key: entry[key] for key in ("session_id", "cwd", "pid", "pid_started_at")},
        "returncode": 2,
        "refusal_output": f"{script.stem}: {error}",
        "observed_at": now(),
    }
    path = state_directory() / "merge-cleanup" / "close-attempts" / (uuid.uuid4().hex + ".json")
    try:
        write_json(path, {"capability": value})
    except OSError as capture_error:
        print(f"{script.stem}: cannot record refusal evidence: {capture_error}", file=sys.stderr)
        return None
    print(f"{script.stem}: refusal evidence: {path}", file=sys.stderr)
    return path


def results_path():
    return state_directory() / "merge-cleanup" / "results" / (uuid.uuid4().hex + ".json")


def remove_matching_obligations(worktree, session_id):
    directory = state_directory() / "merge-cleanup" / "obligations"
    for path in directory.glob("*.json") if directory.is_dir() else ():
        obligation = read_json(path)
        if not obligation:
            continue
        if (obligation.get("session_id") == session_id and obligation.get("worktree")
                and path_key(worktree) == path_key(obligation["worktree"])):
            path.unlink(missing_ok=True)
