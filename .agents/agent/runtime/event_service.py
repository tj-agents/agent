from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import xml.etree.ElementTree as ET


NAMESPACE = "http://schemas.microsoft.com/windows/2004/02/mit/task"
TIMEOUT = 60


def receipt(directory: Path, claude_home: Path, codex_home: Path, search_path: str | None = None) -> dict:
    return {"schema_version": 1, "runtime": str((directory / "bin/.agents/agent/runtime/machine_events.py").resolve()),
            "claude_home": str(claude_home.resolve()), "codex_home": str(codex_home.resolve()),
            "search_path": os.environ.get("PATH", os.defpath) if search_path is None else search_path}


class ServiceError(RuntimeError):
    pass


def identity(context) -> str:
    state = str(context.state_directory.resolve())
    if context.windows:
        state = state.casefold()
    return "tj-agents-machine-events-" + hashlib.sha256(state.encode("utf-8")).hexdigest()[:16]


def marker(context) -> str:
    return f"Managed by tj-agents machine events ({identity(context)})"


def arguments(context) -> list[str]:
    script = context.state_directory / "bin/.agents/agent/runtime/machine_events.py"
    native = context.environ.get("TJ_BIN_DIR")
    native_arguments = ["--native-bin-dir=" + str(Path(native).expanduser().resolve())] if native else []
    return [str(script.resolve()), "--watch", "--state-directory", str(context.state_directory.resolve()),
            "--claude-home", str(context.claude_home.resolve()), "--codex-home", str(context.codex_home.resolve()),
            "--search-path=" + context.environ.get("PATH", os.defpath), *native_arguments]


def command(context, argv: list[str], *, allowed=(0,)):
    try:
        result = context.run(argv, capture_output=True, text=True, encoding="utf-8", errors="replace",
                             timeout=TIMEOUT, env=context.environ)
    except (OSError, subprocess.SubprocessError) as error:
        raise ServiceError(f"machine-event supervisor command failed: {error}") from error
    if result.returncode not in allowed:
        detail = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
        raise ServiceError(f"machine-event supervisor command failed: {detail}")
    return result


def powershell(context) -> str:
    executable = context.which("powershell.exe") or context.which("pwsh.exe")
    if not executable:
        raise ServiceError("PowerShell is required to manage the machine-event scheduled task")
    return executable


