"""The per-host native session contract and its dispatch table (Python 3.9+)."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Protocol

from claude import sessions as claude_sessions
from codex import sessions as codex_sessions
from tj_agent.host import Host


class Sessions(Protocol):
    HOST: Host

    def history_roots(self, root: Path) -> tuple[Path, ...]: ...

    def transcript_files(self, directory: Path) -> Iterable[Path]: ...

    def transcript_ids(self, path: Path) -> tuple[str, ...]: ...

    def read_session(self, path: Path) -> dict | None: ...

    def header(self, path: Path) -> dict | None: ...

    def resume_command(self, session_id: str) -> str: ...


SESSIONS: Mapping[Host, Sessions] = {Host.CLAUDE: claude_sessions, Host.CODEX: codex_sessions}
