"""Ignore transient bytecode files while inspecting the delivered runtime."""

import importlib
from collections.abc import Iterator
from pathlib import Path

import pytest

DELIVERY = importlib.import_module("test_runtime_delivery").DELIVERY


def test_closure_ignores_disappearing_bytecode_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = tmp_path / ".agents/agent/runtime"
    runtime.mkdir(parents=True)
    source = runtime / "live.py"
    source.write_bytes(b"live")
    original = Path.rglob

    def listing(directory: Path, pattern: str) -> Iterator[Path]:
        if directory == runtime:
            yield runtime / "__pycache__/live.cpython-312.pyc.temporary"
            yield source
        else:
            yield from original(directory, pattern)

    monkeypatch.setattr(Path, "rglob", listing)
    assert DELIVERY.closure(tmp_path, [".agents/agent/runtime"]) == {
        ".agents/agent/runtime/live.py": b"live"
    }
