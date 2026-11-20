import json
from collections.abc import Sequence
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import pytest

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "plugins/agent/.agents/agent/runtime"
sys.path.insert(0, str(RUNTIME))
from tj_agent.host import Host  # noqa: E402
from host_plugins import PLUGINS  # noqa: E402
import required_plugins as REQUIRED  # noqa: E402
from codex import catalog  # noqa: E402


class RequiredPluginInventoryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="agent-required-plugins-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.project, self.profile = self.root / "project", self.root / "profile"
        (self.project / ".agents").mkdir(parents=True)
        self.environment = mock.patch.dict(os.environ, {"CODEX_HOME": str(self.profile)})
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def entry(self, identity):
        name, marketplace = identity.split("@")
        root = self.profile / "plugins/cache" / marketplace / name / "version"
        (root / ".codex-plugin").mkdir(parents=True)
        (root / ".codex-plugin/plugin.json").write_text(json.dumps({"name": name}), encoding="utf-8")
        return {"name": name, "marketplaceName": marketplace, "version": "version", "enabled": True}

    def select(self, *identities):
        (self.project / ".agents/plugins.json").write_text(json.dumps({"schema_version": 1, "plugins": identities}), encoding="utf-8")

    def test_selected_codex_plugins_query_each_publisher_without_unfiltered_inventory(self):
        first, second = "agent@tj-agents", "cli@other-publisher"
        self.select(first, second)
        entries = {"tj-agents": self.entry(first), "other-publisher": self.entry(second)}

        def response(command, **_kwargs):
            marketplace = command[command.index("--marketplace") + 1]
            return subprocess.CompletedProcess(command, 0, json.dumps({"installed": [entries[marketplace]]}), "")

        with mock.patch.object(REQUIRED.process, "run", side_effect=response) as run:
            result = REQUIRED.check(self.project, Host.CODEX, "native-codex")
        self.assertEqual({first, second}, set(result))
        self.assertEqual([
            ["native-codex", "plugin", "list", "--marketplace", "other-publisher", "--json"],
            ["native-codex", "plugin", "list", "--marketplace", "tj-agents", "--json"],
        ], [call.args[0] for call in run.call_args_list])

    def test_native_inventory_can_finish_after_the_old_five_second_limit(self):
        script = self.root / "slow inventory.py"
        script.write_text("import time\ntime.sleep(5.2)\nprint('[]')\n", encoding="utf-8")
        native_run = subprocess.run

        def run(_command, **options):
            return native_run([sys.executable, str(script)], **options)

        with mock.patch.object(REQUIRED.process, "run", side_effect=run):
            self.assertEqual([], REQUIRED.inventory("native-claude", PLUGINS[Host.CLAUDE], self.project, ()))

    def test_multiple_marketplaces_share_one_inventory_deadline(self):
        self.select("agent@a", "agent@b", "agent@c")
        clock = iter((0.0, 0.0, 12.0, 21.0))
        with mock.patch.object(REQUIRED.time, "monotonic", side_effect=lambda: next(clock)), \
                mock.patch.object(REQUIRED, "inventory", return_value=[]) as inventory:
            with self.assertRaisesRegex(REQUIRED.RequiredPluginsError, "20-second budget"):
                REQUIRED.check(self.project, Host.CODEX, "native-codex")
        self.assertEqual([20.0, 8.0], [call.kwargs["timeout"] for call in inventory.call_args_list])

    def test_explicit_native_inventory_remains_unfiltered_for_cli_capability(self):
        entry = self.entry("cli@tj-agents")
        with mock.patch.object(REQUIRED.process, "run", return_value=subprocess.CompletedProcess(
                [], 0, json.dumps({"installed": [entry]}), "")) as run:
            roots = REQUIRED.inventory_roots(self.project, Host.CODEX, "native-codex")
        self.assertIn("cli@tj-agents", roots)
        self.assertEqual(["native-codex", "plugin", "list", "--json"], run.call_args.args[0])

    def test_update_inventory_does_not_query_unrelated_marketplaces(self):
        entry = self.entry("agent@tj-agents")

        def query(command, **_options):
            if "--marketplace" not in command:
                raise subprocess.TimeoutExpired(command, 20)
            self.assertEqual("tj-agents", command[command.index("--marketplace") + 1])
            return subprocess.CompletedProcess(command, 0, json.dumps({"installed": [entry]}), "")

        with mock.patch.object(REQUIRED.process, "run", side_effect=query):
            roots = REQUIRED.inventory_roots(self.project, Host.CODEX, "native-codex",
                                             identities=["agent@tj-agents"])
        self.assertEqual({"agent@tj-agents"}, roots.keys())

    def test_claude_launch_roots_reuse_overrides_until_the_delimiter(self):
        roots = {"agent@tj-agents": self.root / "override-agent"}
        arguments = [
            "--settings", "override.json", "--setting-sources=project,local",
            "--plugin-dir", "plugins", "--plugin-url=https://example.test/plugin", "--",
            "--settings", "literal prompt",
        ]
        with mock.patch.object(REQUIRED, "check", return_value=roots) as check:
            effective, actual = REQUIRED.check_launch_roots(self.project, Host.CLAUDE, "native-claude", arguments)
        self.assertEqual(self.project.resolve(), effective)
        self.assertIs(roots, actual)
        check.assert_called_once_with(
            self.project.resolve(), Host.CLAUDE, "native-claude",
            inventory_options=(
                "--settings", "override.json", "--setting-sources", "project,local",
                "--plugin-dir", "plugins", "--plugin-url", "https://example.test/plugin",
            ),
        )


