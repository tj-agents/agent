from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import close_lifecycle
import managed_admission as admission
import plugin_autoupdate
import required_plugins
import session_relaunch
from host import Host


class MaintenanceUnsettledError(admission.AdmissionError):
    pass


def state_path(project: Path, host: Host, session: str) -> Path:
    return admission.refresh_path(project, host, session)


def blocked_path(project: Path, host: Host, session: str) -> Path:
    return state_path(project, host, session).with_suffix(".blocked.json")


def mark_blocked(project: Path, host: Host, session: str) -> None:
    admission.mark_refresh_blocked(project, host, session)


def notice(event: str, message: str) -> dict[str, Any]:
    if event == "UserPromptSubmit":
        return admission.denial(event, message)
    return {}


def unchanged(project: Path, host: Host, session: str) -> bool:
    with admission.publication_lock(project, host):
        return renew_unchanged(project, host, session)


def renew_unchanged(project: Path, host: Host, session: str) -> bool:
    document = admission.cached(project, host)
    if not document["guards"]:
        return False
    records = []
    for guard in document["guards"]:
        path = admission.receipt_path(
            required_plugins.project_root(project), host, session + "@" + guard
        )
        try:
            record = admission.read(path)
        except (OSError, ValueError, admission.AdmissionError):
            return False
        previous = record.get("admission", {})
        if (
            record.get("session") != session
            or previous.get("freshness") != document["freshness"]
            or previous.get("guards") != document["guards"]
        ):
            return False
        root = Path(record["root"])
        proof = admission.signatures([root])
        try:
            if admission.executing_identity(document, root) != guard:
                return False
        except admission.AdmissionError:
            return False
        if admission.signatures([root]) != proof:
            raise admission.AdmissionError("Guard changed during admission renewal")
        records.append((path, {**record, "admission": document, "signature": proof}))
    for _, record in records:
        if admission.signatures([Path(record["root"])]) != record["signature"]:
            raise admission.AdmissionError("Guard changed before admission renewal")
    if admission.cached(project, host) != document:
        raise admission.AdmissionError("Managed admission changed during renewal")
    for path, record in records:
        plugin_autoupdate.write_json(path, record)
    admission.gate(project, host, session, "PreToolUse", Path(records[0][1]["root"]))
    return True


def stale_identity(project: Path, host: Host) -> str:
    try:
        document = admission.read(admission.receipt_path(project, host))
        observed = admission.signatures([Path(path) for path in document["watch"]])
        value = {
            "observed": observed,
            "freshness": document.get("freshness"),
            "checked": document["checked"],
            "expired": time.time() - document["checked"] >= admission.MAX_AGE,
        }
    except (OSError, ValueError, KeyError, TypeError, admission.AdmissionError):
        value = {"admission": "unavailable"}
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def maintain(project: Path, host: Host, path: Path) -> None:
    arguments = [
        sys.executable,
        "-B",
        str(Path(__file__).with_name("reload_plugins.py")),
        "--host",
        host.value,
        "--project",
        str(project),
        "--verify",
        "--no-relaunch",
        "--defer-adoption",
        "--maintenance-budget",
        "180",
    ]
    with path.with_suffix(".log").open("w", encoding="utf-8") as stream:
        result = subprocess.run(
            arguments, cwd=project, stdout=stream, stderr=subprocess.STDOUT, check=False
        )
    if result.returncode == 3:
        raise MaintenanceUnsettledError(
            "Plugin maintenance could not prove that its writer stopped; "
            "inspect " + str(path.with_suffix(".log")) + " before retrying"
        )
    if result.returncode:
        raise admission.AdmissionError(
            "Automatic plugin maintenance failed; see " + str(path.with_suffix(".log"))
        )


