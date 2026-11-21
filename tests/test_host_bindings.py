"""Exercise installed shell bindings against TJ fixtures without starting an agent."""

import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import agent_store
import install
import pytest
from claude import launch as claude_launch


@pytest.mark.parametrize("windows", [True, False])
def test_plain_bindings_preserve_provider_commands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, windows: bool
) -> None:
    context = install.Context(home=tmp_path, environ={}, windows=windows)
    native_bin = tmp_path / "native-bin"
    native_bin.mkdir()
    providers = [
        native_bin / (host + (".cmd" if windows else ""))
        for host in ("claude", "codex")
    ]
    for path in providers:
        path.write_bytes(b"provider-owned command\n")
    monkeypatch.setattr(agent_store, "executable", lambda: native_bin / "agent-store")
    install.apply_lifecycle_commands(context)
    assert install.check_lifecycle_commands(context) == []
    for path in providers:
        assert path.read_bytes() == b"provider-owned command\n"
        binding = install.runtime_bin(context) / path.name
        assert str(install.runtime_bin(context)) in binding.read_text(encoding="utf-8")


@pytest.mark.skipif(os.name == "nt", reason="POSIX executable resolution")
def test_claude_resolver_skips_the_managed_path_binding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    managed = tmp_path / "managed"
    provider = tmp_path / "provider"
    managed.mkdir()
    provider.mkdir()
    shim = managed / "claude"
    native = provider / "claude"
    shim.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    native.write_bytes(b"\x7fELFfixture")
    shim.chmod(0o755)
    native.chmod(0o755)
    monkeypatch.setenv("PATH", os.pathsep.join((str(managed), str(provider))))
    assert claude_launch.resolve_executable(home=tmp_path) == str(native)


@pytest.mark.parametrize("host", ["claude", "codex"])
def test_powershell_plain_commands_preserve_native_arguments(
    tmp_path: Path, host: str
) -> None:
    shell = shutil.which("pwsh") or shutil.which("powershell.exe")
    if shell is None:
        pytest.skip("PowerShell is unavailable")
    script = tmp_path / "profile-test.ps1"
    script.write_text(
        "[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)\n"
        "function tj { ConvertTo-Json -InputObject @($args) -Compress }\n"
        + install.powershell_block(None, "\n")
        + f"\n{host} --resume 'session-id' 'prompt & café'\n",
        encoding="utf-8-sig",
    )
    result = subprocess.run(
        [shell, "-NoProfile", "-NonInteractive", "-File", str(script)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=15,
        check=True,
    )
    assert json.loads(result.stdout) == [
        host,
        "--resume",
        "session-id",
        "prompt & café",
    ]


@pytest.mark.skipif(os.name == "nt", reason="POSIX shell command resolution")
@pytest.mark.parametrize("host", ["claude", "codex"])
def test_posix_plain_commands_preserve_native_arguments(
    tmp_path: Path, host: str
) -> None:
    dummy = tmp_path / "tj"
    dummy.write_text(
        f"#!{sys.executable}\nimport json, sys\nprint(json.dumps(sys.argv[1:]))\n",
        encoding="utf-8",
    )
    dummy.chmod(0o755)
    profile = tmp_path / "profile"
    profile.write_text(install.posix_block(tmp_path), encoding="utf-8")
    result = subprocess.run(
        [
            "sh",
            "-c",
            f'. {shlex.quote(str(profile))}\n{host} "$@"',
            "test",
            "--resume",
            "session-id",
            "prompt & café",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=15,
        check=True,
    )
    assert json.loads(result.stdout) == [
        host,
        "--resume",
        "session-id",
        "prompt & café",
    ]
