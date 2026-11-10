import os
import re
import subprocess
from collections.abc import Iterable, Iterator
from pathlib import Path

import install
import pytest

SCRIPT_REFERENCE = re.compile(
    r"'([^'\n]+?\.(?:py|ps1|sh))'"
    r"|\"([^\"\n]+?\.(?:py|ps1|sh))\""
    r"|([^\s'\"`;|&<>()]+\.(?:py|ps1|sh))\b"
)
SHELL_SCRIPTS = {".ps1", ".sh"}


def installed_machine(
    home: Path, monkeypatch: pytest.MonkeyPatch, windows: bool
) -> install.Context:
    monkeypatch.setattr(
        install.agent_store, "executable", lambda: home / "native-bin" / "agent-store"
    )
    home.mkdir()
    for name in (".bashrc", ".zshrc"):
        (home / name).touch()
    context = install.Context(
        home=home, environ={}, windows=windows, documents=home / "Documents"
    )
    install.apply_runtime(context)
    install.apply_lifecycle_commands(context)
    install.apply_profile(context)
    return context


def written_artefacts(context: install.Context) -> list[Path]:
    runtime = install.runtime_bin(context)
    payload = {
        runtime / name for name in install.runtime_receipt(context.root)["files"]
    }
    return [
        path
        for path in sorted(context.home.rglob("*"))
        if path.is_file() and path not in payload
    ]


def script_references(text: str) -> Iterator[str]:
    for match in SCRIPT_REFERENCE.finditer(text):
        yield next(group for group in match.groups() if group)


def missing_scripts(artefacts: Iterable[Path]) -> tuple[list[str], set[Path]]:
    pending = list(artefacts)
    visited: set[Path] = set()
    missing = []
    while pending:
        artefact = pending.pop()
        if artefact in visited:
            continue
        visited.add(artefact)
        for reference in script_references(
            artefact.read_text(encoding="utf-8-sig", errors="replace")
        ):
            target = artefact.parent / reference
            if not target.is_file():
                missing.append(f"{artefact}: {reference}")
            elif target.suffix in SHELL_SCRIPTS:
                pending.append(target)
    return missing, visited


@pytest.mark.parametrize("windows", [True, False])
def test_install_artefacts_reference_only_scripts_the_package_installs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, windows: bool
) -> None:
    context = installed_machine(tmp_path / "home", monkeypatch, windows)
    artefacts = written_artefacts(context)
    missing, visited = missing_scripts(artefacts)
    assert missing == []
    assert set(install.lifecycle_shims(context)) <= set(artefacts)
    assert any(
        path.parent == install.runtime_bin(context) / ".agents/agent/resources/shell"
        for path in visited
    )


def test_install_artefacts_report_a_wrapper_naming_a_deleted_runtime_module(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    context = installed_machine(tmp_path / "home", monkeypatch, windows=True)
    launcher = (
        install.runtime_bin(context) / ".agents/agent/runtime/retired_launcher.py"
    )
    profile = install.profile_paths(context)[0]
    profile.write_text(
        f"function global:claude {{ & 'python' -B '{launcher}' @args }}\n",
        encoding="utf-8",
    )
    missing, _ = missing_scripts(written_artefacts(context))
    assert missing == [f"{profile}: {launcher}"]


@pytest.mark.parametrize(
    ("command", "arguments"),
    [("plugins", ["update", "--help"]), ("install", ["--help"])],
)
def test_installed_commands_resolve_the_delivered_payload(
    command: str, arguments: list[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    context = installed_machine(tmp_path / "home", monkeypatch, windows=os.name == "nt")
    suffix = ".cmd" if context.windows else ""
    shim = next(
        path
        for path in install.lifecycle_shims(context)
        if path.name == f"tj-{command}{suffix}"
    )
    invocation = [str(shim), *arguments]
    if context.windows:
        invocation = ["cmd.exe", "/d", "/c", *invocation]
    result = subprocess.run(
        invocation, capture_output=True, text=True, timeout=15, check=False
    )
    assert result.returncode == 0, result.stderr
    assert "usage:" in result.stdout
