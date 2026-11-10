import os
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from claude import sessions as claude_sessions
from codex import sessions as codex_sessions


def test_claude_transcript_lookup_uses_session_identity_and_configured_home(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    project = tmp_path / "projects" / "native-encoded-project"
    project.mkdir(parents=True)
    selected = project / "exact-session.jsonl"
    selected.write_text("{}\n", encoding="utf-8")
    unrelated = project / "other.jsonl"
    unrelated.write_text("{}\n", encoding="utf-8")
    os.utime(selected, (101, 101))
    assert claude_sessions.transcript_modified("exact-session") == 101
    with pytest.raises(FileNotFoundError, match="not found"):
        claude_sessions.transcript_modified("unknown")


@pytest.mark.parametrize(
    "file_modified,index_modified,expected", [(99, 101, 101), (102, 99, 102)]
)
def test_codex_native_index_locates_transcript_and_includes_paginated_history(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    file_modified: float,
    index_modified: int,
    expected: float,
) -> None:
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    transcript = tmp_path / "host-recorded-name.jsonl"
    transcript.write_text("{}\n", encoding="utf-8")
    os.utime(transcript, (file_modified, file_modified))
    with closing(sqlite3.connect(tmp_path / "state_5.sqlite")) as database:
        database.execute(
            "CREATE TABLE threads (id TEXT, rollout_path TEXT, updated_at INTEGER)"
        )
        database.execute(
            "INSERT INTO threads VALUES (?, ?, ?)",
            ("thread", str(transcript), index_modified),
        )
        database.commit()
    before = {path.name: path.read_bytes() for path in tmp_path.iterdir()}
    assert codex_sessions.transcript_modified("thread") == expected
    assert {path.name: path.read_bytes() for path in tmp_path.iterdir()} == before
    with pytest.raises(FileNotFoundError, match="not found"):
        codex_sessions.transcript_modified("missing")


def test_codex_missing_index_never_guesses_a_transcript_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    with pytest.raises(FileNotFoundError, match="index not found"):
        codex_sessions.transcript_modified("missing")
