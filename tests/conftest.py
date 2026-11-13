import os
import sys
from collections.abc import Iterator
from pathlib import Path

import owned_launch
import plugin_update
import pytest
from fake_machine import MARKETPLACE_REVISION


@pytest.fixture(autouse=True)
def no_owning_launch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(owned_launch, "current", lambda: None)


@pytest.fixture(autouse=True)
def marketplace_at_a_known_commit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        plugin_update, "marketplace_revision", lambda *_arguments: MARKETPLACE_REVISION
    )


@pytest.fixture
def loader_package_roots(tmp_path: Path) -> tuple[Path, Path]:
    outer = tmp_path / "outer"
    (outer / ".git").mkdir(parents=True)
    inner = outer / "plugins/agent"
    marker = inner / ".codex-plugin/plugin.json"
    marker.parent.mkdir(parents=True)
    marker.write_text("{}", encoding="utf-8")
    return outer, inner


@pytest.fixture(autouse=True)
def preserve_empty_process_environment() -> Iterator[None]:
    # Branch before the test runs: its sys.platform patch may outlive this teardown.
    if sys.platform != "win32":
        yield
    else:
        empty = [key for key, value in os.environ.items() if value == ""]
        yield
        if empty:
            import ctypes

            # Environment mocks can restore Python's map but delete native empty values.
            set_variable = ctypes.WinDLL(
                "kernel32", use_last_error=True
            ).SetEnvironmentVariableW
            set_variable.argtypes = (ctypes.c_wchar_p, ctypes.c_wchar_p)
            set_variable.restype = ctypes.c_int
            for key in empty:
                if os.environ.get(key) == "" and not set_variable(key, ""):
                    raise ctypes.WinError(ctypes.get_last_error())
