import json
import tomllib
from pathlib import Path

import host_permissions
import pytest


@pytest.mark.parametrize(
    "document",
    [None, {}, {"configuration": {}}, {"configuration": {"core": {}}}],
)
def test_permissions_omission_leaves_the_native_policy_in_control(
    tmp_path: Path, document: dict[str, object] | None
) -> None:
    if document is not None:
        settings = tmp_path / ".agents/settings.json"
        settings.parent.mkdir()
        settings.write_text(json.dumps(document), encoding="utf-8")

    assert host_permissions.permissions(tmp_path) is None


@pytest.mark.parametrize("mode", ["bypass", "standard"])
def test_permissions_keeps_an_explicit_project_choice(
    tmp_path: Path, mode: str
) -> None:
    settings = tmp_path / ".agents/settings.json"
    settings.parent.mkdir()
    settings.write_text(
        json.dumps({"configuration": {"core": {"permissions": mode}}}),
        encoding="utf-8",
    )

    assert host_permissions.permissions(tmp_path) == mode


@pytest.mark.parametrize("mode", [None, False, "native", "", "typo"])
def test_permissions_rejects_invalid_explicit_values(
    tmp_path: Path, mode: object
) -> None:
    settings = tmp_path / ".agents/settings.json"
    settings.parent.mkdir()
    settings.write_text(
        json.dumps({"configuration": {"core": {"permissions": mode}}}),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="core permissions must be bypass or standard"):
        host_permissions.permissions(tmp_path)


@pytest.mark.parametrize("project", [False, True])
@pytest.mark.parametrize(
    "text",
    [
        "",
        "{}\n",
        '{"permissions":{"defaultMode":"plan",'
        '"disableBypassPermissionsMode":"disable","deny":["Bash(custom)"]},'
        '"apiKeyHelper":"native-auth-helper"}\n',
    ],
)
def test_claude_settings_without_a_choice_preserves_the_native_document(
    text: str, project: bool
) -> None:
    assert host_permissions.claude_settings(text, None, project=project) == text


@pytest.mark.parametrize(
    "text",
    [
        "",
        '# Native settings\napproval_policy = "untrusted"\n'
        'sandbox_mode = "read-only"\ncli_auth_credentials_store = "keyring"\n',
        'default_permissions = ":danger-full-access"\n'
        '[profiles.work]\napproval_policy = "on-request"\n',
    ],
)
def test_codex_settings_without_a_choice_preserves_the_native_document(
    text: str,
) -> None:
    assert host_permissions.codex_settings(text, None) == text


@pytest.mark.parametrize(
    ("mode", "claude_mode", "codex_policy", "codex_permissions"),
    [
        ("bypass", "bypassPermissions", "never", ":danger-full-access"),
        ("standard", "default", "on-request", ":workspace"),
    ],
)
def test_explicit_permission_modes_preserve_native_auth_settings(
    mode: str, claude_mode: str, codex_policy: str, codex_permissions: str
) -> None:
    claude = json.loads(
        host_permissions.claude_settings('{"apiKeyHelper":"native-auth-helper"}', mode)
    )
    codex = tomllib.loads(
        host_permissions.codex_settings(
            'cli_auth_credentials_store = "keyring"\n', mode
        )
    )

    assert claude["apiKeyHelper"] == "native-auth-helper"
    assert claude["permissions"]["defaultMode"] == claude_mode
    assert codex["cli_auth_credentials_store"] == "keyring"
    assert codex["approval_policy"] == codex_policy
    assert codex["default_permissions"] == codex_permissions
