"""Run the pinned agent-store CLI and install its release into the native bin directory."""

from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path
import platform
import subprocess
import tarfile
import tempfile
import urllib.request
import zipfile

from tj_cli import native_install, process

REPOSITORY = "tj-agents/agent-store"
PIN = Path(__file__).resolve().with_name("agent-store.json")
TIMEOUT_SECONDS = 20


class AgentStoreError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def pinned_version() -> str:
    return json.loads(PIN.read_text(encoding="utf-8"))["version"]


def executable_name() -> str:
    return "agent-store.exe" if os.name == "nt" else "agent-store"


def executable() -> Path:
    return native_install.default_bin_dir() / executable_name()


def run(*arguments: str, stdin: str | None = None) -> dict:
    """The `data` of one agent-store command; raises AgentStoreError with its `error.code`."""
    path = executable()
    if not path.is_file():
        raise AgentStoreError("missing", f"agent-store is not installed at {path}; run the Agent install")
    try:
        result = process.run([str(path), *arguments], input=stdin, capture_output=True, text=True,
                                encoding="utf-8", timeout=TIMEOUT_SECONDS, check=False)
    except (OSError, subprocess.SubprocessError) as error:
        raise AgentStoreError("unexpected", f"agent-store could not run: {error}") from error
    try:
        document = json.loads(result.stdout)
    except ValueError as error:
        raise AgentStoreError("unexpected", f"agent-store printed no JSON: {result.stderr.strip()}") from error
    if not document.get("ok"):
        failure = document.get("error") or {}
        raise AgentStoreError(failure.get("code", "unexpected"), failure.get("message", "agent-store failed"))
    return document["data"]


def receipt_path() -> Path:
    return executable().with_name(".agent-store.json")


def write_receipt(version: str, binary: bytes) -> None:
    receipt = {"version": version, "sha256": hashlib.sha256(binary).hexdigest()}
    receipt_path().write_text(json.dumps(receipt), encoding="utf-8")


def installed(version: str) -> bool:
    try:
        receipt = json.loads(receipt_path().read_text(encoding="utf-8"))
        binary = executable().read_bytes()
    except (OSError, ValueError):
        return False
    return (isinstance(receipt, dict) and receipt.get("version") == version
            and receipt.get("sha256") == hashlib.sha256(binary).hexdigest())


def asset_name(version: str) -> str:
    if platform.machine().lower() not in {"amd64", "x86_64"}:
        raise AgentStoreError("unsupported", f"no agent-store release for {platform.machine()}")
    if os.name == "nt":
        return f"agent-store-{version}-x86_64-pc-windows-msvc.zip"
    if platform.system() == "Linux":
        return f"agent-store-{version}-x86_64-unknown-linux-musl.tar.gz"
    raise AgentStoreError("unsupported", f"no agent-store release for {platform.system()}")


def _download(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=60) as response:
        return response.read()


def _extract(name: str, archive: bytes) -> bytes:
    wanted = executable_name()
    if name.endswith(".zip"):
        with zipfile.ZipFile(io.BytesIO(archive)) as contents:
            if contents.namelist() != [wanted]:
                raise AgentStoreError("unexpected", f"{name} holds {contents.namelist()}, not {wanted}")
            return contents.read(wanted)
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as contents:
        members = contents.getmembers()
        if [member.name for member in members] != [wanted] or not members[0].isfile():
            raise AgentStoreError("unexpected", f"{name} does not hold exactly {wanted}")
        extracted = contents.extractfile(members[0])
        assert extracted is not None
        return extracted.read()


def install(download=_download) -> str:
    """Install the pinned release, verified against its published SHA-256; returns the version."""
    version = pinned_version()
    name = asset_name(version)
    base = f"https://github.com/{REPOSITORY}/releases/download/{version}/{name}"
    archive = download(base)
    expected = download(base + ".sha256").decode("utf-8").split()[0]
    if hashlib.sha256(archive).hexdigest() != expected:
        raise AgentStoreError("unexpected", f"{name} failed SHA-256 verification")
    binary = _extract(name, archive)
    target = executable()
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=target.parent, prefix=".agent-store-", delete=False) as staging:
        staging.write(binary)
    staged = Path(staging.name)
    try:
        staged.chmod(0o755)
        os.replace(staged, target)
    finally:
        staged.unlink(missing_ok=True)
    write_receipt(version, binary)
    return version


def pending() -> str | None:
    version = pinned_version()
    return None if installed(version) else f"install agent-store {version} into {executable().parent}"