def git(directory: Path, *arguments: str) -> None:
    subprocess.run(["git", *arguments], cwd=directory, check=True, capture_output=True)


def test_selected_uses_a_primary_worktrees_selection_unless_local_override_exists(
    tmp_path: Path,
) -> None:
    primary = tmp_path / "primary"
    primary.mkdir()
    (primary / "README.md").write_text("primary\n", encoding="utf-8")
    git(primary, "init", "-q")
    git(primary, "add", "README.md")
    git(primary, "-c", "user.name=t", "-c", "user.email=t@example.test", "commit", "-qm", "initial")
    linked = tmp_path / "linked"
    git(primary, "worktree", "add", "-q", str(linked))

    source = primary / ".agents/plugins.json"
    source.parent.mkdir()
    source.write_text(
        json.dumps({"schema_version": 1, "plugins": ["base@tj-agents"]}),
        encoding="utf-8",
    )

    assert REQUIRED.selected(linked) == ("base@tj-agents",)

    local = linked / ".agents/plugins.json"
    local.parent.mkdir(exist_ok=True)
    local.write_text("{broken", encoding="utf-8")
    with pytest.raises(REQUIRED.RequiredPluginsError, match="Cannot read required plugin selection"):
        REQUIRED.selected(linked)


def test_selected_leaves_an_ordinary_unconfigured_directory_unselected(tmp_path: Path) -> None:
    assert REQUIRED.selected(tmp_path) == ()


def test_project_root_preserves_a_malformed_nested_selection_boundary(
    tmp_path: Path,
) -> None:
    selection = tmp_path / ".agents/plugins.json"
    selection.parent.mkdir()
    selection.write_text(
        json.dumps({"schema_version": 1, "plugins": ["base@tj-agents"]}),
        encoding="utf-8",
    )
    nested = tmp_path / "nested"
    (nested / ".agents/plugins.json").mkdir(parents=True)
    directory = nested / "source"
    directory.mkdir()

    project = REQUIRED.project_root(directory)

    assert project == nested
    with pytest.raises(
        REQUIRED.RequiredPluginsError, match="Cannot read required plugin selection"
    ):
        REQUIRED.selected(project)


def test_project_root_preserves_a_dangling_nested_selection_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    selection = tmp_path / ".agents/plugins.json"
    selection.parent.mkdir()
    selection.write_text(
        json.dumps({"schema_version": 1, "plugins": ["base@tj-agents"]}),
        encoding="utf-8",
    )
    nested = tmp_path / "nested"
    local = nested / ".agents/plugins.json"
    directory = nested / "source"
    directory.mkdir(parents=True)
    is_symlink = Path.is_symlink
    monkeypatch.setattr(
        Path, "is_symlink", lambda path: path == local or is_symlink(path)
    )

    assert REQUIRED.project_root(directory) == nested


