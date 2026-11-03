"""Bridge native lifecycle hooks to an explicitly armed terminal release."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import secrets
import sys
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any

PACKAGE_ROOT = Path(__file__).resolve().parents[3]
for runtime_path in (
    Path(__file__).resolve().parent,
    PACKAGE_ROOT / ".claude" / "runtime",
    PACKAGE_ROOT / ".codex" / "runtime",
):
    if str(runtime_path) not in sys.path:
        sys.path.insert(0, str(runtime_path))

if TYPE_CHECKING:
    from host import Host


RECEIPT_MAX_AGE_SECONDS = 300
ACKNOWLEDGEMENT_TIMEOUT_SECONDS = 5
STATE_LOCK_TIMEOUT_SECONDS = 0.5
INTENTS = {"task-completion", "user-dismissal", "refresh-restart"}


def state_root() -> Path:
    return (
        Path(os.environ.get("AGENT_STATE_DIRECTORY") or Path.home() / ".agents-state")
        / "terminal-release"
    )


def _payload() -> dict[str, Any]:
    try:
        value = json.loads(sys.stdin.read() or "{}")
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _identity(data: dict[str, Any]) -> tuple[str | None, str | None]:
    return _text(data.get("session_id") or data.get("sessionId")), _text(
        data.get("turn_id") or data.get("turnId")
    )


def _safe_name(value: str) -> str:
    if any(part in value for part in ("/", "\\", "..")):
        raise ValueError("invalid session id")
    return value


def _state_path(session_id: str) -> Path:
    return state_root() / (_safe_name(session_id) + ".json")


def _input_path(session_id: str) -> Path:
    return state_root() / "inputs" / (_safe_name(session_id) + ".json")


def _policy_path(session_id: str) -> Path:
    return state_root() / "policies" / (_safe_name(session_id) + ".json")


@contextmanager
def _session_lock(session_id: str) -> Iterator[None]:
    from plugin_autoupdate import acquire_lock

    path = state_root() / "locks" / (_safe_name(session_id) + ".lock")
    deadline = time.monotonic() + STATE_LOCK_TIMEOUT_SECONDS
    while (handle := acquire_lock(path)) is None:
        if time.monotonic() >= deadline:
            raise RuntimeError("terminal release state lock is unavailable")
        time.sleep(0.01)
    try:
        yield
    finally:
        handle.close()


def _revision_path(session_id: str) -> Path:
    return state_root() / "mutations" / (_safe_name(session_id) + ".json")


def _revision(session_id: str) -> int:
    value = (_read(_revision_path(session_id)) or {}).get("revision", 0)
    if not isinstance(value, int) or value < 0:
        raise ValueError("terminal release mutation identity is invalid")
    return value


def _advance(session_id: str) -> None:
    _write(_revision_path(session_id), {"revision": _revision(session_id) + 1})


def _input_record(session_id: str) -> dict[str, Any] | None:
    path = _input_path(session_id)
    recorded = _read(path)
    if recorded is None and path.exists():
        raise ValueError("native input hook receipt is invalid")
    return recorded


def _require_recorded_input(session_id: str, recorded: dict[str, Any] | None) -> None:
    if _input_record(session_id) != recorded:
        raise ValueError("native input changed during the terminal release")


def _require_snapshot(
    session_id: str, revision: int, recorded: dict[str, Any] | None
) -> None:
    _require_recorded_input(session_id, recorded)
    if _revision(session_id) != revision:
        raise ValueError("terminal release generation changed during assessment")


def _read(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _write(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=path.name + ".",
            delete=False,
        ) as stream:
            temporary = Path(stream.name)
            stream.write(json.dumps(value, sort_keys=True) + "\n")
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _signal(path: str, value: dict[str, Any]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        with target.open("x", encoding="utf-8") as stream:
            json.dump(value, stream, sort_keys=True)
            stream.write("\n")
    except FileExistsError:
        return


def _armed(state: dict[str, Any]) -> bool:
    return (
        state.get("status") == "armed"
        and isinstance(state.get("completion"), str)
        and isinstance(state.get("cancel"), str)
        and isinstance(state.get("completion_token"), str)
    )


def _owned(state: dict[str, Any], host: Host, session_id: str) -> bool:
    return state.get("host") == host.value and state.get("session_id") == session_id


def remember_input(
    host: Host, session_id: str, turn_id: str | None, data: dict[str, Any]
) -> None:
    supplied = _text(data.get("transcript_path") or data.get("transcriptPath"))
    _write(
        _input_path(session_id),
        {
            "host": host.value,
            "session_id": session_id,
            "turn_id": turn_id,
            "profile": str(host.configuration_root.resolve()),
            "transcript_path": supplied,
            "input_generation": secrets.token_urlsafe(24),
        },
    )


def current_input(
    host: Host, session_id: str, supplied: Path | None = None
) -> dict[str, Any]:
    from host_sessions import SESSIONS

    state = _read(_input_path(session_id))
    if (
        not state
        or state.get("host") != host.value
        or state.get("session_id") != session_id
        or not isinstance(state.get("input_generation"), str)
        or state.get("profile") != str(host.configuration_root.resolve())
    ):
        raise ValueError(
            "native input hook receipt is unavailable; preserve the session"
        )
    recorded = state.get("transcript_path")
    if (
        supplied is not None
        and recorded
        and supplied.resolve() != Path(recorded).resolve()
    ):
        raise ValueError("transcript differs from the native input hook receipt")
    native = SESSIONS[host].close_target(
        session_id, supplied or (Path(recorded) if recorded else None)
    )
    if host.value == "codex" and not _text(state.get("turn_id")):
        raise ValueError("native input hook receipt has no Codex turn")
    return {**state, **native}


def require_input(host: Host, session_id: str, state: dict[str, Any]) -> None:
    current = current_input(host, session_id)
    if any(state.get(key) != value for key, value in current.items()):
        raise ValueError("native input changed during the terminal release")


def _require_native(
    state: dict[str, Any], shared: Any, supplied: Path | None = None
) -> None:
    from host import Host
    from host_sessions import SESSIONS

    host = Host(state["host"])
    if state["intent"] != "refresh-restart":
        require_input(host, state["session_id"], state["native"])
        if supplied is not None:
            current_input(host, state["session_id"], supplied)
        return
    if not shared.registration_matches(
        shared.read_json(state["registry"]), state["entry"]
    ):
        raise ValueError("session registration changed before terminal release")
    bound = state["native"].get("transcript")
    native = SESSIONS[host].restart_input(
        state["entry"], supplied or (Path(bound) if bound else None), shared
    )
    if any(state["native"].get(key) != value for key, value in native.items()):
        raise ValueError("native input changed during the terminal release")


def _source_digest(sources: list[dict[str, str]]) -> str:
    raw = json.dumps(sources, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _aggregate_has_blockers(aggregate: dict[str, Any]) -> bool:
    for key in ("owned", "responsibilities", "children", "waits"):
        entries = aggregate.get(key)
        if not isinstance(entries, list):
            return True
        if any(
            not isinstance(entry, dict)
            or (entry.get("complete") is not True and entry.get("resolved") is not True)
            for entry in entries
        ):
            return True
    return False


def _receipt_bytes(path: Path) -> tuple[dict[str, Any], str]:
    raw = path.read_bytes()
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("terminal acceptance receipt must be an object")
    return value, hashlib.sha256(raw).hexdigest()


def _checkpoint_sources(
    value: object, checkpoint: object
) -> list[dict[str, str]] | None:
    if checkpoint is None:
        return None
    if not isinstance(checkpoint, dict) or set(checkpoint) != {
        "sources",
        "retired_sources",
    }:
        raise ValueError("terminal acceptance checkpoint does not match its sources")
    sources = checkpoint.get("sources")
    if (
        not isinstance(value, list)
        or not isinstance(sources, list)
        or len(sources) != len(value)
    ):
        raise ValueError("terminal acceptance checkpoint does not match its sources")
    retired = checkpoint.get("retired_sources")
    eligible = {
        source.get("path")
        for index, source in enumerate(value)
        if index != 1
        and isinstance(source, dict)
        and isinstance(source.get("path"), str)
    }
    if (
        not isinstance(retired, list)
        or not all(isinstance(path, str) and path in eligible for path in retired)
        or len(retired) != len(set(retired))
    ):
        raise ValueError("terminal acceptance checkpoint has invalid retired sources")
    for source, archived in zip(value, sources, strict=True):
        if (
            not isinstance(source, dict)
            or not isinstance(archived, dict)
            or source.get("sha256") != archived.get("sha256")
            or not isinstance(archived.get("path"), str)
            or not Path(archived["path"]).is_absolute()
        ):
            raise ValueError(
                "terminal acceptance checkpoint does not match its sources"
            )
    return sources


def _validate_sources(
    value: object, checkpoint: object
) -> tuple[list[dict[str, str]], str]:
    if not isinstance(value, list) or not value:
        raise ValueError("terminal acceptance receipt has no sources")
    archived = _checkpoint_sources(value, checkpoint)
    sources: list[dict[str, str]] = []
    for index, source in enumerate(value):
        if (
            not isinstance(source, dict)
            or not isinstance(source.get("path"), str)
            or not isinstance(source.get("sha256"), str)
            or len(source["sha256"]) != 64
        ):
            raise ValueError("terminal acceptance receipt has an invalid source")
        path = Path(source["path"])
        if not path.is_absolute():
            raise ValueError("terminal acceptance receipt source is unavailable")
        retired = (
            isinstance(checkpoint, dict) and str(path) in checkpoint["retired_sources"]
        )
        if retired and archived is not None:
            archived_path = Path(archived[index]["path"])
            if not archived_path.is_absolute() or not archived_path.is_file():
                raise ValueError("terminal acceptance checkpoint source is unavailable")
            digest = hashlib.sha256(archived_path.read_bytes()).hexdigest()
            if digest != source["sha256"]:
                raise ValueError("terminal acceptance checkpoint source changed")
            if not path.exists():
                sources.append({"path": str(path.resolve()), "sha256": digest})
                continue
        if not path.is_file():
            raise ValueError("terminal acceptance receipt source is unavailable")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest != source["sha256"]:
            raise ValueError("terminal acceptance receipt source changed")
        sources.append({"path": str(path.resolve()), "sha256": digest})
    return sources, _source_digest(sources)


def _validate_cleanup(
    value: object, host: Host, session_id: str, shared: Any | None, require_worker: bool
) -> dict[str, Any]:
    if not isinstance(value, dict) or not isinstance(value.get("state"), str):
        raise ValueError("terminal acceptance receipt has no cleanup disposition")
    state = value["state"]
    if state in {"unnecessary", "retained", "verified"}:
        return {"state": state}
    if state != "pending":
        raise ValueError("terminal cleanup is not releasable")
    owner = value.get("owner")
    fields = ("obligation_id", "obligation_path", "receipt_path", "worktree")
    if (
        not isinstance(owner, dict)
        or owner.get("host") != host.value
        or owner.get("session_id") != session_id
        or any(not isinstance(value.get(field), str) for field in fields)
    ):
        raise ValueError("terminal cleanup obligation is invalid")
    obligation_path = Path(value["obligation_path"])
    receipt_path = Path(value["receipt_path"])
    worktree = Path(value["worktree"])
    if (
        not obligation_path.is_absolute()
        or not receipt_path.is_absolute()
        or not worktree.is_absolute()
    ):
        raise ValueError("terminal cleanup obligation has a relative path")
    obligation = _read(obligation_path)
    if (
        not obligation
        or obligation.get("schema_version") != 1
        or obligation.get("kind") != "targeted-cleanup"
        or obligation.get("state")
        not in {"pending", "running", "verified", "retained", "refused", "timed_out"}
        or obligation.get("obligation_id") != value["obligation_id"]
        or obligation.get("owner") != owner
        or obligation.get("worktree") != str(worktree.resolve())
        or not isinstance(obligation.get("identity_sha256"), str)
        or len(obligation["identity_sha256"]) != 64
        or not _text(obligation.get("launch_token"))
    ):
        raise ValueError("terminal cleanup obligation changed")
    if not require_worker:
        return {
            "state": state,
            "obligation_id": value["obligation_id"],
            "obligation_path": str(obligation_path.resolve()),
            "receipt_path": str(receipt_path.resolve()),
            "owner": owner,
            "worktree": str(worktree.resolve()),
        }
    mutable = _read(receipt_path)
    if not mutable:
        raise ValueError("terminal cleanup worker receipt is unavailable")
    if (
        mutable.get("obligation_id") != value["obligation_id"]
        or mutable.get("owner") != owner
        or mutable.get("worktree") != str(worktree.resolve())
        or mutable.get("identity_sha256") != obligation["identity_sha256"]
        or mutable.get("launch_token") != obligation["launch_token"]
    ):
        raise ValueError("terminal cleanup worker receipt changed")
    if mutable.get("state") == "verified":
        if worktree.exists():
            raise ValueError("terminal cleanup target remains present")
        return {
            "state": state,
            "obligation_id": value["obligation_id"],
            "obligation_path": str(obligation_path.resolve()),
            "receipt_path": str(receipt_path.resolve()),
            "owner": owner,
            "worktree": str(worktree.resolve()),
        }
    if mutable.get("state") == "retained":
        if not _text(mutable.get("reason")):
            raise ValueError("terminal cleanup retention has no disposition")
        return {
            "state": state,
            "obligation_id": value["obligation_id"],
            "obligation_path": str(obligation_path.resolve()),
            "receipt_path": str(receipt_path.resolve()),
            "owner": owner,
            "worktree": str(worktree.resolve()),
        }
    if mutable.get("state") != "running":
        raise ValueError("terminal cleanup worker is not active")
    launch = mutable.get("launch")
    launcher = launch.get("launcher") if isinstance(launch, dict) else None
    if (
        not isinstance(launch, dict)
        or launch.get("token") != mutable["launch_token"]
        or not isinstance(launcher, dict)
        or not isinstance(launcher.get("pid"), int)
        or not isinstance(launcher.get("started_at"), (int, float))
    ):
        raise ValueError("terminal cleanup launch is not acknowledged")
    worker = mutable.get("worker")
    if (
        shared is None
        or not isinstance(worker, dict)
        or not shared.verified_live(worker.get("pid"), worker.get("started_at"))
    ):
        raise ValueError("terminal cleanup worker is not live")
    return {
        "state": state,
        "obligation_id": value["obligation_id"],
        "obligation_path": str(obligation_path.resolve()),
        "receipt_path": str(receipt_path.resolve()),
        "owner": owner,
        "worktree": str(worktree.resolve()),
    }


def _validate_receipt(
    path: Path,
    host: Host,
    session_id: str,
    intent: str,
    expected_sha256: str | None = None,
    shared: Any | None = None,
    require_cleanup_worker: bool = True,
) -> tuple[dict[str, Any], dict[str, Any]]:
    if not path.is_absolute():
        raise ValueError("terminal acceptance receipt path must be absolute")
    receipt, digest = _receipt_bytes(path)
    if expected_sha256 is not None and digest != expected_sha256:
        raise ValueError("terminal acceptance receipt changed")
    if (
        receipt.get("schema_version") != 1
        or receipt.get("kind") != "terminal-acceptance"
    ):
        raise ValueError("invalid terminal acceptance receipt")
    owner = receipt.get("owner")
    if (
        not isinstance(owner, dict)
        or owner.get("host") != host.value
        or owner.get("session_id") != session_id
    ):
        raise ValueError("terminal acceptance receipt belongs to another session")
    if not isinstance(receipt.get("candidate_id"), str) or not isinstance(
        receipt.get("acceptance_generation"), str
    ):
        raise ValueError("terminal acceptance receipt has no candidate identity")
    checked_at = receipt.get("checked_at")
    if (
        not isinstance(checked_at, (int, float))
        or not 0 <= time.time() - checked_at <= RECEIPT_MAX_AGE_SECONDS
    ):
        raise ValueError("terminal acceptance receipt is stale")
    sources, sources_sha256 = _validate_sources(
        receipt.get("sources"), receipt.get("checkpoint")
    )
    candidate_id = hashlib.sha256(
        json.dumps(
            {
                "owner": owner,
                "sources": sources,
                "bound_delivery": receipt.get("bound_delivery"),
            },
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
    ).hexdigest()
    if (
        receipt["candidate_id"] != candidate_id
        or receipt["acceptance_generation"] != candidate_id
    ):
        raise ValueError("terminal acceptance receipt candidate binding changed")
    if intent == "task-completion":
        if receipt.get("state") != "accepted":
            raise ValueError("terminal acceptance is not accepted")
        deterministic = receipt.get("deterministic")
        if not isinstance(deterministic, dict) or deterministic.get("blockers") != []:
            raise ValueError("terminal acceptance has deterministic blockers")
        aggregate = receipt.get("aggregate")
        if not isinstance(aggregate, dict) or aggregate.get("known") is not True:
            raise ValueError("terminal acceptance responsibilities are unknown")
        audit = receipt.get("audit")
        if not isinstance(audit, dict):
            raise ValueError("terminal acceptance receipt has no audit")
        if (
            audit.get("candidate_id") != receipt["candidate_id"]
            or audit.get("sources_sha256") != sources_sha256
        ):
            raise ValueError("terminal acceptance audit does not match its candidate")
        if audit.get("state") != "passed":
            raise ValueError("terminal acceptance audit is incomplete")
        if audit.get("state") == "passed":
            result = audit.get("result")
            if (
                not _text(audit.get("native_child_id"))
                or not _text(audit.get("reservation_id"))
                or not isinstance(result, dict)
                or not isinstance(result.get("path"), str)
                or not isinstance(result.get("sha256"), str)
                or len(result["sha256"]) != 64
            ):
                raise ValueError("terminal acceptance audit proof is invalid")
            result_path = Path(result["path"])
            if not result_path.is_absolute() or not result_path.is_file():
                raise ValueError("terminal acceptance audit result is unavailable")
            if hashlib.sha256(result_path.read_bytes()).hexdigest() != result["sha256"]:
                raise ValueError("terminal acceptance audit result changed")
        if _aggregate_has_blockers(aggregate):
            raise ValueError("terminal acceptance has unresolved responsibilities")
        cleanup = _validate_cleanup(
            receipt.get("cleanup"), host, session_id, shared, require_cleanup_worker
        )
    elif intent == "user-dismissal":
        if receipt.get("state") not in {"candidate", "accepted"}:
            raise ValueError("terminal dismissal has no preserved checkpoint")
        cleanup = None
    else:
        raise ValueError("terminal acceptance receipt is not valid for this intent")
    frozen = {
        "path": str(path.resolve()),
        "sha256": digest,
        "candidate_id": receipt["candidate_id"],
        "acceptance_generation": receipt["acceptance_generation"],
        "sources_sha256": sources_sha256,
        "cleanup": cleanup,
    }
    return receipt, frozen


def _validate_frozen_receipt(state: dict[str, Any], shared: Any | None = None) -> None:
    from host import Host

    intent = state.get("intent")
    frozen = state.get("receipt")
    if intent == "refresh-restart":
        if frozen is not None:
            raise ValueError(
                "refresh restart cannot carry a terminal acceptance receipt"
            )
        return
    if (
        not isinstance(frozen, dict)
        or not isinstance(frozen.get("path"), str)
        or not isinstance(frozen.get("sha256"), str)
    ):
        raise ValueError("terminal release has no frozen receipt")
    if not isinstance(intent, str):
        raise ValueError("terminal release has an invalid intent")
    frozen_path = frozen["path"]
    frozen_sha256 = frozen["sha256"]
    receipt, current = _validate_receipt(
        Path(frozen_path),
        Host(state["host"]),
        state["session_id"],
        intent,
        frozen_sha256,
        shared=shared,
    )
    if any(frozen.get(key) != current.get(key) for key in current):
        raise ValueError("terminal acceptance receipt identity changed")
    if receipt["candidate_id"] != frozen.get("candidate_id"):
        raise ValueError("terminal acceptance receipt candidate changed")


def _load_shared() -> Any:
    path = (
        Path(__file__).resolve().parents[1]
        / "utility"
        / "peer"
        / "scripts"
        / "session_close.py"
    )
    if str(path.parent) not in sys.path:
        sys.path.insert(0, str(path.parent))
    spec = importlib.util.spec_from_file_location(
        "terminal_release_session_close", path
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("the packaged Agent peer runtime is unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _prepared_entry(
    host: Host,
    session_id: str,
    profile: Path,
    native: dict[str, Any] | None,
    shared: Any,
    supplied: Path | None = None,
    expected: dict[str, Any] | None = None,
) -> tuple[Path, dict[str, Any], dict[str, Any]]:
    registry = state_root().parent / "cli-sessions" / (_safe_name(session_id) + ".json")
    sessions = shared.host_sessions(host.value)
    record = shared.read_json(registry) or {}
    if expected is None:
        entry = sessions.prepared_record(record, session_id, shared)
    else:
        if not shared.registration_matches(record, expected):
            raise ValueError("session registration changed before terminal release")
        entry = expected
        sessions.verify_frontend(entry, shared)
    if (
        entry.get("host") != host.value
        or entry.get("session_id") != session_id
        or shared.path_key(entry.get("profile", "")) != shared.path_key(profile)
    ):
        raise ValueError(
            "native frontend does not match the requested session and profile"
        )
    if native is None:
        native = sessions.restart_input(entry, supplied, shared)
    armed = sessions.arm_close(entry, native.get("turn_id"), native, supplied, shared)
    if not shared.registration_matches(shared.read_json(registry), entry):
        raise ValueError("session registration changed before terminal release")
    return registry, entry, {**native, **armed}


def policy(
    host: Host, session_id: str, profile: Path, action: str, reason: str
) -> dict[str, Any]:
    if action not in {"keep-open", "revoke"}:
        raise ValueError("unknown terminal release policy action")
    if not reason.strip():
        raise ValueError("terminal release policy needs a nonempty reason")
    if profile.resolve() != host.configuration_root.resolve():
        raise ValueError("terminal release policy belongs to a different profile")
    with _session_lock(session_id):
        if action == "keep-open":
            _write(
                _policy_path(session_id),
                {
                    "host": host.value,
                    "session_id": session_id,
                    "profile": str(profile.resolve()),
                    "reason": reason,
                    "at": time.time(),
                },
            )
            _cancel_locked(host, session_id, "persistent keep-open policy")
            return {"status": "keep-open", "session_id": session_id}
        _policy_path(session_id).unlink(missing_ok=True)
        _advance(session_id)
    return {"status": "revoked", "session_id": session_id}


def _policy_blocks(host: Host, session_id: str, profile: Path) -> bool:
    value = _read(_policy_path(session_id))
    return bool(
        value
        and value.get("host") == host.value
        and value.get("session_id") == session_id
        and value.get("profile") == str(profile.resolve())
    )


def _cancel_locked(host: Host, session_id: str, reason: str) -> None:
    _advance(session_id)
    state = _read(_state_path(session_id))
    if (
        state
        and _owned(state, host, session_id)
        and state.get("status") in {"prepared", "armed"}
    ):
        if isinstance(state.get("cancel"), str):
            _signal(state["cancel"], {"cancelled": True, "reason": reason})
        _write(
            _state_path(session_id),
            {
                **state,
                "status": "cancelled",
                "cancelled_at": time.time(),
                "cancel_reason": reason,
            },
        )


def cancel(host: Host, session_id: str, reason: str) -> None:
    with _session_lock(session_id):
        _cancel_locked(host, session_id, reason)


def retire_release(state: dict[str, Any]) -> None:
    session_id = state["session_id"]
    with _session_lock(session_id):
        path = _state_path(session_id)
        if _read(path) == state:
            _advance(session_id)
            path.unlink(missing_ok=True)


def prepare(
    host: Host,
    session_id: str,
    profile: Path,
    intent: str,
    shared: Any,
    receipt_path: Path | None = None,
    supplied: Path | None = None,
    dismissal_reason: str | None = None,
    bound_entry: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if intent not in INTENTS:
        raise ValueError("unknown terminal release intent")
    if profile.resolve() != host.configuration_root.resolve():
        raise ValueError("terminal release belongs to a different profile")
    if _policy_blocks(host, session_id, profile):
        raise ValueError("persistent keep-open policy is active")
    if intent == "user-dismissal" and not _text(dismissal_reason):
        raise ValueError("terminal dismissal needs explicit user authority")
    with _session_lock(session_id):
        revision = _revision(session_id)
        input_hook = _input_record(session_id)
    native = (
        None
        if intent == "refresh-restart"
        else current_input(host, session_id, supplied)
    )
    if intent == "refresh-restart":
        if receipt_path is not None:
            raise ValueError(
                "refresh restart does not use a terminal acceptance receipt"
            )
        frozen = None
    else:
        if receipt_path is None:
            raise ValueError("terminal release needs a terminal acceptance receipt")
        _receipt, frozen = _validate_receipt(
            receipt_path,
            host,
            session_id,
            intent,
            shared=shared,
            require_cleanup_worker=False,
        )
    registry, entry, native = _prepared_entry(
        host, session_id, profile, native, shared, supplied, bound_entry
    )
    if _policy_blocks(host, session_id, profile):
        raise ValueError("persistent keep-open policy is active")
    state = {
        "version": 1,
        "status": "prepared",
        "host": host.value,
        "session_id": session_id,
        "profile": str(profile.resolve()),
        "intent": intent,
        "release_generation": secrets.token_urlsafe(24),
        "prepared_at": time.time(),
        **native,
        "native": native,
        "input_hook": input_hook,
        "entry": entry,
        "registry": str(registry.resolve()),
        "receipt": frozen,
    }
    state["cancel"] = str(
        state_root() / "generations" / (state["release_generation"] + ".cancel.json")
    )
    if dismissal_reason is not None:
        state["dismissal_reason"] = dismissal_reason
    _require_native(state, shared, supplied)
    with _session_lock(session_id):
        if _policy_blocks(host, session_id, profile):
            raise ValueError("persistent keep-open policy is active")
        _require_snapshot(session_id, revision, input_hook)
        _cancel_locked(host, session_id, "terminal release superseded")
        _write(_state_path(session_id), state)
    return {
        "status": "prepared",
        "session_id": session_id,
        "release_generation": state["release_generation"],
        "acceptance_sha256": frozen["sha256"] if frozen else None,
    }


def _prepared(
    host: Host, session_id: str, profile: Path, release_generation: str
) -> dict[str, Any]:
    state = _read(_state_path(session_id))
    if (
        not state
        or not _owned(state, host, session_id)
        or state.get("status") != "prepared"
    ):
        raise ValueError("terminal release is not prepared")
    if (
        state.get("profile") != str(profile.resolve())
        or state.get("release_generation") != release_generation
    ):
        raise ValueError("terminal release generation changed")
    return state


def recheck(
    host: Host,
    session_id: str,
    profile: Path,
    release_generation: str,
    shared: Any,
    supplied: Path | None = None,
) -> dict[str, Any]:
    with _session_lock(session_id):
        state = _prepared(host, session_id, profile, release_generation)
        revision = _revision(session_id)
    if _policy_blocks(host, session_id, profile):
        raise ValueError("persistent keep-open policy is active")
    _validate_frozen_receipt(state, shared)
    _require_native(state, shared, supplied)
    registry, entry, native = _prepared_entry(
        host, session_id, profile, state["native"], shared, supplied, state["entry"]
    )
    _require_native(state, shared, supplied)
    updated = {
        **state,
        **native,
        "native": native,
        "entry": entry,
        "registry": str(registry.resolve()),
        "rechecked_at": time.time(),
    }
    with _session_lock(session_id):
        if _policy_blocks(host, session_id, profile):
            raise ValueError("persistent keep-open policy is active")
        _require_snapshot(session_id, revision, state["input_hook"])
        if _prepared(host, session_id, profile, release_generation) != state:
            raise ValueError("terminal release generation changed during assessment")
        _advance(session_id)
        _write(_state_path(session_id), updated)
    return {
        "status": "rechecked",
        "session_id": session_id,
        "release_generation": release_generation,
    }


def _acknowledged(request: dict[str, Any], digest: str, shared: Any) -> bool:
    acknowledgement = _read(Path(request["acknowledgement"]))
    if not acknowledgement or acknowledgement.get("request_sha256") != digest:
        return False
    return bool(
        shared.verified_live(
            acknowledgement.get("pid"), acknowledgement.get("started_at")
        )
    )


def arm(
    host: Host,
    session_id: str,
    profile: Path,
    release_generation: str,
    shared: Any,
    supplied: Path | None = None,
) -> dict[str, Any]:
    recheck(host, session_id, profile, release_generation, shared, supplied)
    with _session_lock(session_id):
        state = _prepared(host, session_id, profile, release_generation)
        revision = _revision(session_id)
    root = state_root()
    token = secrets.token_urlsafe(24)
    request = root / "requests" / (token + ".json")
    result_path = root / "requests" / (token + ".result.json")
    completion = root / "requests" / (token + ".complete.json")
    acknowledgement = root / "requests" / (token + ".ack.json")
    armed = {
        **state,
        "status": "armed",
        "armed_at": time.time(),
        "completion_token": token,
        "completion": str(completion),
        "result": str(result_path),
        "request": str(request),
        "acknowledgement": str(acknowledgement),
    }
    raw = json.dumps(armed, sort_keys=True).encode("utf-8") + b"\n"
    digest = hashlib.sha256(raw).hexdigest()
    _require_native(state, shared, supplied)
    with _session_lock(session_id):
        if _policy_blocks(host, session_id, profile):
            raise ValueError("persistent keep-open policy is active")
        _require_snapshot(session_id, revision, state["input_hook"])
        if _prepared(host, session_id, profile, release_generation) != state:
            raise ValueError("terminal release generation changed during assessment")
        if Path(armed["cancel"]).exists():
            raise ValueError("terminal release generation was cancelled")
        request.parent.mkdir(parents=True, exist_ok=True)
        with request.open("xb") as stream:
            stream.write(raw)
        _advance(session_id)
        _write(_state_path(session_id), armed)
    try:
        shared.start_agent_watcher(
            Path(shared.__file__).resolve().with_name("client_close_watcher.py"),
            ["--request", str(request), "--request-sha256", digest],
        )
        deadline = time.monotonic() + ACKNOWLEDGEMENT_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            require_release(armed, verify_receipt=False)
            if _acknowledged(armed, digest, shared):
                require_release(armed, verify_receipt=False)
                response = {
                    key: armed[key]
                    for key in (
                        "status",
                        "session_id",
                        "release_generation",
                        "request",
                        "completion",
                        "cancel",
                        "completion_token",
                        "result",
                    )
                }
                return {**response, "request_sha256": digest}
            time.sleep(0.05)
        raise RuntimeError("terminal release watcher did not acknowledge")
    except Exception:
        _signal(
            armed["cancel"],
            {"cancelled": True, "reason": "terminal release watcher was unavailable"},
        )
        with _session_lock(session_id):
            if _read(_state_path(session_id)) == armed:
                _cancel_locked(
                    host, session_id, "terminal release watcher was unavailable"
                )
        raise


def dismiss(
    host: Host,
    session_id: str,
    profile: Path,
    receipt_path: Path,
    reason: str,
    shared: Any,
) -> dict[str, Any]:
    prepared = prepare(
        host,
        session_id,
        profile,
        "user-dismissal",
        shared,
        receipt_path,
        dismissal_reason=reason,
    )
    return arm(host, session_id, profile, prepared["release_generation"], shared)


def require_release(state: dict[str, Any], *, verify_receipt: bool = True) -> None:
    from host import Host

    host = Host(state["host"])
    if not _armed(state):
        raise ValueError("terminal release is not armed")
    session_id = state["session_id"]
    shared = (
        _load_shared()
        if verify_receipt or state["intent"] == "refresh-restart"
        else None
    )
    if verify_receipt:
        _validate_frozen_receipt(state, shared)
    _require_native(state, shared)
    with _session_lock(session_id):
        if _policy_blocks(host, session_id, host.configuration_root):
            raise ValueError("persistent keep-open policy is active")
        if _read(_state_path(session_id)) != state or Path(state["cancel"]).exists():
            raise ValueError("terminal release generation was cancelled or changed")
        _require_recorded_input(session_id, state["input_hook"])


def run(event: str, host: Host, data: dict[str, Any]) -> int:
    from host_sessions import SESSIONS

    session_id, turn_id = _identity(data)
    if not session_id:
        return 0
    if event == "prompt":
        with _session_lock(session_id):
            _cancel_locked(host, session_id, "new prompt")
            remember_input(host, session_id, turn_id, data)
        return 0
    if event == "stop":
        state = _read(_state_path(session_id)) or {}
        if (
            _owned(state, host, session_id)
            and _armed(state)
            and SESSIONS[host].completes(state.get("native", {}), data, turn_id)
        ):
            _signal(state["completion"], {"token": state["completion_token"]})
        return 0
    if event == "end":
        with _session_lock(session_id):
            _cancel_locked(host, session_id, "session ended")
            _input_path(session_id).unlink(missing_ok=True)
            _policy_path(session_id).unlink(missing_ok=True)
        return 0
    raise ValueError("unknown lifecycle event")


def _command(args: argparse.Namespace) -> dict[str, Any]:
    from host import Host

    host = Host(args.host)
    profile = Path(args.profile).resolve()
    shared = _load_shared()
    if args.command == "policy":
        return policy(host, args.session_id, profile, args.action, args.reason)
    if args.command == "prepare":
        return prepare(
            host,
            args.session_id,
            profile,
            args.intent,
            shared,
            args.receipt,
            dismissal_reason=args.reason,
        )
    if args.command == "recheck":
        return recheck(host, args.session_id, profile, args.release_generation, shared)
    if args.command == "arm":
        return arm(host, args.session_id, profile, args.release_generation, shared)
    if args.command == "dismiss":
        return dismiss(
            host, args.session_id, profile, args.receipt, args.reason, shared
        )
    raise ValueError("unknown terminal release command")


def main(argv: list[str] | None = None) -> int:
    try:
        from host import Host

        parser = argparse.ArgumentParser(description=__doc__)
        commands = parser.add_subparsers(dest="command", required=True)
        for event in ("prompt", "stop", "end"):
            command = commands.add_parser(event)
            command.add_argument("--host", type=Host, choices=list(Host), required=True)
        for name in ("prepare", "recheck", "arm", "dismiss"):
            command = commands.add_parser(name)
            command.add_argument("--host", type=Host, choices=list(Host), required=True)
            command.add_argument("--session-id", required=True)
            command.add_argument("--profile", required=True)
            if name in {"prepare", "dismiss"}:
                command.add_argument("--receipt", type=Path)
            if name == "prepare":
                command.add_argument("--intent", choices=sorted(INTENTS), required=True)
                command.add_argument("--reason")
            if name in {"recheck", "arm"}:
                command.add_argument("--release-generation", required=True)
            if name == "dismiss":
                command.add_argument("--reason", required=True)
        command = commands.add_parser("policy")
        command.add_argument("--host", type=Host, choices=list(Host), required=True)
        command.add_argument("--session-id", required=True)
        command.add_argument("--profile", required=True)
        command.add_argument("--action", choices=("keep-open", "revoke"), required=True)
        command.add_argument("--reason", required=True)
        args = parser.parse_args(argv)
        if args.command in {"prompt", "stop", "end"}:
            return run(args.command, args.host, _payload())
        print(json.dumps(_command(args), sort_keys=True))
        return 0
    except (OSError, RuntimeError, ValueError) as error:
        print(f"close_lifecycle: {type(error).__name__}: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
