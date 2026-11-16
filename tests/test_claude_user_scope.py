import json
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import install
import plugin_update
import pytest
import required_plugins
from claude import plugins
from fake_machine import write_json
from tj_agent.host import Host
from tj_cli import process
from tree_digest import Digests


@dataclass
class NativeScope:
    home: Path
    project: Path
    fail_install: bool = False

    def enabled(self) -> dict[str, bool]:
        enabled: dict[str, bool] = json.loads(
            (self.home / "settings.json").read_text(encoding="utf-8")
        )["enabledPlugins"]
        return enabled

    def run(
        self, command: Sequence[str], **_options: object
    ) -> subprocess.CompletedProcess[str]:
        offset = command.index("plugin")
        operation, identity = command[offset + 1 : offset + 3]
        enabled = self.enabled()
        if operation == "install":
            enabled.update(
                {identity: True, "base@tj-agents": True, "fresh@other": True}
            )
            write_json(self.home / "settings.json", {"enabledPlugins": enabled})
            if self.fail_install:
                return subprocess.CompletedProcess(command, 1, "", "install failed")
            write_json(
                self.home
                / "plugins/cache/tj-agents/python/pin/.claude-plugin/plugin.json",
                {"name": "python"},
            )
        elif operation == "disable":
            if "-y" in command:
                return subprocess.CompletedProcess(command, 1, "", "unknown option -y")
            user_only = list(command[:offset]) == [
                "claude",
                "--setting-sources",
                "user",
            ]
            effective = dict(enabled)
            if not user_only:
                for source in ("settings.json", "settings.local.json"):
                    effective.update(
                        json.loads(
                            (self.project / ".claude" / source).read_text(
                                encoding="utf-8"
                            )
                        )["enabledPlugins"]
                    )
            dependencies = {
                "python@tj-agents": ("base@tj-agents", "fresh@other"),
                "selected@tj-agents": ("base@tj-agents",),
            }
            if any(
                effective.get(owner) is True and identity in requires
                for owner, requires in dependencies.items()
            ):
                return subprocess.CompletedProcess(
                    command, 1, "", "still required by an enabled plugin"
                )
            enabled[identity] = False
            write_json(self.home / "settings.json", {"enabledPlugins": enabled})
        return subprocess.CompletedProcess(command, 0, "{}", "")


@pytest.fixture
def native_scope(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> NativeScope:
    home, project = tmp_path / "user", tmp_path / "project"
    for source in ("settings.json", "settings.local.json"):
        write_json(
            project / ".claude" / source,
            {"enabledPlugins": {"selected@tj-agents": True}},
        )
    write_json(home / "settings.json", {"enabledPlugins": {"kept@other": True}})
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home))
    monkeypatch.chdir(project)
    native = NativeScope(home, project)
    monkeypatch.setattr(process, "run", native.run)
    return native


def test_installer_scope_cleanup_ignores_project_and_local_dependents(
    native_scope: NativeScope,
) -> None:
    native = native_scope
    write_json(
        native.home / "settings.json", {"enabledPlugins": {"base@tj-agents": True}}
    )
    context = install.Context(
        home=native.home.parent,
        environ={
            "CLAUDE_CONFIG_DIR": str(native.home),
            "CODEX_HOME": str(native.home / "codex"),
        },
        windows=False,
        run=native.run,
    )
    install.apply_plugin_scope(context)
    assert not any(native.enabled().values())
    for source in ("settings.json", "settings.local.json"):
        assert json.loads(
            (native.project / ".claude" / source).read_text(encoding="utf-8")
        )["enabledPlugins"] == {"selected@tj-agents": True}


@pytest.mark.parametrize("fail_install", [False, True])
@pytest.mark.parametrize("identity", ["python@tj-agents", "kept@other"])
def test_setup_cleans_install_enables_and_dependencies_even_after_failure(
    native_scope: NativeScope, fail_install: bool, identity: str
) -> None:
    native = native_scope
    native.fail_install = fail_install
    if fail_install:
        with pytest.raises(
            required_plugins.RequiredPluginsError, match="install failed"
        ):
            plugins.install(native.project, [identity])
    else:
        plugins.install(native.project, [identity])
    assert {identity for identity, enabled in native.enabled().items() if enabled} == {
        "kept@other"
    }


@pytest.mark.parametrize("fail_install", [False, True])
def test_refresh_cleans_torn_reinstall_enables_even_after_failure(
    native_scope: NativeScope, fail_install: bool
) -> None:
    native = native_scope
    native.fail_install = fail_install
    write_json(
        native.home / "settings.json",
        {"enabledPlugins": {"kept@other": True, "python@tj-agents": True}},
    )
    write_json(
        native.home / "plugins/marketplaces/tj-agents/.claude-plugin/marketplace.json",
        {"plugins": [{"name": "python", "source": {"sha": "a" * 40}}]},
    )
    write_json(
        native.home / "plugins/installed_plugins.json",
        {
            "plugins": {
                "python@tj-agents": [
                    {
                        "scope": "user",
                        "gitCommitSha": "a" * 40,
                        "installPath": str(
                            native.home / "plugins/cache/tj-agents/python/pin"
                        ),
                    }
                ]
            }
        },
    )
    outcome = plugin_update.update(Host.CLAUDE, Digests(native.home / "state"), {})
    assert bool(outcome.failures) is fail_install
    assert {identity for identity, enabled in native.enabled().items() if enabled} == {
        "kept@other"
    }
