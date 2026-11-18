import base64
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import installer_boundary as boundary
import pytest


def package(root: Path, contents: bytes = b"installer") -> Path:
    (root / "install.ps1").write_bytes(contents)
    resource = root / boundary.GENERATED_RESOURCE
    resource.parent.mkdir(parents=True)
    resource.write_bytes(contents)
    return resource


def test_verifies_the_exact_packaged_root_installer(tmp_path: Path) -> None:
    resource = package(tmp_path)
    assert boundary.verify_root_installer(tmp_path) == resource


def test_rejects_a_packaged_installer_with_different_bytes(tmp_path: Path) -> None:
    package(tmp_path)
    (tmp_path / boundary.GENERATED_RESOURCE).write_bytes(b"different")
    with pytest.raises(boundary.InstallerBoundaryError, match="does not match"):
        boundary.verify_root_installer(tmp_path)


def test_rejects_a_linked_packaged_installer(tmp_path: Path) -> None:
    package(tmp_path)
    resource = tmp_path / boundary.GENERATED_RESOURCE
    resource.unlink()
    outside = tmp_path / "outside.ps1"
    outside.write_bytes(b"installer")
    try:
        resource.symlink_to(outside)
    except OSError as error:
        pytest.skip(str(error))
    with pytest.raises(boundary.InstallerBoundaryError, match="regular unlinked"):
        boundary.verify_root_installer(tmp_path)


def test_profile_plan_rejects_a_link_before_reading_it(tmp_path: Path) -> None:
    profile = tmp_path / "profile.ps1"
    outside = tmp_path / "outside.ps1"
    outside.write_text("outside", encoding="utf-8")
    try:
        profile.symlink_to(outside)
    except OSError as error:
        pytest.skip(str(error))
    with pytest.raises(boundary.InstallerBoundaryError, match="regular unlinked"):
        boundary.apply_profile_plan(
            tmp_path, None, [(profile, "replacement", "utf-8", False)]
        )


def run_profile_plan(
    tmp_path: Path, updates: list[dict[str, str | None]]
) -> subprocess.CompletedProcess[str]:
    shell = shutil.which("pwsh")
    if shell is None:
        pytest.skip("PowerShell is unavailable")
    resource = boundary.installer_resource(Path(boundary.__file__).resolve().parents[3])
    return subprocess.run(
        [shell, "-NoProfile", "-File", str(resource), "-ApplyProfilePlan"],
        input=json.dumps(
            {"home": str(tmp_path), "documents": None, "updates": updates}
        ),
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=False,
    )


def update(path: Path, expected: bytes, replacement: bytes) -> dict[str, str | None]:
    return {
        "path": str(path),
        "expected": base64.b64encode(expected).decode(),
        "replacement": base64.b64encode(replacement).decode(),
    }


def test_profile_executor_rejects_changed_expected_bytes(tmp_path: Path) -> None:
    profile = tmp_path / ".bashrc"
    profile.write_bytes(b"changed")

    result = run_profile_plan(tmp_path, [update(profile, b"expected", b"replacement")])

    assert result.returncode != 0
    assert profile.read_bytes() == b"changed"


def test_profile_executor_rejects_hardlinked_profile(tmp_path: Path) -> None:
    profile = tmp_path / ".bashrc"
    outside = tmp_path / "outside"
    outside.write_bytes(b"current")
    try:
        os.link(outside, profile)
    except OSError as error:
        pytest.skip(str(error))

    result = run_profile_plan(tmp_path, [update(profile, b"current", b"replacement")])

    assert result.returncode != 0
    assert outside.read_bytes() == b"current"


def test_hook_trust_rejects_stale_hash_before_config_write(tmp_path: Path) -> None:
    shell = shutil.which("pwsh")
    if shell is None:
        pytest.skip("PowerShell is unavailable")
    project = tmp_path / "project"
    project.mkdir()
    subprocess.run(["git", "init", "--quiet", str(project)], check=True)
    selection = project / ".agents" / "plugins.json"
    selection.parent.mkdir()
    selection.write_text(json.dumps({"plugins": ["agent@tj-agents"]}), encoding="utf-8")
    bin_directory = tmp_path / "bin"
    bin_directory.mkdir()
    requests = tmp_path / "requests.jsonl"
    server = bin_directory / "server.py"
    server.write_text(
        """import json
import pathlib
import sys

output = pathlib.Path(sys.argv[1])
for line in sys.stdin:
    request = json.loads(line)
    method = request.get("method")
    with output.open("a", encoding="utf-8") as stream:
        stream.write(method + "\\n")
    if "id" not in request:
        continue
    if method == "hooks/list":
        result = {
            "data": [
                {
                    "hooks": [
                        {
                            "key": "agent@tj-agents:hooks/codex.json:stop:0:0",
                            "currentHash": "sha256:stale",
                            "trustStatus": "untrusted",
                        }
                    ]
                }
            ]
        }
    else:
        result = {}
    print(json.dumps({"id": request["id"], "result": result}), flush=True)
""",
        encoding="utf-8",
    )
    command = bin_directory / "codex.cmd"
    command.write_text(
        f'@"{sys.executable}" -u "{server}" "{requests}"\r\n', encoding="utf-8"
    )
    environment = dict(os.environ)
    environment["PATH"] = str(bin_directory) + os.pathsep + environment["PATH"]
    resource = boundary.installer_resource(Path(boundary.__file__).resolve().parents[3])
    payload = {
        "projects": [str(project)],
        "updates": {
            "agent@tj-agents:hooks/codex.json:stop:0:0": {
                "trusted_hash": "sha256:expected"
            }
        },
    }

    result = subprocess.run(
        [shell, "-NoProfile", "-File", str(resource), "-ApplyCodexHookTrust"],
        input=json.dumps(payload),
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=False,
        env=environment,
    )

    assert result.returncode != 0
    assert requests.read_text(encoding="utf-8").splitlines() == [
        "initialize",
        "initialized",
        "hooks/list",
    ]
