import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import pytest
from published_plugins import none_declared


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "plugins" / "agent" / ".agents" / "agent" / "utility" / "plugins" / "scripts"
sys.path.insert(0, str(SCRIPTS))


def load(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ADOPT = load("adopt_plugins")
LockError = ADOPT.lock_io.LockError


class RemovePluginTests(unittest.TestCase):
    def setUp(self):
        live = mock.patch.object(ADOPT, "current_roster", side_effect=ADOPT.load_roster)
        live.start()
        self.addCleanup(live.stop)
        published = mock.patch.object(ADOPT.repo_config.published_harness, "declarations", none_declared)
        published.start()
        self.addCleanup(published.stop)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "project"
        (self.root / ".agents").mkdir(parents=True)

    def write_selection(self, plugins):
        (self.root / ".agents" / "plugins.json").write_text(json.dumps({
            "schema_version": 1,
            "marketplaces": {"tj-agents": {"repository": "tj-agents/marketplace"}},
            "plugins": plugins,
        }), encoding="utf-8")

    def selected(self):
        return json.loads((self.root / ".agents" / "plugins.json").read_text(encoding="utf-8"))["plugins"]

    def test_removal_of_agent_cli_prerequisite_refuses_without_writing(self):
        self.write_selection(["agent@tj-agents", "base@tj-agents", "cli@tj-agents"])
        before = (self.root / ".agents" / "plugins.json").read_bytes()
        with self.assertRaisesRegex(LockError, "prerequisite"):
            ADOPT.run(self.root, [], [], "write", removals=["cli"])
        self.assertEqual(before, (self.root / ".agents" / "plugins.json").read_bytes())

    def test_missing_removal_is_idempotent_and_does_not_restore_defaults(self):
        self.write_selection(["base@tj-agents", "cli@tj-agents"])
        ADOPT.run(self.root, [], [], "write", removals=["cli"])
        self.assertEqual(["base@tj-agents"], self.selected())
        self.assertEqual([], ADOPT.run(self.root, [], [], "check", removals=["cli"]))

    def test_selected_marketplace_keeps_auto_update_while_removed_marketplaces_drop(self):
        self.write_selection(["base@tj-agents"])
        settings = self.root / ".claude/settings.json"
        settings.parent.mkdir()
        settings.write_text(json.dumps({"extraKnownMarketplaces": {
            "tj-agents": {"source": {"source": "github", "repo": "tj-agents/marketplace"}, "autoUpdate": True},
            "retired": {"source": {"source": "github", "repo": "tj-agents/retired"}, "autoUpdate": True},
        }}), encoding="utf-8")

        ADOPT.run(self.root, [], [], "write")

        self.assertEqual({"tj-agents": {
            "source": {"source": "github", "repo": "tj-agents/marketplace"}, "autoUpdate": True,
        }}, json.loads(settings.read_text(encoding="utf-8"))["extraKnownMarketplaces"])

    def test_selected_marketplace_keeps_auto_update_false(self):
        self.write_selection(["base@tj-agents"])
        settings = self.root / ".claude/settings.json"
        settings.parent.mkdir()
        settings.write_text(json.dumps({"extraKnownMarketplaces": {
            "tj-agents": {"source": {"source": "github", "repo": "tj-agents/marketplace"}, "autoUpdate": False},
        }}), encoding="utf-8")

        ADOPT.run(self.root, [], [], "write")

        self.assertFalse(json.loads(settings.read_text(encoding="utf-8"))["extraKnownMarketplaces"]["tj-agents"]["autoUpdate"])

    def test_unpublished_selections_converge_away_without_a_removal_request(self):
        self.write_selection(["agent@tj-agents", "agent-store@tj-agents", "base@tj-agents", "cli@tj-agents",
                              "kit@tj-agents", "machine@tj-agents"])
        ADOPT.run(self.root, [], [], "write")
        self.assertEqual(["agent-store@tj-agents", "agent@tj-agents", "base@tj-agents", "cli@tj-agents"],
                         self.selected())
        self.assertNotIn("machine@tj-agents", (self.root / ".claude" / "settings.json").read_text(encoding="utf-8"))
        self.assertEqual([], ADOPT.run(self.root, [], [], "check"))

    def test_default_selection_never_selects_the_retired_machine_package(self):
        ADOPT.run(self.root, [], [], "write")
        self.assertEqual(sorted(ADOPT.plugin_selection.plugin_id(name) for name in ADOPT.plugin_selection.CORE_PLUGINS),
                         self.selected())
        self.assertNotIn("machine@tj-agents", self.selected())

    def test_adoption_writes_only_the_requested_plugins_not_their_dependencies(self):
        ADOPT.run(self.root, [], ["python"], "write")
        self.assertEqual(["python@tj-agents"], self.selected())

    def test_adoption_keeps_an_existing_flattened_selection_unchanged(self):
        flattened = ["agent-store@tj-agents", "agent@tj-agents", "base@tj-agents", "cli@tj-agents"]
        self.write_selection(flattened)
        ADOPT.run(self.root, [], ["python"], "write")
        self.assertEqual(sorted([*flattened, "python@tj-agents"]), self.selected())

    def test_removal_of_an_unlisted_dependency_of_a_remaining_plugin_refuses(self):
        self.write_selection(["agent@tj-agents"])
        before = (self.root / ".agents" / "plugins.json").read_bytes()
        with self.assertRaisesRegex(LockError, "prerequisite required by remaining plugins: cli"):
            ADOPT.run(self.root, [], [], "write", removals=["cli"])
        self.assertEqual(before, (self.root / ".agents" / "plugins.json").read_bytes())

    def test_automatic_reconciliation_checks_then_repairs_both_hosts_and_is_idempotent(self):
        self.write_selection(["engineering@tj-agents", "machine@tj-agents", "python@tj-agents"])
        before = (self.root / ".agents/plugins.json").read_bytes()
        with mock.patch.object(ADOPT, "current_roster", return_value=ADOPT.load_roster()):
            self.assertIn(".agents/plugins.json", ADOPT.reconcile(self.root, "check"))
            self.assertEqual(before, (self.root / ".agents/plugins.json").read_bytes())
            self.assertFalse((self.root / ".codex").exists())
            ADOPT.reconcile(self.root, "write")
        self.assertEqual(["engineering@tj-agents", "python@tj-agents"], self.selected())
        for path in (".agents/plugins.json", ".claude/settings.json", ".codex/config.toml"):
            self.assertNotIn("machine@tj-agents", (self.root / path).read_text(encoding="utf-8"))
        before = {path: (path.read_bytes(), path.stat().st_mtime_ns)
                  for path in self.root.rglob("*") if path.is_file()}
        self.assertEqual([], ADOPT.reconcile(self.root, "write"))
        self.assertEqual(before, {path: (path.read_bytes(), path.stat().st_mtime_ns)
                                  for path in self.root.rglob("*") if path.is_file()})

    def test_automatic_reconciliation_does_not_drop_newer_published_plugins(self):
        self.write_selection(["base@tj-agents", "new-plugin@tj-agents"])
        roster = ADOPT.load_roster()
        roster["plugins"]["new-plugin"] = ["base"]
        with mock.patch.object(ADOPT, "current_roster", return_value=roster) as current:
            ADOPT.reconcile(self.root, "write")
        current.assert_called_once()
        self.assertIn("new-plugin@tj-agents", self.selected())

    def test_unreachable_marketplace_does_not_remove_unknown_selection(self):
        self.write_selection(["base@tj-agents", "machine@tj-agents"])
        before = (self.root / ".agents/plugins.json").read_bytes()
        with mock.patch.object(ADOPT, "current_roster", side_effect=LockError("offline")), \
                self.assertRaisesRegex(LockError, "offline"):
            ADOPT.reconcile(self.root, "write")
        self.assertEqual(before, (self.root / ".agents/plugins.json").read_bytes())
        self.assertFalse((self.root / ".codex").exists())

    def test_automatic_reconciliation_does_not_enroll_an_unconfigured_project(self):
        self.assertEqual([], ADOPT.reconcile(self.root, "write"))
        self.assertFalse((self.root / ".agents/plugins.json").exists())

    def test_batch_reconciliation_uses_one_live_roster_and_plans_every_project_before_writing(self):
        self.write_selection(["engineering@tj-agents"])
        other = self.root.parent / "z-other"
        (other / ".agents").mkdir(parents=True)
        (other / ".agents/plugins.json").write_bytes((self.root / ".agents/plugins.json").read_bytes())
        (other / ".claude").mkdir()
        path = other / ".claude/settings.json"
        path.write_text("{broken", encoding="utf-8")
        before = (self.root / ".agents/plugins.json").read_bytes()
        with mock.patch.object(ADOPT, "current_roster", return_value=ADOPT.load_roster()) as roster, \
                self.assertRaises(LockError):
            ADOPT.reconcile_many([self.root, other], "write")
        roster.assert_called_once()
        self.assertEqual(before, (self.root / ".agents/plugins.json").read_bytes())
        self.assertFalse((self.root / ".claude").exists())
        path.write_text("{}", encoding="utf-8")
        with mock.patch.object(ADOPT, "current_roster", return_value=ADOPT.load_roster()) as roster:
            ADOPT.reconcile_many([self.root, other], "write")
        roster.assert_called_once()
        self.assertTrue((self.root / ".codex/config.toml").is_file())
        self.assertTrue((other / ".codex/config.toml").is_file())
        self.assertEqual([], ADOPT.reconcile_many([self.root, other], "write"))

    def test_retirement_after_the_bundled_snapshot_is_reconciled(self):
        self.write_selection(["base@tj-agents", "docs@tj-agents"])
        roster = ADOPT.load_roster()
        self.assertIn("docs", roster["plugins"])
        del roster["plugins"]["docs"]
        with mock.patch.object(ADOPT, "current_roster", return_value=roster):
            ADOPT.reconcile(self.root, "write")
        self.assertEqual(["base@tj-agents"], self.selected())

    def test_live_roster_does_not_require_bundled_stack_presets_to_remain_valid(self):
        snapshot = ADOPT.lock_io.load_json(ADOPT.LOCK)
        snapshot["marketplace"]["plugins"] = [entry for entry in snapshot["marketplace"]["plugins"]
                                                if entry["name"] != "gpp"]
        roster = ADOPT.load_roster(snapshot)
        self.assertNotIn("gpp", roster["plugins"])
        self.assertEqual({}, roster["stacks"])

    def test_removal_of_remaining_prerequisite_refuses_without_writing(self):
        self.write_selection(["agent@tj-agents", "base@tj-agents", "cli@tj-agents"])
        before = (self.root / ".agents" / "plugins.json").read_bytes()
        with self.assertRaisesRegex(LockError, "prerequisite"):
            ADOPT.run(self.root, [], [], "write", removals=["base"])
        self.assertEqual(before, (self.root / ".agents" / "plugins.json").read_bytes())


EMPLOYER = {"all": [{"remote_host": "example.test"}, {"remote": "^example-employer/"}]}


@pytest.fixture
def installed_employers(monkeypatch: pytest.MonkeyPatch) -> list[object]:
    """The employer declarations adoption sees as installed; none unless a test adds one."""
    employers: list[object] = []
    monkeypatch.setattr(ADOPT.declarations, "installed_employers", lambda _root, _environ: employers)
    return employers


@pytest.fixture
def profile_roster(installed_employers: list[object]) -> dict[str, object]:
    """The bundled roster declaring each kind, with example-employer an organization extending work."""
    snapshot = ADOPT.lock_io.load_json(ADOPT.LOCK)
    entries = snapshot["marketplace"]["plugins"]
    for entry in entries:
        entry.pop("profile", None)
        entry["kind"] = ("profile" if entry["name"] in {"personal", "work"}
                         else "organization" if "extends" in entry else "shared")
    entries.append({"name": "example-employer", "repository": "example-employer", "revision": "a" * 40,
                    "requires": ["work"], "kind": "organization", "extends": "work"})
    roster: dict[str, object] = ADOPT.load_roster(snapshot)
    return roster


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(ADOPT.repo_config.published_harness, "declarations", none_declared)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    (tmp_path / ".agents").mkdir()
    subprocess.run(["git", "init", "--quiet", str(tmp_path)], check=True, timeout=15)
    return tmp_path


def set_origin(root: Path, url: str) -> None:
    subprocess.run(["git", "-C", str(root), "config", "remote.origin.url", url], check=True, timeout=15)


def employer(marketplace: str = "tj-agents") -> object:
    return ADOPT.declarations.Employer("example-employer", marketplace, EMPLOYER, ("publisher/example-employer",))


def selection(root: Path) -> list[str]:
    plugins: list[str] = json.loads((root / ".agents" / "plugins.json").read_text(encoding="utf-8"))["plugins"]
    return plugins


def test_load_roster_reads_profiles_and_the_profile_each_organization_extends(
    profile_roster: dict[str, object],
) -> None:
    organizations = profile_roster["organizations"]
    assert profile_roster["profiles"] == {"personal", "work"}
    assert isinstance(organizations, dict)
    assert organizations["example-employer"] == "work"


def test_load_roster_refuses_an_organization_without_the_profile_it_extends() -> None:
    snapshot = ADOPT.lock_io.load_json(ADOPT.LOCK)
    snapshot["marketplace"]["plugins"].append(
        {"name": "example-employer", "repository": "example-employer", "revision": "a" * 40, "kind": "organization"})
    with pytest.raises(LockError, match="organization example-employer must extend a profile"):
        ADOPT.load_roster(snapshot)


def test_run_selects_a_single_profile(project: Path, profile_roster: dict[str, object]) -> None:
    ADOPT.run(project, [], ["personal"], "write", roster=profile_roster)
    assert "personal@tj-agents" in selection(project)


def test_run_leaves_the_profile_an_organization_extends_to_its_dependencies(
    project: Path, profile_roster: dict[str, object]
) -> None:
    ADOPT.run(project, [], ["example-employer"], "write", roster=profile_roster)
    assert selection(project) == ["example-employer@tj-agents"]


@pytest.mark.parametrize("plugins", [["personal", "work"], ["personal", "example-employer"]])
def test_run_refuses_two_profiles_without_writing(
    project: Path, profile_roster: dict[str, object], plugins: list[str]
) -> None:
    with pytest.raises(LockError, match="one profile, not personal and work"):
        ADOPT.run(project, [], plugins, "write", roster=profile_roster)
    assert not (project / ".agents" / "plugins.json").exists()


def test_reconcile_refuses_an_existing_selection_of_two_profiles(
    project: Path, profile_roster: dict[str, object]
) -> None:
    path = project / ".agents" / "plugins.json"
    path.write_text(json.dumps({
        "schema_version": 1,
        "marketplaces": {"tj-agents": {"repository": "tj-agents/marketplace"}},
        "plugins": ["personal@tj-agents", "work@tj-agents"],
    }), encoding="utf-8")
    before = path.read_bytes()
    with mock.patch.object(ADOPT, "current_roster", return_value=profile_roster),             pytest.raises(LockError, match="one profile"):
        ADOPT.reconcile(project, "write")
    assert path.read_bytes() == before


@pytest.mark.parametrize(("url", "evidence"), [
    ("git@example.test:example-employer/service.git", "origin host example.test, origin example-employer/service"),
    ("https://github.com/publisher/example-employer.git", "source repository publisher/example-employer"),
])
def test_run_refuses_personal_where_an_installed_employer_matches_without_writing(
    project: Path, profile_roster: dict[str, object], installed_employers: list[object], url: str, evidence: str
) -> None:
    installed_employers.append(employer())
    set_origin(project, url)
    message = ("Cannot select personal: the installed employer declaration example-employer@tj-agents matches "
               f"this repository ({evidence}); select work, the profile example-employer extends")
    with pytest.raises(LockError, match=re.escape(message)):
        ADOPT.run(project, [], ["personal"], "write", roster=profile_roster)
    assert not (project / ".agents" / "plugins.json").exists()


def test_run_selects_the_profile_a_matching_employer_extends(
    project: Path, profile_roster: dict[str, object], installed_employers: list[object]
) -> None:
    installed_employers.append(employer())
    set_origin(project, "git@example.test:example-employer/service.git")
    ADOPT.run(project, [], ["example-employer"], "write", roster=profile_roster)
    assert selection(project) == ["example-employer@tj-agents"]


def test_run_selects_personal_where_no_installed_employer_matches(
    project: Path, profile_roster: dict[str, object], installed_employers: list[object]
) -> None:
    installed_employers.append(employer())
    set_origin(project, "git@example.test:someone/service.git")
    ADOPT.run(project, [], ["personal"], "write", roster=profile_roster)
    assert "personal@tj-agents" in selection(project)


def test_adoption_copies_an_inherited_selection_to_the_linked_checkout_and_excludes_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ADOPT.repo_config.published_harness, "declarations", none_declared)
    primary = tmp_path / "primary"
    primary.mkdir()
    (primary / "README.md").write_text("primary\n", encoding="utf-8")
    subprocess.run(["git", "init", "--quiet", str(primary)], check=True, timeout=15)
    subprocess.run(["git", "-C", str(primary), "add", "README.md"], check=True, timeout=15)
    subprocess.run(["git", "-C", str(primary), "-c", "user.name=t", "-c", "user.email=t@example.test", "commit", "-qm", "initial"], check=True, timeout=15)
    subprocess.run(["git", "-C", str(primary), "remote", "add", "origin", "https://github.com/employer/project.git"], check=True, timeout=15)
    linked = tmp_path / "linked"
    subprocess.run(["git", "-C", str(primary), "worktree", "add", "--quiet", str(linked)], check=True, timeout=15)
    (primary / ".agents").mkdir()
    inherited = {
        "schema_version": 1,
        "marketplaces": {"tj-agents": {"repository": "tj-agents/marketplace"}},
        "plugins": ["base@tj-agents"],
    }
    (primary / ".agents/plugins.json").write_text(json.dumps(inherited), encoding="utf-8")

    ADOPT.run(linked, [], [], "write")

    local = linked / ".agents/plugins.json"
    assert local.is_file()
    assert json.loads(local.read_text(encoding="utf-8"))["plugins"] == inherited["plugins"]
    assert subprocess.run(["git", "check-ignore", "-q", ".agents/plugins.json"], cwd=linked, timeout=15).returncode == 0


def test_run_refuses_every_profile_where_an_employer_outside_the_roster_matches(
    project: Path, profile_roster: dict[str, object], installed_employers: list[object]
) -> None:
    installed_employers.append(employer("elsewhere"))
    set_origin(project, "git@example.test:example-employer/service.git")
    with pytest.raises(LockError, match="example-employer@elsewhere is not an organization in the tj-agents roster"):
        ADOPT.run(project, [], ["work"], "write", roster=profile_roster)

if __name__ == "__main__":
    unittest.main()
