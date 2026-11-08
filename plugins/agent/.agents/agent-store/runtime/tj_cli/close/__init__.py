"""Close a caller-specified frontend and native terminal tab."""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Literal, NotRequired, TypedDict, cast

from tj_cli import processes
from tj_cli.close import terminal


class ProcessTarget(TypedDict):
    pid: int
    started_at: float


class CloseTarget(TypedDict):
    schema_version: Literal[1]
    frontend: ProcessTarget
    wrapper: ProcessTarget
    terminal: dict[str, object]


class Receipt(TypedDict):
    schema_version: Literal[1]
    operation: str
    status: str
    at: float
    target: CloseTarget
    reason: NotRequired[str]


type Guard = Callable[[], None]


def _read_json(path: Path) -> dict[str, object]:
    try:
        value: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise processes.Refusal("cannot read JSON: " + str(path)) from error
    if not isinstance(value, dict):
        raise processes.Refusal("JSON object required: " + str(path))
    return cast(dict[str, object], value)


def _write_json(path: Path, value: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name("." + path.name + ".tmp")
    temporary.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def validate_target(value: object) -> CloseTarget:
    if not isinstance(value, dict):
        raise processes.Refusal("target must be an object")
    if value.get("schema_version") != 1:
        raise processes.Refusal("target schema_version must be 1")
    frontend = value.get("frontend")
    wrapper = value.get("wrapper")
    terminal_value = value.get("terminal")
    processes.valid_identity(frontend)
    processes.valid_identity(wrapper)
    if not isinstance(terminal_value, dict) or not isinstance(
        terminal_value.get("kind"), str
    ):
        raise processes.Refusal("target needs terminal object")
    if terminal_value["kind"] == "windows-terminal":
        binding = terminal_value.get("tab_binding")
        if not isinstance(binding, dict):
            raise processes.Refusal(
                "Windows Terminal target needs tab binding evidence"
            )
        if (
            not isinstance(binding.get("runtime_id"), str)
            or not binding["runtime_id"].strip()
            or not isinstance(binding.get("observed_title"), str)
            or not binding["observed_title"].strip()
            or binding.get("evidence") != "windows-uia-unique-title-at-launch"
        ):
            raise processes.Refusal(
                "Windows Terminal tab binding evidence is incomplete"
            )
    return cast(CloseTarget, value)


def target(path: Path) -> CloseTarget:
    return validate_target(_read_json(path))


def receipt(
    path: Path, target_value: CloseTarget, status: str, **extra: object
) -> None:
    _write_json(
        path,
        {
            "schema_version": 1,
            "operation": "cli-close",
            "status": status,
            "at": time.time(),
            "target": target_value,
            **extra,
        },
    )


def _no_guard() -> None:
    return None


def close(
    target_value: CloseTarget, receipt_path: Path, *, before_close: Guard = _no_guard
) -> None:
    try:
        terminal.close(target_value, before_close=before_close)
        deadline = time.monotonic() + 5
        while (
            processes.matches(target_value["frontend"]) and time.monotonic() < deadline
        ):
            time.sleep(0.05)
        processes.require_exited(target_value["frontend"])
    except Exception as error:
        receipt(receipt_path, target_value, "refused", reason=str(error))
        raise
    receipt(receipt_path, target_value, "closed")


__all__ = [
    "CloseTarget",
    "ProcessTarget",
    "Receipt",
    "close",
    "receipt",
    "target",
    "terminal",
    "validate_target",
]
