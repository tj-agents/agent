from __future__ import annotations

import hashlib
import json
import os
import platform
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class Receipt:
    version: str
    hashes: Mapping[str, str]
    identity: str | None = None
    archive_digest: str | None = None

    def __post_init__(self) -> None:
        if not self.version or not self.hashes:
            raise ValueError("receipt requires a version and installed files")
        if any(
            not name or not is_sha256(digest) for name, digest in self.hashes.items()
        ):
            raise ValueError("receipt requires file names and SHA-256 hashes")
        if self.archive_digest is not None and not is_sha256(self.archive_digest):
            raise ValueError("receipt archive digest must be SHA-256")


def is_sha256(value: str) -> bool:
    return len(value) == 64 and all(
        character in "0123456789abcdef" for character in value
    )


def sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def read(path: Path) -> Receipt | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if (
        not isinstance(value, dict)
        or type(value.get("schema")) is not int
        or value["schema"] != 1
    ):
        return None
    version, hashes = value.get("version"), value.get("hashes")
    identity, archive_digest = value.get("identity"), value.get("archive_digest")
    if (
        not isinstance(version, str)
        or not isinstance(hashes, dict)
        or any(
            not isinstance(k, str) or not isinstance(v, str) for k, v in hashes.items()
        )
        or (identity is not None and not isinstance(identity, str))
        or (archive_digest is not None and not isinstance(archive_digest, str))
    ):
        return None
    try:
        return Receipt(version, hashes, identity, archive_digest)
    except ValueError:
        return None


def write(path: Path, receipt: Receipt) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    staged = path.with_name(path.name + ".new")
    try:
        staged.write_text(
            json.dumps(
                {
                    "schema": 1,
                    "version": receipt.version,
                    "hashes": dict(receipt.hashes),
                    "identity": receipt.identity,
                    "archive_digest": receipt.archive_digest,
                },
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        staged.replace(path)
    finally:
        staged.unlink(missing_ok=True)


def pending(
    path: Path, version: str, *, names: Iterable[str] | None = None
) -> str | None:
    receipt = read(path)
    if receipt is None:
        return f"missing or invalid receipt: {path}"
    if receipt.version != version:
        return f"recorded version {receipt.version}; expected {version}"
    if names is not None and set(receipt.hashes) != set(names):
        return f"receipt has unexpected files: {path}"
    for name, digest in receipt.hashes.items():
        target = path.parent / name
        try:
            is_file = target.is_file()
        except OSError:
            return f"cannot read installed file: {target}"
        if not is_file:
            return f"missing installed file: {target}"
        if platform.system() != "Windows" and not os.access(target, os.X_OK):
            return f"installed file is not executable: {target}"
        try:
            actual_digest = sha256(target)
        except OSError:
            return f"cannot read installed file: {target}"
        else:
            if actual_digest != digest:
                return f"installed file changed: {target}"
    return None
