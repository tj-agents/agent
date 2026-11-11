from __future__ import annotations

import hashlib
import io
import tarfile
import urllib.request
import zipfile
from pathlib import Path

from tj_cli import tool_receipts
from tj_cli.errors import TjCliError


def install(
    *,
    url: str,
    digest: str,
    member: str,
    target: Path,
    receipt_path: Path,
    version: str,
    error_type: type[TjCliError],
) -> None:
    with urllib.request.urlopen(url, timeout=600) as response:
        contents: bytes = response.read()
    if hashlib.sha256(contents).hexdigest() != digest:
        raise error_type(f"{url.rsplit('/', 1)[-1]} failed SHA-256 verification")
    if url.endswith(".zip"):
        with zipfile.ZipFile(io.BytesIO(contents)) as archive:
            binary = archive.read(member)
    else:
        with tarfile.open(fileobj=io.BytesIO(contents), mode="r:*") as tar:
            entry = tar.getmember(member)
            if not entry.isfile():
                raise error_type(f"archive member is not a regular file: {member}")
            extracted = tar.extractfile(entry)
            if extracted is None:
                raise error_type(f"archive has no executable: {member}")
            with extracted:
                binary = extracted.read()
    target.parent.mkdir(parents=True, exist_ok=True)
    staged = target.with_name(target.name + ".new")
    try:
        staged.write_bytes(binary)
        staged.chmod(0o755)
        staged.replace(target)
    finally:
        staged.unlink(missing_ok=True)
    tool_receipts.write(
        receipt_path,
        tool_receipts.Receipt(version, {target.name: tool_receipts.sha256(target)}),
    )
