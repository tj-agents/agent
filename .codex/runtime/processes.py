"""Native process and transport evidence for managed Codex frontends and engines."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from uuid import UUID

import jobs
from launch_contract import Liveness
from launch_errors import LaunchError
from tj_agent.host import Host
from tj_cli import process


@dataclass(frozen=True, slots=True)
class Identity:
    pid: int
    started_at: str

    @classmethod
    def parse(cls, value: object) -> Identity:
        if not isinstance(value, dict):
            raise ValueError("Native process identity must be an object")
        pid, started = value.get("pid"), value.get("started_at")
        if type(pid) is not int or not 0 < pid <= 0xFFFFFFFF:
            raise ValueError("Native process identity requires a positive PID")
        if not isinstance(started, str) or not started:
            raise ValueError("Native process identity requires a creation time")
        if datetime.fromisoformat(started).tzinfo is None:
            raise ValueError("Native process creation time requires a timezone")
        return cls(pid, started)

    def document(self) -> dict[str, object]:
        return {"pid": self.pid, "started_at": self.started_at}


@dataclass(frozen=True, slots=True)
class Observation:
    session: str
    launch: str
    native: Identity
    endpoint: str | None = None

    def __post_init__(self) -> None:
        if str(UUID(self.session)) != self.session:
            raise ValueError("Native process evidence requires a canonical Codex UUID")
        if re.fullmatch(r"[A-Za-z0-9_-]+", self.launch) is None:
            raise ValueError("Native process evidence requires an exact launch ID")
        if self.endpoint is not None and (
            not isinstance(self.endpoint, str) or not Path(self.endpoint).is_absolute()
        ):
            raise ValueError("Native daemon evidence requires an absolute endpoint")

    @classmethod
    def parse(cls, value: object) -> Observation:
        if (
            not isinstance(value, dict)
            or type(value.get("schema")) is not int
            or value["schema"] != 1
        ):
            raise ValueError("Invalid native process evidence schema")
        session, launch = value.get("session"), value.get("launch")
        if not isinstance(session, str) or not isinstance(launch, str):
            raise ValueError("Invalid native process evidence ownership")
        return cls(
            session, launch, Identity.parse(value.get("native")), value.get("endpoint")
        )

    def document(self) -> dict[str, object]:
        return {
            "schema": 1,
            "session": self.session,
            "launch": self.launch,
            "native": self.native.document(),
            **({"endpoint": self.endpoint} if self.endpoint is not None else {}),
        }

    def path(self, directory: Path) -> Path:
        key = json.dumps(self.document(), sort_keys=True).encode("utf-8")
        return directory / f"{hashlib.sha256(key).hexdigest()}.json"


def state_directory() -> Path:
    configured = os.path.normcase(str(Host.CODEX.configuration_root.resolve()))
    scope = hashlib.sha256(configured.encode("utf-8")).hexdigest()
    return jobs.state_directory() / "codex-processes" / scope


def query(arguments: list[str], value: object = None) -> object:
    result = process.run(
        ["tj", "_process", *arguments],
        input=json.dumps(value),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=20,
        check=False,
    )
    if result.returncode:
        raise LaunchError(result.stderr.strip() or "Native process inspection failed")
    return json.loads(result.stdout)


def record(session: str, launch: str) -> None:
    """Retain the OS-verified calling native identity before ownership admission."""
    native = Identity.parse(query(["owner", "--launch-id", launch]))
    save(Observation(session, launch, native))


def record_prepared(session: str, launch: str) -> bool:
    """Observe an exact prebound native child before its first interactive turn."""
    receipt = query(["spawned", "--launch-id", launch, "--session", f"codex:{session}"])
    if receipt is None:
        return False
    if (
        not isinstance(receipt, dict)
        or receipt.get("launch_id") != launch
        or receipt.get("session") != f"codex:{session}"
    ):
        raise ValueError(
            "Native spawn receipt belongs to a different launch or session"
        )
    endpoint = receipt.get("endpoint")
    if receipt.get("codex_transport") is not None or endpoint is not None:
        expected = (
            Host.CODEX.configuration_root
            / "app-server-control"
            / "app-server-control.sock"
        )
        if (
            receipt.get("codex_transport") != "local-daemon"
            or not isinstance(endpoint, str)
            or not Path(endpoint).is_absolute()
            or Path(endpoint).resolve() != expected.resolve()
        ):
            raise ValueError(
                "Native spawn receipt has different Codex transport evidence"
            )
    save(Observation(session, launch, Identity.parse(receipt.get("native")), endpoint))
    return True


def save(observation: Observation) -> None:
    directory = state_directory()
    directory.mkdir(parents=True, exist_ok=True)
    target = observation.path(directory)
    # Each observation has its own immutable name. Concurrent hooks cannot replace
    # another process's evidence, and interrupted writes never publish partial JSON.
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=directory, suffix=".tmp", delete=False
    ) as stream:
        temporary = Path(stream.name)
        try:
            json.dump(observation.document(), stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        except (OSError, ValueError):
            stream.close()
            temporary.unlink(missing_ok=True)
            raise
    try:
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)


def inventory(*, launch_id: str | None = None) -> dict[str, Liveness]:
    """Retain explicit death evidence; an unknown inspection never becomes death."""
    directory = state_directory()
    try:
        entries = sorted(directory.iterdir())
    except FileNotFoundError:
        return {}
    observations = []
    for path in entries:
        if path.suffix != ".json":
            continue
        observation = Observation.parse(json.loads(path.read_text(encoding="utf-8")))
        if observation.path(directory) != path:
            raise ValueError("Native process evidence does not match its identity")
        if launch_id is None or observation.launch == launch_id:
            observations.append(observation)
    if not observations:
        return {}
    identities = [observation.native.document() for observation in observations]
    statuses = query(["inspect"], identities)
    if not isinstance(statuses, list) or len(statuses) != len(observations):
        raise ValueError("Native process inspection omitted evidence")
    alive: dict[str, set[int]] = {}
    launches: dict[str, set[str]] = {}
    reloadable: dict[str, set[int]] = {}
    for observation, status in zip(observations, statuses, strict=True):
        if (
            Identity.parse(status) != observation.native
            or not isinstance(status, dict)
            or type(status.get("alive")) is not bool
        ):
            raise ValueError("Native process inspection returned different evidence")
        processes = alive.setdefault(observation.session, set())
        launches.setdefault(observation.session, set()).add(observation.launch)
        if status["alive"]:
            processes.add(observation.native.pid)
            if observation.endpoint is not None:
                reloadable.setdefault(observation.session, set()).add(
                    observation.native.pid
                )
    return {
        session: Liveness(
            bool(pids),
            process_ids=tuple(sorted(pids)),
            launch_ids=tuple(sorted(launches[session])),
            processes_reloadable=bool(pids) and pids <= reloadable.get(session, set()),
        )
        for session, pids in alive.items()
    }