def ps_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def ps_command(context, script: str) -> list[str]:
    body = "$ErrorActionPreference='Stop';[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false);" + script
    encoded = base64.b64encode(body.encode("utf-16-le")).decode("ascii")
    return [powershell(context), "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded]


def service_command(context) -> list[str]:
    interpreter = Path(sys.executable)
    if context.windows:
        interpreter = interpreter.with_name("pythonw.exe")
        if not interpreter.is_file():
            raise ServiceError("pythonw.exe is required for the machine-event scheduled task")
    return [str(interpreter), *arguments(context)]


def windows_action(context) -> tuple[str, str]:
    command = service_command(context)
    return command[0], subprocess.list2cmdline(command[1:])


def windows_snapshot(context) -> dict:
    name = ps_literal(identity(context))
    script = ("$identity=[Security.Principal.WindowsIdentity]::GetCurrent();"
              "$sid=$identity.User.Value;$account=$identity.Name;"
              f"$task=Get-ScheduledTask -TaskPath '\\' -TaskName {name} -ErrorAction SilentlyContinue;"
              "$xml=$null;$state=$null;if($task){"
              f"$xml=Export-ScheduledTask -TaskPath '\\' -TaskName {name};$state=[string]$task.State" + "};"
              "@{sid=$sid;account=$account;xml=$xml;state=$state}|ConvertTo-Json -Compress")
    result = command(context, ps_command(context, script))
    try:
        snapshot = json.loads(result.stdout.lstrip("\ufeff"))
        if not isinstance(snapshot, dict) or not isinstance(snapshot.get("sid"), str) or not snapshot["sid"]:
            raise ValueError("missing current user SID")
        return snapshot
    except (TypeError, ValueError) as error:
        raise ServiceError("scheduled-task query returned invalid state") from error


def task_xml(context, sid: str, *, enabled: bool = True) -> str:
    ET.register_namespace("", NAMESPACE)
    root = ET.Element(f"{{{NAMESPACE}}}Task", {"version": "1.4"})

    def section(parent, name, values):
        node = ET.SubElement(parent, name)
        for key, value in values.items():
            ET.SubElement(node, key).text = value
        return node

    section(root, "RegistrationInfo", {"Description": marker(context)})
    section(ET.SubElement(root, "Triggers"), "LogonTrigger", {"Enabled": "true", "UserId": sid})
    principals = ET.SubElement(root, "Principals")
    principal = section(principals, "Principal", {"UserId": sid, "LogonType": "InteractiveToken",
                                                "RunLevel": "LeastPrivilege"})
    principal.set("id", "CurrentUser")
    settings = section(root, "Settings", {"MultipleInstancesPolicy": "IgnoreNew", "DisallowStartIfOnBatteries": "false",
                                         "StopIfGoingOnBatteries": "false", "StartWhenAvailable": "true",
                                         "Enabled": str(enabled).lower(), "Hidden": "true", "ExecutionTimeLimit": "PT0S"})
    section(settings, "RestartOnFailure", {"Interval": "PT1M", "Count": "999"})
    executable, argv = windows_action(context)
    actions = ET.SubElement(root, "Actions", {"Context": "CurrentUser"})
    section(actions, "Exec", {"Command": executable, "Arguments": argv,
                             "WorkingDirectory": str(context.state_directory.resolve())})
    return ET.tostring(root, encoding="unicode")


def xml_values(body: str) -> tuple[dict[str, str], tuple[int, int, int]]:
    try:
        root = ET.fromstring(body)
    except (ET.ParseError, TypeError) as error:
        raise ServiceError("scheduled-task XML is invalid") from error
    values = {}

    def visit(node, prefix=""):
        name = node.tag.rsplit("}", 1)[-1]
        path = prefix + "/" + name
        if not list(node):
            values[path] = node.text or ""
        for child in node:
            visit(child, path)

    visit(root)
    counts = tuple(len(root.findall(f"{{{NAMESPACE}}}{name}/*")) for name in ("Triggers", "Principals", "Actions"))
    return values, counts


def windows_drift(context, snapshot) -> list[str]:
    if not snapshot.get("xml"):
        return ["machine-event scheduled task is missing"]
    actual, counts = xml_values(snapshot["xml"])
    if actual.get("/Task/RegistrationInfo/Description") != marker(context):
        raise ServiceError(f"refusing to replace unowned scheduled task {identity(context)}")
    enabled = actual.get("/Task/Settings/Enabled", "true") not in ("false", "0")
    expected, expected_counts = xml_values(task_xml(context, snapshot["sid"], enabled=enabled))
    if not enabled:
        actual["/Task/Settings/Enabled"] = "false"
    defaults = {"/Task/Triggers/LogonTrigger/Enabled": "true",
                "/Task/Principals/Principal/RunLevel": "LeastPrivilege",
                "/Task/Settings/Enabled": "true"}
    for key, value in defaults.items():
        actual.setdefault(key, value)
    for key in ("/Task/Triggers/LogonTrigger/UserId", "/Task/Principals/Principal/UserId"):
        if snapshot.get("account") and actual.get(key) == snapshot["account"]:
            actual[key] = snapshot["sid"]
    drift = []
    if counts != expected_counts or any(actual.get(key) != value for key, value in expected.items()):
        drift.append("machine-event scheduled task command or settings differ")
    if enabled and snapshot.get("state") != "Running":
        drift.append("machine-event scheduled task is not running")
    return drift


def systemd_escape(value: str) -> str:
    if "\x00" in value or "\n" in value or "\r" in value:
        raise ServiceError("machine-event service path contains unsupported control characters")
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%").replace("$", "$$") + '"'


def unit_path(context) -> Path:
    root = Path(context.environ.get("XDG_CONFIG_HOME") or context.home / ".config")
    return root / "systemd/user" / (identity(context) + ".service")


def unit_contents(context) -> str:
    argv = " ".join(systemd_escape(value) for value in service_command(context))
    return (f"[Unit]\nDescription={marker(context)}\n\n[Service]\nType=simple\nExecStart={argv}\n"
            "Restart=on-failure\nRestartSec=10s\n\n[Install]\nWantedBy=default.target\n")


def systemctl(context) -> str:
    executable = context.which("systemctl")
    if not executable:
        raise ServiceError("systemctl --user is required for the machine-event subscriber")
    return executable


def linux_snapshot(context) -> dict[str, str]:
    result = command(context, [systemctl(context), "--user", "show", unit_path(context).name,
                              "--property=LoadState,ActiveState,UnitFileState,FragmentPath,DropInPaths,NeedDaemonReload"],
                     allowed=(0, 1, 4))
    snapshot = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
    if result.returncode and snapshot.get("LoadState") != "not-found":
        raise ServiceError("machine-event user supervisor is unavailable: " +
                           (result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"))
    if not snapshot.get("LoadState"):
        raise ServiceError("machine-event user supervisor returned invalid state")
    return snapshot


def linux_drift(context, snapshot) -> list[str]:
    path = unit_path(context)
    if path.is_symlink():
        raise ServiceError(f"refusing to replace linked service unit {path}")
    try:
        body = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        body = None
    if body is not None and f"Description={marker(context)}\n" not in body:
        raise ServiceError(f"refusing to replace unowned service unit {path}")
    fragment = snapshot.get("FragmentPath")
    if fragment and Path(fragment) != path:
        raise ServiceError(f"refusing to replace service supplied by {fragment}")
    if snapshot.get("DropInPaths"):
        raise ServiceError(f"machine-event service has unmanaged overrides: {snapshot['DropInPaths']}")
    drift = []
    if body != unit_contents(context):
        drift.append("machine-event user service definition differs")
    if snapshot.get("LoadState") != "loaded" or snapshot.get("NeedDaemonReload") == "yes":
        drift.append("machine-event user service needs daemon-reload")
    paused = body is not None and snapshot.get("UnitFileState") == "disabled"
    if not paused and snapshot.get("UnitFileState") != "enabled":
        drift.append("machine-event user service is not enabled")
    if not paused and snapshot.get("ActiveState") != "active":
        drift.append("machine-event user service is not running")
    return drift


def check(context) -> list[str]:
    if context.windows:
        return windows_drift(context, windows_snapshot(context))
    return linux_drift(context, linux_snapshot(context))


def apply(context) -> list[str]:
    script = Path(arguments(context)[0])
    if not script.is_file():
        raise ServiceError(f"installed machine-event runtime is missing: {script}")
    if context.windows:
        snapshot = windows_snapshot(context)
        pending = windows_drift(context, snapshot)
        if not pending:
            return []
        name = ps_literal(identity(context))
        xml = ps_literal(task_xml(context, snapshot["sid"]))
        owner = ps_literal(marker(context))
        values = xml_values(snapshot["xml"])[0] if snapshot.get("xml") else {}
        enabled = "$false" if values.get("/Task/Settings/Enabled") in ("false", "0") else "$true"
        body = (f"$existing=Get-ScheduledTask -TaskPath '\\' -TaskName {name} -ErrorAction SilentlyContinue;"
                f"if($existing -and $existing.Description -ne {owner}){{throw 'Refusing to replace unowned task'}};"
                f"$enabled={enabled};if($existing)" + "{$enabled=$enabled -and $existing.Settings.Enabled;"
                f"Stop-ScheduledTask -TaskPath '\\' -TaskName {name}" + "};"
                f"$definition=[xml]{xml};"
                "$definition.Task.Settings.Enabled=([string]$enabled).ToLowerInvariant();"
                f"Register-ScheduledTask -TaskPath '\\' -TaskName {name} -Xml $definition.OuterXml -Force|Out-Null;"
                f"if($enabled){{Start-ScheduledTask -TaskPath '\\' -TaskName {name}}}")
        command(context, ps_command(context, body))
    else:
        snapshot = linux_snapshot(context)
        path = unit_path(context)
        original = path.read_bytes() if path.exists() else None
        pending = linux_drift(context, snapshot)
        if not pending:
            return []
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".service.next")
        if temporary.exists() or temporary.is_symlink():
            raise ServiceError(f"service staging path already exists: {temporary}")
        with temporary.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(unit_contents(context))
        current = path.read_bytes() if path.exists() else None
        if path.is_symlink() or current != original:
            temporary.unlink()
            raise ServiceError(f"service unit changed during registration: {path}")
        os.replace(temporary, path)
        ctl = systemctl(context)
        command(context, [ctl, "--user", "daemon-reload"])
        paused = original is not None and (snapshot.get("UnitFileState") == "disabled" or
                                          linux_snapshot(context).get("UnitFileState") == "disabled")
        if not paused:
            command(context, [ctl, "--user", "enable", path.name])
            command(context, [ctl, "--user", "restart", path.name])
    remaining = check(context)
    for _ in range(10):
        if not remaining or any("not running" not in item for item in remaining):
            break
        time.sleep(0.2)
        remaining = check(context)
    if remaining:
        raise ServiceError("machine-event supervisor did not converge: " + "; ".join(remaining))
    return pending
