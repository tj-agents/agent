import os
import re
import shutil
import subprocess
import sys
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
    home: Path,
    monkeypatch: pytest.MonkeyPatch,
    windows: bool,
    root: Path = install.ROOT,
) -> install.Context:
    monkeypatch.setattr(
        install.agent_store, "executable", lambda: home / "native-bin" / "agent-store"
    )
    home.mkdir()
    for name in (".bashrc", ".zshrc"):
        (home / name).touch()
    context = install.Context(
        home=home, environ={}, windows=windows, documents=home / "Documents", root=root
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


def test_installed_commands_survive_removing_the_author_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "author checkout"
    plugin = source / "plugins" / "agent"
    for export in install.RUNTIME_EXPORTS:
        shutil.copytree(install.ROOT / export, plugin / export)
    for name in ("install.ps1", "install.sh"):
        shutil.copy2(Path(__file__).resolve().parents[1] / name, source / name)
    monkeypatch.delenv("PYTHONPATH", raising=False)
    monkeypatch.delenv("PYTHONHOME", raising=False)
    monkeypatch.setenv("PYTHONNOUSERSITE", "1")
    monkeypatch.setenv(
        "PATH", str(Path(sys._base_executable).resolve().parent), prepend=os.pathsep
    )
    if os.name == "nt":
        shell = shutil.which("pwsh")
        assert shell is not None
        bootstrap = [shell, "-NoProfile", "-File", str(source / "install.ps1")]
    else:
        bootstrap = ["bash", str(source / "install.sh")]
    result = subprocess.run(
        [*bootstrap, "--help"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "usage: tj install" in result.stdout
    author_python = source / ".venv" / "Scripts" / "python.exe"
    author_python.parent.mkdir(parents=True)
    author_python.touch()
    monkeypatch.setattr(install.sys, "executable", str(author_python))
    context = installed_machine(
        tmp_path / "home", monkeypatch, windows=os.name == "nt", root=plugin
    )
    shims = install.lifecycle_shims(context)
    shutil.rmtree(source)
    for shim in shims:
        command = shim.stem.removeprefix("tj-")
        arguments = ["update", "--help"] if command == "plugins" else ["--help"]
        invocation = [str(shim), *arguments]
        if context.windows:
            invocation = ["cmd.exe", "/d", "/c", *invocation]
        result = subprocess.run(
            invocation,
            cwd=tmp_path,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        assert result.returncode == 0, f"{shim}: {result.stderr}"
        assert "usage:" in result.stdout


@pytest.mark.parametrize("windows", [True, False])
def test_all_installed_bindings_use_the_base_interpreter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, windows: bool
) -> None:
    author_python = tmp_path / "author" / ".venv" / "python"
    monkeypatch.setattr(install.sys, "executable", str(author_python))
    context = installed_machine(tmp_path / "home", monkeypatch, windows)
    for shim in install.lifecycle_shims(context):
        text = shim.read_text(encoding="utf-8")
        assert str(Path(sys._base_executable).resolve()) in text
        assert str(author_python) not in text
    if not windows:
        alias = install.runtime_bin(context) / "python"
        text = alias.read_text(encoding="utf-8")
        assert install.managed_python_alias(text) == str(
            Path(sys._base_executable).resolve()
        )
        assert str(author_python) not in text


@pytest.mark.parametrize("base", [None, "", "relative-python"])
def test_console_python_rejects_an_unresolved_base(
    base: str | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(install.sys, "_base_executable", base)
    with pytest.raises(install.InstallError, match="absolute base Python"):
        install.console_python()


@pytest.mark.parametrize("directory", [True, False])
def test_console_python_rejects_an_unavailable_executable(
    directory: bool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = tmp_path / "python.exe"
    if directory:
        base.mkdir()
    monkeypatch.setattr(install.sys, "_base_executable", str(base))
    with pytest.raises(
        install.InstallError, match="base Python interpreter is unavailable"
    ):
        install.console_python()


def test_console_python_rejects_an_unsupported_python(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(install.sys, "version_info", (3, 11))
    with pytest.raises(install.InstallError, match=r"Python 3\.12 or newer"):
        install.console_python()


def test_console_python_selects_the_console_base_executable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    windowed = tmp_path / "pythonw.exe"
    windowed.touch()
    console = windowed.with_name("python.exe")
    console.touch()
    monkeypatch.setattr(install.sys, "_base_executable", str(windowed))
    assert install.console_python() == str(console)
    console.unlink()
    with pytest.raises(
        install.InstallError, match="base Python interpreter is unavailable"
    ):
        install.console_python()
