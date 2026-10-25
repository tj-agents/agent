"""Safely converge one receipted Agent runtime closure (Python 3.9+)."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import time


REPOSITORY = "tj-agents/agent"
SHA256 = re.compile(r"[0-9a-f]{64}\Z")


class DeliveryError(RuntimeError):
    pass


def is_reparse(path: Path) -> bool:
    try:
        return path.is_symlink() or bool(getattr(path.lstat(), "st_file_attributes", 0) & 0x400)
    except OSError:
        return True


def _lexical_ancestors(path: Path, label: str) -> Path:
    """Reject a selected path whose lexical route crosses a link.

    ``Path.resolve`` intentionally follows links, so it cannot establish that a
    caller-selected state root (or source root) was not reached through one.
    Check the lexical chain first, then use resolve only for containment.
    """
    lexical = Path(os.path.abspath(os.fspath(path)))
    for current in (lexical, *lexical.parents):
        try:
            current.lstat()
        except FileNotFoundError:
            continue
        except OSError as error:
            raise DeliveryError(f"{label} is unavailable: {current}") from error
        if is_reparse(current):
            raise DeliveryError(f"{label} traverses a link: {current}")
    return lexical


def relative(value: object) -> str:
    if not isinstance(value, str) or not value or "\\" in value or ":" in value:
        raise DeliveryError(f"unsafe runtime path: {value!r}")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or path.as_posix() != value:
        raise DeliveryError(f"unsafe runtime path: {value!r}")
    return value


def _key(value: str) -> str:
    return value.replace("/", "\\").casefold()


def _safe(root: Path, name: str, *, allow_missing=True) -> Path:
    name = relative(name)
    root = _lexical_ancestors(root, "managed runtime root")
    if is_reparse(root):
        raise DeliveryError(f"managed runtime root is a link: {root}")
    current = root
    for part in PurePosixPath(name).parts:
        current /= part
        if current.exists() and is_reparse(current):
            raise DeliveryError(f"managed runtime traverses a link: {current}")
    try:
        resolved = current.resolve()
        if not resolved.is_relative_to(root.resolve()):
            raise DeliveryError(f"managed runtime escapes its root: {current}")
    except OSError as error:
        raise DeliveryError(f"managed runtime path is unavailable: {current}") from error
    if not allow_missing and not current.exists():
        raise DeliveryError(f"managed runtime file is unavailable: {current}")
    return current


def digest(contents: dict[str, bytes]) -> str:
    value = hashlib.sha256()
    for name, body in sorted(contents.items()):
        value.update(name.encode("utf-8") + b"\0" + str(len(body)).encode("ascii") + b"\0" + body)
    return value.hexdigest()


def closure(root: Path, exports: list[str]) -> dict[str, bytes]:
    root = _lexical_ancestors(root, "runtime source root")
    if is_reparse(root):
        raise DeliveryError(f"runtime source root is a link: {root}")
    results, aliases = {}, set()
    for export in exports:
        export = relative(export)
        target = _safe(root, export, allow_missing=False)
        paths = target.rglob("*") if target.is_dir() else (target,)
        for path in sorted(paths):
            if is_reparse(path):
                raise DeliveryError(f"runtime source contains a link: {path}")
            if not path.is_file() or "__pycache__" in path.parts or path.suffix == ".pyc":
                continue
            name = path.relative_to(root).as_posix()
            alias = _key(name)
            if alias in aliases:
                raise DeliveryError(f"runtime source has Windows path aliases: {name}")
            aliases.add(alias)
            results[name] = path.read_bytes()
    return results


def _files(value: object) -> dict[str, str]:
    if not isinstance(value, dict) or not value:
        raise DeliveryError("runtime receipt requires nonempty files")
    result, aliases = {}, set()
    for name, body in value.items():
        name = relative(name)
        if not isinstance(body, str) or not SHA256.fullmatch(body):
            raise DeliveryError(f"runtime receipt has invalid file digest: {name}")
        alias = _key(name)
        if alias in aliases:
            raise DeliveryError(f"runtime receipt has Windows path aliases: {name}")
        aliases.add(alias)
        result[name] = body
    return result


def _exports(value: object, label: str) -> list[str]:
    if not isinstance(value, list) or not value or any(not isinstance(item, str) for item in value):
        raise DeliveryError(f"{label} are malformed")
    try:
        if len(value) != len(set(value)):
            raise DeliveryError(f"{label} contain duplicates")
    except TypeError as error:
        raise DeliveryError(f"{label} are malformed") from error
    values = [relative(item) for item in value]
    if not all(item.startswith(".agents/") for item in values):
        raise DeliveryError(f"{label} are malformed")
    return values


def validate_receipt(value: object, source: Path | None = None) -> dict:
    if not isinstance(value, dict) or value.get("repository") != REPOSITORY:
        raise DeliveryError("runtime receipt is not an Agent receipt")
    revision, exports, aggregate = value.get("revision"), value.get("exports"), value.get("sha256")
    if (not isinstance(revision, str) or not revision
            or not isinstance(aggregate, str) or not SHA256.fullmatch(aggregate)):
        raise DeliveryError("runtime receipt is malformed")
    exports = _exports(exports, "runtime receipt exports")
    files = _files(value.get("files"))
    pending = value.get("pending")
    if pending is not None:
        if not isinstance(pending, dict) or set(pending) != {"files", "exports"}:
            raise DeliveryError("runtime pending receipt is malformed")
        _exports(pending["exports"], "runtime pending exports")
        pending_files = pending["files"]
        if not isinstance(pending_files, dict):
            raise DeliveryError("runtime pending files are malformed")
        for name, choices in pending_files.items():
            relative(name)
            if not isinstance(choices, list) or not choices or any(not isinstance(item, str) or not SHA256.fullmatch(item) for item in choices):
                raise DeliveryError("runtime pending file hashes are malformed")
    result = {"repository": REPOSITORY, "revision": revision, "exports": list(exports), "sha256": aggregate,
              "files": files, **({"pending": pending} if pending is not None else {})}
    if source is not None:
        contents = closure(source, result["exports"])
        hashes = {name: hashlib.sha256(body).hexdigest() for name, body in contents.items()}
        if hashes != files or digest(contents) != aggregate:
            raise DeliveryError("runtime receipt does not match its exact source closure")
    return result


def _read(path: Path) -> dict | None:
    try:
        return validate_receipt(json.loads(path.read_text(encoding="utf-8")))
    except FileNotFoundError:
        return None
    except (OSError, ValueError, DeliveryError) as error:
        raise DeliveryError(f"runtime receipt is unavailable or malformed: {error}") from error


def _atomic(path: Path, body: bytes) -> None:
    _safe(path.parent, path.name)
    temporary = path.parent / ("." + path.name + "." + secrets.token_hex(12) + ".tmp")
    if temporary.exists():
        raise DeliveryError("runtime temporary path is unavailable")
    try:
        with temporary.open("xb") as stream:
            stream.write(body)
        if is_reparse(path.parent) or is_reparse(temporary):
            raise DeliveryError("runtime destination changed during write")
        os.replace(temporary, path)
    finally:
        if temporary.exists() and not is_reparse(temporary):
            temporary.unlink()


def _lock(root: Path):
    import plugin_autoupdate

    path = root.parent / ".agent-runtime-delivery.lock"
    deadline = time.monotonic() + 30
    lock = None
    while lock is None and time.monotonic() < deadline:
        lock = plugin_autoupdate.acquire_lock(path)
        if lock is None:
            time.sleep(0.02)
    if lock is None:
        raise DeliveryError("runtime delivery is busy")
    return lock


def _owned(receipt: dict | None) -> dict[str, set[str]]:
    if receipt is None:
        return {}
    if receipt.get("pending"):
        return {name: set(values) for name, values in receipt["pending"]["files"].items()}
    return {name: {value} for name, value in receipt["files"].items()}


def _pending(previous: dict | None, desired: dict) -> dict:
    union = _owned(previous)
    for name, value in desired["files"].items():
        union.setdefault(name, set()).add(value)
    return {"repository": REPOSITORY, "revision": desired["revision"], "exports": desired["exports"],
            "sha256": desired["sha256"], "files": desired["files"],
            "pending": {"files": {name: sorted(values) for name, values in sorted(union.items())},
                        "exports": sorted(set((previous or {}).get("exports", [])) | set(desired["exports"]))}}


def _write_receipt(root: Path, receipt: dict) -> None:
    _atomic(_safe(root, "source.json"), (json.dumps(receipt, indent=2, sort_keys=True) + "\n").encode("utf-8"))


def _inspect(root: Path, desired: dict, previous: dict | None) -> list[str]:
    errors, owned = [], _owned(previous)
    for name, expected in desired["files"].items():
        path = _safe(root, name)
        if not path.exists():
            errors.append(f"missing managed runtime file: {name}")
        elif not path.is_file():
            errors.append(f"managed runtime path is not a file: {name}")
        elif hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            errors.append(f"managed runtime file differs: {name}")
    for name, choices in owned.items():
        if name in desired["files"]:
            continue
        path = _safe(root, name)
        if path.exists():
            if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() not in choices:
                errors.append(f"retired managed runtime file conflicts: {name}")
            else:
                errors.append(f"retired managed runtime file remains: {name}")
    return errors


def check(root: Path, source: Path, receipt: dict) -> list[str]:
    desired = validate_receipt(receipt, source)
    root = _lexical_ancestors(Path(root), "managed runtime root")
    if not root.exists():
        return ["managed runtime root is missing"]
    if is_reparse(root):
        return ["managed runtime root is a link"]
    current = _read(_safe(root, "source.json"))
    if current is None:
        return ["managed runtime receipt is missing"]
    if current.get("pending"):
        return ["managed runtime delivery is incomplete"]
    if current != desired:
        return ["managed runtime receipt differs from desired source"]
    return _inspect(root, desired, current)


def apply(root: Path, source: Path, receipt: dict) -> list[str]:
    desired = validate_receipt(receipt, source)
    root = _lexical_ancestors(Path(root), "managed runtime root")
    if root.exists() and is_reparse(root):
        raise DeliveryError("managed runtime root is a link")
    root.mkdir(parents=True, exist_ok=True)
    if is_reparse(root):
        raise DeliveryError("managed runtime root is a link")
    lock = _lock(root)
    try:
        receipt_path = _safe(root, "source.json")
        previous = _read(receipt_path)
        if previous == desired and not _inspect(root, desired, previous):
            return []
        owned = _owned(previous)
        contents = closure(source, desired["exports"])
        errors = []
        for name, body in contents.items():
            path = _safe(root, name)
            if path.exists() and name not in owned and path.read_bytes() != body:
                errors.append(f"unowned runtime path conflicts: {name}")
        if errors:
            return errors
        # Do not publish incoming hashes until every destination is either
        # absent or already owned. Otherwise a retry could mistake a user's
        # colliding file for payload interrupted after write-ahead.
        _write_receipt(root, _pending(previous, desired))
        for name, body in contents.items():
            path = _safe(root, name)
            if path.is_file() and path.read_bytes() == body:
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            _atomic(path, body)
        for name, choices in owned.items():
            if name in desired["files"]:
                continue
            path = _safe(root, name)
            if not path.exists():
                continue
            if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() not in choices:
                errors.append(f"retired managed runtime file conflicts: {name}")
                continue
            path.unlink()
        for export in sorted(set((previous or {}).get("exports", [])) | set(desired["exports"]), reverse=True):
            directory = _safe(root, export)
            for child in sorted(directory.rglob("*"), key=lambda item: len(item.parts), reverse=True) if directory.is_dir() else ():
                if child.is_dir() and not is_reparse(child):
                    try:
                        child.rmdir()
                    except OSError:
                        pass
        errors += _inspect(root, desired, {**desired, "pending": _pending(previous, desired)["pending"]})
        if errors:
            return errors
        _write_receipt(root, desired)
        return []
    finally:
        lock.close()


def native_receipt(target_receipt: dict, source: Path) -> dict:
    """Derive a ready receipt from verified native package bytes and declared exports."""
    target = validate_receipt(target_receipt)
    contents = closure(source, target["exports"])
    files = {name: hashlib.sha256(body).hexdigest() for name, body in contents.items()}
    current = {"repository": REPOSITORY, "revision": target["revision"], "exports": target["exports"],
               "sha256": digest(contents), "files": files}
    if current["sha256"] == target["sha256"] and files == target["files"]:
        return target
    current["revision"] = "native:" + current["sha256"]
    return current