@pytest.mark.parametrize(
    "setup",
    [
        {"script": "setup.py"},
        {"script": "setup.py", "converge": True},
        {"script": "setup.py", "converge": False},
    ],
)
def test_setup_declaration_accepts_script_alone_or_with_a_bool_converge(
    tmp_path: Path,
    setup: dict[str, object],
) -> None:
    (tmp_path / "setup.py").write_text("", encoding="utf-8")
    document = {"requires": {"setup": setup}}
    target = REQUIRED.declaration(tmp_path, "agent@tj-agents", document)
    assert target is not None
    assert target.converge is setup.get("converge", False)


@pytest.mark.parametrize(
    "setup",
    [
        {"script": "setup.py", "converge": "yes"},
        {"script": "setup.py", "extra": True},
        {"converge": True},
        {"script": "setup.py", "converge": True, "extra": 1},
    ],
)
def test_setup_declaration_rejects_a_non_bool_converge_or_an_unknown_key(
    tmp_path: Path,
    setup: dict[str, object],
) -> None:
    (tmp_path / "setup.py").write_text("", encoding="utf-8")
    document = {"requires": {"setup": setup}}
    with pytest.raises(REQUIRED.PluginSetupError):
        REQUIRED.declaration(tmp_path, "agent@tj-agents", document)



def claude_project(tmp_path: Path, *identities: str) -> Path:
    project = tmp_path / "project"
    (project / ".agents").mkdir(parents=True)
    (project / ".agents/plugins.json").write_text(
        json.dumps({"schema_version": 1, "plugins": list(identities)}), encoding="utf-8")
    return project


def claude_row(tmp_path: Path, identity: str, dependencies: Sequence[object] = (), **extra: object) -> dict[str, object]:
    root = tmp_path / "cache" / identity
    (root / ".claude-plugin").mkdir(parents=True, exist_ok=True)
    manifest = {"name": identity.split("@")[0], "dependencies": list(dependencies)}
    (root / ".claude-plugin/plugin.json").write_text(json.dumps(manifest), encoding="utf-8")
    return {"id": identity, "scope": "user", "enabled": True, "installPath": str(root), **extra}


def test_check_verifies_the_transitive_dependencies_installed_builds_declare(tmp_path: Path) -> None:
    project = claude_project(tmp_path, "company@tj-agents")
    rows = [
        claude_row(tmp_path, "company@tj-agents", ["docs@tj-agents", "work"]),
        claude_row(tmp_path, "work@tj-agents", ["base"]),
        claude_row(tmp_path, "docs@tj-agents"),
        claude_row(tmp_path, "base@tj-agents"),
    ]

    roots = REQUIRED.check(project, Host.CLAUDE, "native-claude", entries=rows)

    assert roots is not None
    assert sorted(roots) == ["base@tj-agents", "company@tj-agents", "docs@tj-agents", "work@tj-agents"]


