from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import time
from typing import Any

try:
    import tomllib
except ModuleNotFoundError:
    print("hook-control requires Python 3.11 or newer", file=sys.stderr)
    raise SystemExit(2)


SIDECAR_SUFFIX = ".hook-control.json"
LOCK_SUFFIX = ".hook-control.lock"
# MoveFileEx reports a conflicting open handle as either access denied or sharing violation.
WINDOWS_REPLACE_CONFLICTS = (5, 32)
NORMAL_HEADER = re.compile(r"^\s*\[features\]\s*(?:#.*)?(?:\r?\n)?$")
HEADER = re.compile(r"^\s*\[[^\]]+\]\s*(?:#.*)?(?:\r?\n)?$")
NORMAL_HOOKS = re.compile(r"^(\s*)hooks(\s*=\s*)(true|false)(\s*(?:#.*)?)(\r?\n)?$", re.IGNORECASE)
DOTTED_HOOKS = re.compile(r"^(\s*)features\.hooks(\s*=\s*)(true|false)(\s*(?:#.*)?)(\r?\n)?$", re.IGNORECASE)


class ControlError(Exception):
    pass


def default_codex_home() -> Path:
    configured = os.environ.get("CODEX_HOME")
    return Path(configured) if configured else Path.home() / ".codex"


def target_config(arguments: argparse.Namespace) -> Path:
    if arguments.scope == "global":
        return (arguments.codex_home or default_codex_home()) / "config.toml"
    if arguments.project is None:
        raise ControlError("--project is required when --scope project")
    return arguments.project / ".codex" / "config.toml"


def sidecar_path(config: Path) -> Path:
    return config.with_name(config.name + SIDECAR_SUFFIX)


def lock_path(config: Path) -> Path:
    return config.with_name(config.name + LOCK_SUFFIX)


def reject_symlink(path: Path) -> None:
    if path.exists() and path.is_symlink():
        raise ControlError(f"refusing symlinked path: {path}")


class ExclusiveLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.handle: Any = None

    def __enter__(self) -> "ExclusiveLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        reject_symlink(self.path)
        deadline = time.monotonic() + 10
        while True:
            handle: Any = None
            try:
                handle = open(self.path, "a+b")
                handle.seek(0)
                if not handle.read(1):
                    handle.seek(0)
                    handle.write(b"0")
                    handle.flush()
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                self.handle = handle
                return self
            except OSError:
                if handle is not None:
                    handle.close()
                if time.monotonic() >= deadline:
                    raise ControlError(f"timed out waiting for hook-control lock: {self.path}")
                time.sleep(0.05)

    def __exit__(self, exception_type: Any, exception: Any, traceback: Any) -> None:
        if self.handle is None:
            return
        self.handle.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        self.handle.close()


def read_config(path: Path) -> tuple[bytes, dict[str, Any]]:
    reject_symlink(path)
    if not path.exists():
        return b"", {}
    try:
        raw = retry_windows_sharing_violation(path.read_bytes)
        parsed = tomllib.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise ControlError(f"cannot parse {path}: {error}") from error
    if not isinstance(parsed, dict):
        raise ControlError(f"unexpected TOML root in {path}")
    return raw, parsed