def run(project: Path, host: Host, session: str, event: str) -> dict[str, Any]:
    if event not in {"UserPromptSubmit", "Stop"}:
        raise admission.AdmissionError(
            "Automatic refresh requires a safe lifecycle hook"
        )
    if not required_plugins.selected(required_plugins.project_root(project)):
        return {}
    try:
        admission.gate(
            project, host, session, event, admission.package_root(Path(__file__))
        )
        state_path(project, host, session).unlink(missing_ok=True)
        return {}
    except (admission.AdmissionError, OSError, ValueError, KeyError, TypeError):
        pass
    path = state_path(project, host, session)
    lock = plugin_autoupdate.acquire_lock(path.with_suffix(".lock"))
    if lock is None:
        return notice(
            event, "Automatic plugin refresh is already running for this conversation."
        )
    state: dict[str, Any] = {"status": "pending", "at": time.time()}
    try:
        if close_lifecycle.has_pending_close(host, session):
            return {}
        native = close_lifecycle.current_input(host, session)
        identity = stale_identity(required_plugins.project_root(project), host)
        try:
            previous = admission.read(path)
        except (OSError, ValueError, admission.AdmissionError):
            previous = {}
        if previous.get("status") == "writer-unsettled":
            return notice(event, str(previous["reason"]))
        if previous.get("input_generation") == native["input_generation"] and (
            previous.get("status") != "renewed"
            or previous.get("stale_identity") == identity
        ):
            return notice(
                event,
                "Automatic plugin refresh is pending. Preserve this conversation; "
                "the same input will not start another refresh attempt.",
            )
        state.update(
            input_generation=native["input_generation"], stale_identity=identity
        )
        plugin_autoupdate.write_json(path, state)
        try:
            admission.cached(project, host)
        except (admission.AdmissionError, OSError, ValueError, KeyError, TypeError):
            maintain(project, host, path)
        close_lifecycle.require_input(host, session, native)
        if close_lifecycle.has_pending_close(host, session):
            state["status"] = "close-owned"
            plugin_autoupdate.write_json(path, state)
            return {}
        if unchanged(project, host, session):
            state["status"] = "renewed"
            state["stale_identity"] = stale_identity(
                required_plugins.project_root(project), host
            )
            plugin_autoupdate.write_json(path, state)
            blocked = blocked_path(project, host, session)
            resume = blocked.exists()
            blocked.unlink(missing_ok=True)
            if event == "Stop" and resume:
                return {
                    "decision": "block",
                    "reason": (
                        "Plugin admission was renewed automatically. Continue the "
                        "authorized work that the stale-skills guard paused."
                    ),
                }
            return {}
        close_lifecycle.require_input(host, session, native)
        outcome = session_relaunch.relaunch(
            project, {}, host=host, expected_session=session, expected_input=native
        )
        if outcome.get("relaunch") != "armed":
            raise admission.AdmissionError("Automatic plugin restart was not armed")
        state["status"] = "restart-armed"
        plugin_autoupdate.write_json(path, state)
        return notice(
            event,
            "Updated plugins are verified. A guarded restart is armed to resume "
            "this same conversation. End this turn so it can proceed; the restart "
            "already owns the close assessment.",
        )
    except (
        admission.AdmissionError,
        OSError,
        RuntimeError,
        ValueError,
        KeyError,
        TypeError,
        subprocess.SubprocessError,
    ) as error:
        state.update(
            status="writer-unsettled"
            if isinstance(error, MaintenanceUnsettledError)
            else "failed",
            reason=str(error),
        )
        plugin_autoupdate.write_json(path, state)
        return notice(
            event,
            "Automatic plugin refresh remains pending: "
            + str(error)
            + ". Keep the conversation open; no refresh restart was claimed.",
        )
    finally:
        lock.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", type=Host, choices=list(Host), required=True)
    args = parser.parse_args()
    event = "UserPromptSubmit"
    try:
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict):
            raise admission.AdmissionError(
                "Automatic refresh hook payload must be an object"
            )
        event = payload.get("hook_event_name", event)
        project = Path(payload.get("cwd") or Path.cwd())
        session = payload.get("session_id") or os.environ.get(
            args.host.session_variable, ""
        )
        if not isinstance(session, str) or not session:
            raise admission.AdmissionError(
                "Native session identity is unavailable for automatic refresh"
            )
        verdict = run(project, args.host, session, event)
    except (
        admission.AdmissionError,
        OSError,
        ValueError,
        KeyError,
        TypeError,
    ) as error:
        verdict = notice(
            event, "Automatic plugin refresh remains pending: " + str(error)
        )
    if verdict:
        print(json.dumps(verdict))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