def test_codex_prefetch_follows_foreign_dependencies_without_recording_builds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = claude_project(tmp_path, "work@tj-agents")
    profile = tmp_path / "codex"
    monkeypatch.setenv("CODEX_HOME", str(profile))
    rows = {}
    for identity, dependencies in (
        ("work@tj-agents", ["docs@foreign"]),
        ("docs@foreign", ["work@tj-agents"]),
    ):
        name, marketplace = identity.split("@")
        root = profile / "plugins/cache" / marketplace / name / "version"
        for host in Host:
            manifest = root / host.manifest_directory / "plugin.json"
            manifest.parent.mkdir(parents=True, exist_ok=True)
            manifest.write_text(json.dumps({"name": name, "dependencies": dependencies}), encoding="utf-8")
        rows[marketplace] = {"name": name, "marketplaceName": marketplace, "version": "version", "enabled": True}

    def response(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        marketplace = command[command.index("--marketplace") + 1]
        return subprocess.CompletedProcess(command, 0, json.dumps({"installed": [rows[marketplace]]}), "")

    with mock.patch.object(REQUIRED.process, "run", side_effect=response) as run, mock.patch.object(catalog, "remember") as remember:
        entries = REQUIRED.inventory_rows(project, Host.CODEX, "native-codex", identities=["work@tj-agents"])
        remember.assert_not_called()
        roots = REQUIRED.check(project, Host.CODEX, "native-codex", entries=entries)
        remember.assert_called_once()
    assert roots is not None and set(roots) == {"work@tj-agents", "docs@foreign"}
    assert [call.args[0][-2] for call in run.call_args_list] == ["tj-agents", "foreign"]


def test_check_reports_a_missing_transitive_dependency(tmp_path: Path) -> None:
    project = claude_project(tmp_path, "company@tj-agents")
    rows = [claude_row(tmp_path, "company@tj-agents", ["docs"])]

    with pytest.raises(REQUIRED.MissingRequiredPluginsError, match="cached build: docs@tj-agents") as raised:
        REQUIRED.check(project, Host.CLAUDE, "native-claude", entries=rows)
    assert raised.value.missing == ("docs@tj-agents",)


@pytest.mark.parametrize(("detail", "problem"), [
    ({"reason": "not-enabled"}, "is not enabled in"),
    ({"reason": "not-found"}, "is not installed"),
    ({"type": "dependency-version-unsatisfied", "required": "^2"}, "does not satisfy the required version ^2"),
])
def test_check_names_the_dependency_that_keeps_a_selected_plugin_from_loading(
    tmp_path: Path, detail: dict[str, object], problem: str,
) -> None:
    project = claude_project(tmp_path, "company@tj-agents")
    error = {"type": "dependency-unsatisfied", "plugin": "company@tj-agents",
             "dependency": "docs@tj-agents", **detail}
    rows = [claude_row(tmp_path, "company@tj-agents", ["docs"], errors=["Dependency is unavailable"],
                       errorDetails=[error])]

    with pytest.raises(REQUIRED.UnsatisfiedDependencyError) as raised:
        REQUIRED.check(project, Host.CLAUDE, "native-claude", entries=rows)

    assert f"claude will not load company@tj-agents: its dependency docs@tj-agents {problem}" in str(raised.value)
    assert raised.value.missing == ("docs@tj-agents",)
    assert REQUIRED.enabled_roots(PLUGINS[Host.CLAUDE], project, rows) == {}


def test_check_surfaces_why_a_failing_enabled_plugin_was_excluded(tmp_path: Path) -> None:
    project = claude_project(tmp_path, "agent@tj-agents")
    rows = [claude_row(tmp_path, "agent@tj-agents", errors=["Hook load failed"],
                       errorDetails=[{"type": "hook-load-failed"}])]

    with pytest.raises(REQUIRED.MissingRequiredPluginsError, match=r"agent@tj-agents \(Hook load failed\)"):
        REQUIRED.check(project, Host.CLAUDE, "native-claude", entries=rows)


def test_check_keeps_independent_missing_plugins_in_the_repair_set(
    tmp_path: Path,
) -> None:
    project = claude_project(tmp_path, "company@tj-agents", "agent@tj-agents")
    error = {
        "type": "dependency-unsatisfied",
        "plugin": "company@tj-agents",
        "dependency": "docs@tj-agents",
        "reason": "not-found",
    }
    rows = [
        claude_row(
            tmp_path, "company@tj-agents", ["docs"],
            errors=["Dependency is unavailable"], errorDetails=[error],
        )
    ]

    with pytest.raises(REQUIRED.UnsatisfiedDependencyError, match="docs@tj-agents") as raised:
        REQUIRED.check(project, Host.CLAUDE, "native-claude", entries=rows)

    assert raised.value.missing == ("agent@tj-agents", "docs@tj-agents")


def test_setup_targets_include_the_dependencies_of_the_selection(tmp_path: Path) -> None:
    project = claude_project(tmp_path, "company@tj-agents")
    rows = [claude_row(tmp_path, "company@tj-agents", ["docs"]), claude_row(tmp_path, "docs@tj-agents")]
    for row in rows:
        root = Path(str(row["installPath"]))
        identity = str(row["id"])
        name, marketplace = identity.split("@")
        (root / "setup.py").write_text("", encoding="utf-8")
        (root / "harness.json").write_text(json.dumps(
            {"plugin": f"{marketplace}/{name}", "requires": {"setup": {"script": "setup.py"}}}), encoding="utf-8")

    with mock.patch.object(REQUIRED, "inventory_rows", return_value=rows):
        targets = REQUIRED.targets(project, Host.CLAUDE, "native-claude")

    assert [target.identity for target in targets] == ["company@tj-agents", "docs@tj-agents"]


if __name__ == "__main__":
    unittest.main()