def atomic_write(path: Path, data: bytes) -> None:
    reject_symlink(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        retry_windows_sharing_violation(lambda: os.replace(temporary, path))
    finally:
        if temporary.exists():
            temporary.unlink()


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    atomic_write(path, (json.dumps(value, sort_keys=True, indent=2) + "\n").encode("utf-8"))


def retry_windows_sharing_violation(action: Any, timeout: float = 2) -> Any:
    deadline = time.monotonic() + timeout
    while True:
        try:
            return action()
        except PermissionError as error:
            if (os.name != "nt" or getattr(error, "winerror", None) not in WINDOWS_REPLACE_CONFLICTS
                    or time.monotonic() >= deadline):
                raise
            time.sleep(0.01)


def load_snapshot(path: Path) -> dict[str, Any] | None:
    reject_symlink(path)
    if not path.exists():
        return None
    try:
        snapshot = json.loads(retry_windows_sharing_violation(
            lambda: path.read_text(encoding="utf-8")))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ControlError(f"cannot read hook-control snapshot {path}: {error}") from error
    if not isinstance(snapshot, dict) or type(snapshot.get("version")) is not int or snapshot["version"] != 1:
        raise ControlError(f"unrecognized hook-control snapshot: {path}")
    original = snapshot.get("original")
    if not ((type(original) is str and original == "absent") or type(original) is bool):
        raise ControlError(f"invalid original value in hook-control snapshot: {path}")
    for field in ("config_existed", "features_table_existed"):
        if type(snapshot.get(field)) is not bool:
            raise ControlError(f"invalid {field} in hook-control snapshot: {path}")
    original_sha256 = snapshot.get("original_sha256")
    if not isinstance(original_sha256, str) or re.fullmatch(r"[0-9a-f]{64}", original_sha256) is None:
        raise ControlError(f"invalid original_sha256 in hook-control snapshot: {path}")
    if snapshot.get("owned_separator") not in ("", "\n", "\r\n"):
        raise ControlError(f"invalid owned_separator in hook-control snapshot: {path}")
    disabled_sha256 = snapshot.get("disabled_sha256")
    if disabled_sha256 is not None and (not isinstance(disabled_sha256, str) or re.fullmatch(r"[0-9a-f]{64}", disabled_sha256) is None):
        raise ControlError(f"invalid disabled_sha256 in hook-control snapshot: {path}")
    return snapshot


def canonical_value(parsed: dict[str, Any]) -> bool | str:
    features = parsed.get("features")
    if features is None:
        return "absent"
    if not isinstance(features, dict):
        raise ControlError("features must be a TOML table before hook-control can edit it")
    value = features.get("hooks", "absent")
    if value == "absent":
        return value
    if not isinstance(value, bool):
        raise ControlError("features.hooks must be true or false before hook-control can edit it")
    return value


def locations(text: str, expected: bool | str) -> tuple[list[tuple[str, int]], int | None]:
    lines = text.splitlines(keepends=True)
    headers = [index for index, line in enumerate(lines) if HEADER.match(line)]
    normal_header = [index for index, line in enumerate(lines) if NORMAL_HEADER.match(line)]
    normal: list[tuple[str, int]] = []
    for header in normal_header:
        end = next((index for index in headers if index > header), len(lines))
        for index in range(header + 1, end):
            if NORMAL_HOOKS.match(lines[index]):
                normal.append(("normal", index))
    first_header = headers[0] if headers else len(lines)
    dotted = [("dotted", index) for index in range(first_header) if DOTTED_HOOKS.match(lines[index])]
    result = normal + dotted
    if len(normal_header) > 1 or len(result) > 1:
        raise ControlError("ambiguous features.hooks syntax; hook-control will not rewrite this TOML")
    if expected != "absent" and len(result) != 1:
        raise ControlError("features.hooks uses unfamiliar TOML syntax; hook-control will not rewrite it")
    return result, normal_header[0] if normal_header else None


def expected_after(parsed: dict[str, Any], value: bool | str) -> dict[str, Any]:
    result = copy.deepcopy(parsed)
    if value == "absent":
        features = result.get("features")
        if isinstance(features, dict):
            features.pop("hooks", None)
    else:
        features = result.setdefault("features", {})
        if not isinstance(features, dict):
            raise ControlError("features must be a TOML table before hook-control can edit it")
        features["hooks"] = value
    return result


def line_ending(text: str) -> str:
    return "\r\n" if "\r\n" in text else "\n"


def add_line(text: str, line: str, index: int | None = None) -> str:
    lines = text.splitlines(keepends=True)
    ending = line_ending(text)
    item = line + ending
    if index is None:
        if text and not text.endswith(("\n", "\r")):
            text += ending
        return text + item
    lines.insert(index, item)
    return "".join(lines)


def remove_line(text: str, index: int) -> str:
    lines = text.splitlines(keepends=True)
    del lines[index]
    return "".join(lines)


def edit_value(raw: bytes, parsed: dict[str, Any], value: bool | str) -> bytes:
    text = raw.decode("utf-8")
    current = canonical_value(parsed)
    found, normal_header = locations(text, current)
    if current == "absent" and found:
        raise ControlError("ambiguous features.hooks syntax; hook-control will not rewrite this TOML")
    if current != "absent":
        style, index = found[0]
        if value == "absent":
            text = remove_line(text, index)
        else:
            lines = text.splitlines(keepends=True)
            matcher = NORMAL_HOOKS if style == "normal" else DOTTED_HOOKS
            match = matcher.match(lines[index])
            if match is None:
                raise ControlError("features.hooks uses unfamiliar TOML syntax; hook-control will not rewrite it")
            ending = match.group(5) or ""
            lines[index] = f"{match.group(1)}{('hooks' if style == 'normal' else 'features.hooks')}{match.group(2)}{str(value).lower()}{match.group(4)}{ending}"
            text = "".join(lines)
    elif value != "absent":
        features = parsed.get("features")
        if normal_header is not None:
            text = add_line(text, f"hooks = {str(value).lower()}", normal_header + 1)
        elif features is not None:
            if not isinstance(features, dict):
                raise ControlError("features must be a TOML table before hook-control can edit it")
            headers = [index for index, line in enumerate(text.splitlines(keepends=True)) if HEADER.match(line)]
            text = add_line(text, f"features.hooks = {str(value).lower()}", headers[0] if headers else 0)
        else:
            prefix = "" if not text or text.endswith(("\n", "\r")) else line_ending(text)
            text = f"{text}{prefix}[features]{line_ending(text)}hooks = {str(value).lower()}{line_ending(text)}"
    try:
        after = tomllib.loads(text)
    except tomllib.TOMLDecodeError as error:
        raise ControlError(f"hook-control produced invalid TOML: {error}") from error
    if after != expected_after(parsed, value):
        raise ControlError("hook-control refused an edit that changes more than features.hooks")
    return text.encode("utf-8")


def remove_introduced_empty_features_table(text: str) -> str:
    lines = text.splitlines(keepends=True)
    headers = [index for index, line in enumerate(lines) if HEADER.match(line)]
    feature_headers = [index for index, line in enumerate(lines) if NORMAL_HEADER.match(line)]
    if len(feature_headers) != 1:
        return text
    header = feature_headers[0]
    end = next((index for index in headers if index > header), len(lines))
    if any(lines[index].strip() for index in range(header + 1, end)):
        return text
    del lines[header]
    return "".join(lines)


def feature_table_exists(text: str) -> bool:
    return any(NORMAL_HEADER.match(line) for line in text.splitlines(keepends=True))


def snapshot_for(scope: str, config: Path, raw: bytes, parsed: dict[str, Any]) -> dict[str, Any]:
    separator = ""
    if raw and not raw.endswith((b"\n", b"\r")) and parsed.get("features") is None:
        separator = line_ending(raw.decode("utf-8"))
    return {
        "version": 1,
        "scope": scope,
        "config": str(config.resolve(strict=False)),
        "original": canonical_value(parsed),
        "config_existed": config.exists(),
        "features_table_existed": feature_table_exists(raw.decode("utf-8")),
        "original_sha256": hashlib.sha256(raw).hexdigest(),
        "owned_separator": separator,
        "disabled_sha256": None,
    }


def validate_snapshot(snapshot: dict[str, Any], scope: str, config: Path) -> None:
    if type(snapshot.get("scope")) is not str or type(snapshot.get("config")) is not str:
        raise ControlError("invalid scope ownership in hook-control snapshot")
    if snapshot["scope"] != scope or snapshot["config"] != str(config.resolve(strict=False)):
        raise ControlError("hook-control snapshot belongs to a different scope or config path")


def disable(scope: str, config: Path) -> dict[str, Any]:
    sidecar = sidecar_path(config)
    with ExclusiveLock(lock_path(config)):
        raw, parsed = read_config(config)
        current = canonical_value(parsed)
        snapshot = load_snapshot(sidecar)
        updated: bytes | None = None
        if current is not False:
            updated = edit_value(raw, parsed, False)
        if snapshot is None:
            snapshot = snapshot_for(scope, config, raw, parsed)
        else:
            validate_snapshot(snapshot, scope, config)
            original = snapshot["original"]
            if current != False and current != original:
                raise ControlError("features.hooks changed since hook-control disabled it; refusing to overwrite it")
        if current is False:
            if load_snapshot(sidecar) is None:
                atomic_json(sidecar, snapshot)
            return {"action": "off", "changed": False, "config": str(config), "saved": snapshot["original"]}
        atomic_json(sidecar, snapshot)
        if config.exists() and config.read_bytes() != raw:
            raise ControlError("config changed while hook-control was preparing its edit")
        if updated is None:
            raise ControlError("hook-control could not prepare its disable edit")
        atomic_write(config, updated)
        snapshot["disabled_sha256"] = hashlib.sha256(updated).hexdigest()
        atomic_json(sidecar, snapshot)
        return {"action": "off", "changed": True, "config": str(config), "saved": snapshot["original"]}


def restore(scope: str, config: Path) -> dict[str, Any]:
    sidecar = sidecar_path(config)
    with ExclusiveLock(lock_path(config)):
        snapshot = load_snapshot(sidecar)
        if snapshot is None:
            raise ControlError("hook-control on only restores this utility's saved state; no off snapshot exists")
        validate_snapshot(snapshot, scope, config)
        raw, parsed = read_config(config)
        current = canonical_value(parsed)
        original = snapshot["original"]
        if current is not False:
            raise ControlError("features.hooks changed while disabled; refusing to overwrite the user value")
        if original is False:
            reject_symlink(sidecar)
            retry_windows_sharing_violation(sidecar.unlink)
            return {"action": "on", "changed": False, "config": str(config), "restored": original}
        updated = edit_value(raw, parsed, original)
        if config.exists() and config.read_bytes() != raw:
            raise ControlError("config changed while hook-control was preparing its restore")
        if original == "absent" and not snapshot.get("features_table_existed"):
            updated = remove_introduced_empty_features_table(updated.decode("utf-8")).encode("utf-8")
            candidate = updated.decode("utf-8")
            if candidate.strip() == "" and not snapshot.get("config_existed"):
                config.unlink()
            else:
                separator = snapshot.get("owned_separator", "")
                if separator and updated.endswith(separator.encode("utf-8")):
                    without_separator = updated[:-len(separator.encode("utf-8"))]
                    if hashlib.sha256(without_separator).hexdigest() == snapshot.get("original_sha256"):
                        updated = without_separator
                atomic_write(config, updated)
        else:
            atomic_write(config, updated)
        reject_symlink(sidecar)
        retry_windows_sharing_violation(sidecar.unlink)
        return {"action": "on", "changed": True, "config": str(config), "restored": original}


def disabled_entries(parsed: dict[str, Any]) -> int:
    hooks = parsed.get("hooks")
    if not isinstance(hooks, dict):
        return 0
    state = hooks.get("state")
    if isinstance(state, list):
        return sum(isinstance(item, dict) and item.get("enabled") is False for item in state)
    if isinstance(state, dict):
        if state.get("enabled") is False:
            return 1
        return sum(isinstance(item, dict) and item.get("enabled") is False for item in state.values())
    return 0


def status(scope: str, config: Path) -> dict[str, Any]:
    raw, parsed = read_config(config)
    snapshot = load_snapshot(sidecar_path(config))
    if snapshot is not None:
        validate_snapshot(snapshot, scope, config)
    legacy = parsed.get("features", {}).get("codex_hooks", "absent") if isinstance(parsed.get("features", {}), dict) else "invalid"
    return {
        "scope": scope,
        "config": str(config),
        "canonical_features_hooks": canonical_value(parsed),
        "legacy_features_codex_hooks": legacy,
        "saved_scope_gate": snapshot["original"] if snapshot else "none",
        "effective_state": "not verified",
        "individually_disabled_entries": disabled_entries(parsed),
        "override_note": "project, profile, command-line, or administrator settings can override this saved layer",
        "project_trust_note": "a project .codex/config.toml applies only when Codex trusts that repository" if scope == "project" else None,
        "lifecycle_note": "the gate covers ordinary native lifecycle hooks; builtin cleanup and legacy notify behavior are outside it",
        "restart_note": "restart Codex; no proven hot reload exists",
    }


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(description="Safely disable or restore Codex hooks with features.hooks")
    command.add_argument("action", choices=("status", "off", "on"))
    command.add_argument("--scope", choices=("global", "project"), default="global")
    command.add_argument("--project", type=Path)
    command.add_argument("--codex-home", type=Path)
    return command


def main(argv: list[str] | None = None) -> int:
    arguments = parser().parse_args(argv)
    try:
        config = target_config(arguments)
        if arguments.action == "status":
            result = status(arguments.scope, config)
        elif arguments.action == "off":
            result = disable(arguments.scope, config)
        else:
            result = restore(arguments.scope, config)
    except ControlError as error:
        print(f"hook-control: {error}", file=sys.stderr)
        return 2
    except OSError as error:
        print(f"hook-control: {error}", file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
