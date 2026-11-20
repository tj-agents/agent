"""Run installed shell bindings against a dummy tj command."""

import json
import os
import shutil
import subprocess
from pathlib import Path

import install
import pytest
from tj_agent.host import Host


def test_installer_ignores_its_own_forwarders_when_only_one_provider_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    native = tmp_path / "providers"
    native.mkdir()
    windows = os.name == "nt"
    context = install.Context(home=tmp_path / "home", environ={}, windows=windows)
    bindings = install.runtime_bin(context)
    bindings.mkdir(parents=True)
    shim_suffix = ".cmd" if windows else ""
    for host in Host:
        shim = bindings / (host.value + shim_suffix)
        shim.touch()
        shim.chmod(0o755)
    installed = native / ("codex.exe" if windows else "codex")
    installed.touch()
    installed.chmod(0o755)
    path = os.pathsep.join((str(bindings), str(native)))
    monkeypatch.setenv("PATH", path)
    context.environ["PATH"] = path

    assert {
        host: Path(executable)
        for host, executable in install.plugin_setup_executables(context).items()
    } == {Host.CODEX: installed}
    assert install.plugin_hosts(context) == [Host.CODEX]


@pytest.mark.parametrize("host", ["claude", "codex"])
def test_installed_profile_forwards_plain_host_commands(
    tmp_path: Path, host: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    context = install.Context(
        home=tmp_path / "home with spaces",
        environ={},
        windows=os.name == "nt",
        documents=tmp_path / "documents",
    )
    runtime = install.runtime_bin(context)
    runtime.mkdir(parents=True)
    arguments = ["--model", "native model", "", "literal & | ' \" $() % café"]
    if context.windows:
        loader = install.profile_loader(context)
        assert loader is not None
        loader.parent.mkdir(parents=True)
        loader.write_text("# Fixture profile loader.\n", encoding="utf-8")
        shell = shutil.which("pwsh") or shutil.which("powershell.exe")
        assert shell is not None
        monkeypatch.setenv("TJ_BINDING_ARGUMENTS", json.dumps([host, *arguments]))
        monkeypatch.setenv("TJ_BINDING_PROFILE", str(install.profile_paths(context)[0]))
        script = tmp_path / "invoke.ps1"
        script.write_text(
            "$ErrorActionPreference = 'Stop'\n"
            "[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)\n"
            "function global:tj {\n"
            "  ConvertTo-Json -Compress -InputObject @($args)\n"
            "  $global:LASTEXITCODE = 23\n"
            "}\n"
            ". $env:TJ_BINDING_PROFILE\n"
            "$forwarded = @($env:TJ_BINDING_ARGUMENTS | ConvertFrom-Json)\n"
            "$hostCommand = $forwarded[0]\n"
            "$hostArguments = $forwarded[1..($forwarded.Count - 1)]\n"
            "& $hostCommand @hostArguments\n"
            "exit $LASTEXITCODE\n",
            encoding="utf-8",
        )
        command = [shell, "-NoProfile", "-File", str(script)]
    else:
        dummy = runtime / "tj"
        dummy.write_text('#!/bin/sh\nprintf "%s\\0" "$@"\nexit 23\n', encoding="utf-8")
        dummy.chmod(0o755)
        shell = shutil.which("bash")
        assert shell is not None
        command = [
            shell,
            "--noprofile",
            "--norc",
            "-c",
            '. "$1"; shift; "$@"',
            "fixture",
            str(context.home / ".bashrc"),
            host,
            *arguments,
        ]
    install.apply_profile(context)
    result = subprocess.run(
        command,
        cwd=tmp_path,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
        timeout=15,
    )
    assert result.returncode == 23, result.stderr
    forwarded = (
        json.loads(result.stdout)
        if context.windows
        else result.stdout.removesuffix("\0").split("\0")
    )
    assert forwarded == [host, *arguments]
    assert install.check_profile(context) == []
