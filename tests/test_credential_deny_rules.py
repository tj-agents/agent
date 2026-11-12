import json

import host_permissions
import pytest


@pytest.mark.parametrize("mode", ["standard", "bypass"])
@pytest.mark.parametrize("project", [False, True])
def test_permission_projection_preserves_user_denials_without_injecting_auth_restrictions(
    mode: str, project: bool
) -> None:
    original = {"permissions": {"deny": ["Read(.env)"], "allow": ["Bash(git status)"]}}
    rendered = host_permissions.claude_settings(json.dumps(original), mode, project=project)
    permissions = json.loads(rendered)["permissions"]
    assert permissions["deny"] == ["Read(.env)"]
    assert permissions["allow"] == ["Bash(git status)"]
    assert host_permissions.claude_settings(rendered, mode, project=project) == rendered


def test_project_permission_projection_adds_no_credential_denials() -> None:
    rendered = host_permissions.claude_settings("{}", "standard", project=True)
    assert "deny" not in json.loads(rendered)["permissions"]
