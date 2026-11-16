"""Fresh GitHub marketplace heads using the caller's configured Git credentials."""

from __future__ import annotations

import base64
import json
import re
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from http.client import HTTPException
from pathlib import Path
from typing import override

from tj_cli import process

TIMEOUT_SECONDS = 5
REVISION = re.compile(r"[0-9a-f]{40}")
REPOSITORY = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9_.-]+")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    @override
    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: object,
        code: int,
        msg: str,
        headers: object,
        newurl: str,
    ) -> None:
        return None


def repository(url: str) -> str:
    """Accept GitHub HTTPS, ssh:// and scp-style origins; ignore URL userinfo."""
    scp = re.fullmatch(r"(?:[^/@:\s]+@)?github\.com:([^?#\s]+)", url, re.IGNORECASE)
    if scp is not None:
        path = scp[1]
    else:
        parsed = urllib.parse.urlsplit(url)
        if (
            parsed.scheme not in {"https", "ssh"}
            or parsed.hostname != "github.com"
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("Marketplace origin must use github.com")
        path = parsed.path.removeprefix("/")
    path = path.removesuffix("/").removesuffix(".git")
    if not REPOSITORY.fullmatch(path) or path.rpartition("/")[2] in {".", ".."}:
        raise ValueError("Marketplace origin has no valid GitHub repository")
    return path


def _git(
    root: Path, environ: Mapping[str, str], *arguments: str, contents: str | None = None
) -> subprocess.CompletedProcess[str]:
    return process.run(
        ["git", "-C", str(root), *arguments],
        env={**environ, "GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "Never"},
        input=contents,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=TIMEOUT_SECONDS,
        check=False,
    )


def behind(root: Path, environ: Mapping[str, str]) -> bool:
    """Unknown or failed freshness checks require the native marketplace refresh."""
    try:
        local = _git(root, environ, "rev-parse", "HEAD")
        if local.returncode or not REVISION.fullmatch(local.stdout.strip()):
            return True
        origin = _git(root, environ, "remote", "get-url", "origin")
        if origin.returncode:
            return True
        source = repository(origin.stdout.strip())
        credential = _git(
            root,
            environ,
            "credential",
            "fill",
            contents=f"protocol=https\nhost=github.com\npath={source}.git\n\n",
        )
        fields = dict(
            line.split("=", 1) for line in credential.stdout.splitlines() if "=" in line
        )
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "tj-agent",
            "Cache-Control": "no-cache",
        }
        if fields:
            if (
                credential.returncode
                or not fields.get("username")
                or not fields.get("password")
            ):
                return True
            encoded = base64.b64encode(
                f"{fields['username']}:{fields['password']}".encode()
            ).decode("ascii")
            headers["Authorization"] = f"Basic {encoded}"
        request = urllib.request.Request(
            f"https://api.github.com/repos/{source}/commits/HEAD", headers=headers
        )
        opener = urllib.request.build_opener(_NoRedirect())
        with opener.open(request, timeout=TIMEOUT_SECONDS) as response:
            document = json.load(response)
        revision = document.get("sha") if isinstance(document, dict) else None
        return not (
            isinstance(revision, str)
            and REVISION.fullmatch(revision)
            and revision == local.stdout.strip()
        )
    except urllib.error.HTTPError as error:
        error.close()
        return True
    except (OSError, ValueError, HTTPException, subprocess.SubprocessError):
        # Credential output and request exceptions can contain secrets. Unknown is
        # deliberately handled by the existing native update, without diagnostics.
        return True
