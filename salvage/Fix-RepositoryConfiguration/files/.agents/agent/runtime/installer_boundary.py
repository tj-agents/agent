"""Verify the packaged root installer before protected host operations."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path


class InstallerBoundaryError(RuntimeError):
    pass


RESOURCE = Path(".agents") / "agent" / "resources" / "installer" / "install.ps1"
GENERATED_RESOURCE = Path("plugins") / "agent" / RESOURCE


def installer_resource(root: Path) -> Path:
    return _regular_file(Path(root) / RESOURCE)


def verify_root_installer(root: Path) -> Path:
    source = Path(root) / "install.ps1"
    resource = Path(root) / GENERATED_RESOURCE
    try:
        source_metadata = source.lstat()
        source_bytes = source.read_bytes()
    except OSError as error:
        raise InstallerBoundaryError("root installer could not be read") from error
    if source.is_symlink() or not source.is_file() or source_metadata.st_nlink != 1:
        raise InstallerBoundaryError("root installer is not a regular unlinked file")
    resource = _regular_file(resource)
    try:
        resource_bytes = resource.read_bytes()
    except OSError as error:
        raise InstallerBoundaryError("packaged installer could not be read") from error
    if hashlib.sha256(source_bytes).digest() != hashlib.sha256(resource_bytes).digest():
        raise InstallerBoundaryError("packaged installer does not match install.ps1")
    return resource


def _regular_file(path: Path) -> Path:
    try:
        metadata = path.lstat()
    except OSError as error:
        raise InstallerBoundaryError(
            f"packaged installer is unavailable: {path}"
        ) from error
    if path.is_symlink() or not path.is_file() or metadata.st_nlink != 1:
        raise InstallerBoundaryError(
            f"packaged installer is not a regular unlinked file: {path}"
        )
    return path


def protected_installer() -> Path:
    root = Path(__file__).resolve().parents[3]
    if (root / "install.ps1").is_file():
        return verify_root_installer(root)
    return installer_resource(root)


def lexical_absolute(path: Path) -> Path:
    return path.expanduser().absolute()


def acquire_codex_cache(
    project: Path, identities: tuple[str, ...]
) -> list[dict[str, object]]:
    from codex import installed

    requested = tuple(identities)
    if not requested or requested != tuple(sorted(set(requested))):
        raise InstallerBoundaryError("Codex cache identities must be sorted and unique")
    try:
        root = Path(project).resolve(strict=True)
        rows = installed.acquire_cache(root, requested)
    except (OSError, ValueError, RuntimeError) as error:
        raise InstallerBoundaryError(
            f"Codex cache acquisition failed for {project}: {error}"
        ) from error
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        raise InstallerBoundaryError(
            "Codex cache acquisition returned an invalid inventory"
        )
    return rows


def apply_codex_hook_trust(
    projects: tuple[Path, ...], updates: dict[str, dict[str, str]]
) -> None:
    if not updates:
        return
    shell = shutil.which("pwsh")
    if not shell:
        raise InstallerBoundaryError("PowerShell is required to trust Codex hooks")
    roots = tuple(
        sorted({Path(project).resolve(strict=True) for project in projects}, key=str)
    )
    if not roots:
        raise InstallerBoundaryError(
            "Codex hook trust requires selected repository roots"
        )
    if any(
        not isinstance(key, str)
        or set(value) != {"trusted_hash"}
        or not isinstance(value["trusted_hash"], str)
        for key, value in updates.items()
    ):
        raise InstallerBoundaryError("Codex hook trust updates are invalid")
    installer = protected_installer()
    payload = json.dumps(
        {"projects": [str(root) for root in roots], "updates": updates}
    )
    try:
        result = subprocess.run(
            [shell, "-NoProfile", "-File", str(installer), "-ApplyCodexHookTrust"],
            input=payload,
            text=True,
            encoding="utf-8",
            capture_output=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise InstallerBoundaryError(
            f"Codex hook trust could not run: {error}"
        ) from error
    if result.returncode:
        detail = (result.stderr or result.stdout).strip()
        raise InstallerBoundaryError(f"Codex hook trust failed: {detail}")


def apply_profile_plan(
    home: Path, documents: Path | None, updates: list[tuple[Path, str, str, bool]]
) -> None:
    if not updates:
        return
    shell = shutil.which("pwsh")
    if not shell:
        raise InstallerBoundaryError("PowerShell is required to update shell profiles")
    planned = []
    for path, text, encoding, bom in updates:
        target = lexical_absolute(Path(path))
        try:
            metadata = target.lstat()
        except FileNotFoundError:
            original = None
        except OSError as error:
            raise InstallerBoundaryError(
                f"Profile could not be read: {target}: {error}"
            ) from error
        else:
            if target.is_symlink() or metadata.st_nlink != 1:
                raise InstallerBoundaryError(
                    f"Profile is not a regular unlinked file: {target}"
                )
            try:
                original = target.read_bytes()
            except OSError as error:
                raise InstallerBoundaryError(
                    f"Profile could not be read: {target}: {error}"
                ) from error
        prefix = {
            "utf-8": b"\xef\xbb\xbf",
            "utf-16-le": b"\xff\xfe",
            "utf-16-be": b"\xfe\xff",
        }.get(encoding)
        if prefix is None:
            raise InstallerBoundaryError(f"Unsupported profile encoding: {encoding}")
        replacement = (prefix if bom else b"") + text.encode(encoding)
        planned.append(
            {
                "path": str(target),
                "expected": (
                    None if original is None else base64.b64encode(original).decode()
                ),
                "replacement": base64.b64encode(replacement).decode(),
            }
        )
    installer = protected_installer()
    payload = json.dumps(
        {
            "home": str(lexical_absolute(home)),
            "documents": (
                None if documents is None else str(lexical_absolute(documents))
            ),
            "updates": planned,
        }
    )
    environment = dict(os.environ)
    environment["HOME"] = str(lexical_absolute(home))
    try:
        result = subprocess.run(
            [shell, "-NoProfile", "-File", str(installer), "-ApplyProfilePlan"],
            input=payload,
            text=True,
            encoding="utf-8",
            capture_output=True,
            timeout=60,
            check=False,
            env=environment,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise InstallerBoundaryError(
            f"Profile update could not run: {error}"
        ) from error
    if result.returncode:
        detail = (result.stderr or result.stdout).strip()
        raise InstallerBoundaryError(f"Profile update failed: {detail}")


def apply_user_environment(variables: dict[str, str]) -> None:
    if not variables:
        return
    allowed = {"TJ_AGENTS_REAL_GIT_BASH", "CLAUDE_CODE_GIT_BASH_PATH"}
    invalid = set(variables) - allowed or any(
        not isinstance(value, str) or not value for value in variables.values()
    )
    if invalid:
        raise InstallerBoundaryError("User environment plan is invalid")
    shell = shutil.which("pwsh")
    if not shell:
        raise InstallerBoundaryError(
            "PowerShell is required to update user environment"
        )
    installer = protected_installer()
    try:
        result = subprocess.run(
            [shell, "-NoProfile", "-File", str(installer), "-ApplyUserEnvironment"],
            input=json.dumps({"variables": variables}),
            text=True,
            encoding="utf-8",
            capture_output=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise InstallerBoundaryError(
            f"User environment update could not run: {error}"
        ) from error
    if result.returncode:
        detail = (result.stderr or result.stdout).strip()
        raise InstallerBoundaryError(f"User environment update failed: {detail}")
