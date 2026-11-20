"""Run installed shell bindings against a dummy tj command."""

import json
import os
import shutil
import subprocess
from pathlib import Path

import install
import pytest


@pytest.mark.parametrize("host", ["claude", "codex"])
def test_installed_profile_forwards_plain_host_commands(
    tmp_path: Path, host: str
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
    environment = dict(os.environ)
    if context.windows:
        loader = install.profile_loader(context)
        assert loader is not None
        loader.parent.mkdir(parents=True)
        loader.write_text("# Fixture profile loader.\n", encoding="utf-8")
        shell = shutil.which("pwsh") or shutil.which("powershell.exe")
        assert shell is not None
        environment["TJ_BINDING_ARGUMENTS"] = json.dumps([host, *arguments])
        environment["TJ_BINDING_PROFILE"] = str(install.profile_paths(context)[0])
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
        dummy.write_text(
            '#!/bin/sh\nprintf "%s\\0" "$@"\nexit 23\n', encoding="utf-8"
        )
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
        env=environment,
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
