import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

import pytest


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
        self.assertIn("cli@tj-agents", self.selected())
        self.assertNotIn("machine@tj-agents", self.selected())

    def test_automatic_reconciliation_checks_then_repairs_both_hosts_and_is_idempotent(self):
        self.write_selection(["engineering@tj-agents", "machine@tj-agents", "python@tj-agents"])
        before = (self.root / ".agents/plugins.json").read_bytes()
        with mock.patch.object(ADOPT, "current_roster", return_value=ADOPT.load_roster()):
            self.assertIn(".agents/plugins.json", ADOPT.reconcile(self.root, "check"))
            self.assertEqual(before, (self.root / ".agents/plugins.json").read_bytes())
            self.assertFalse((self.root / ".codex").exists())
            ADOPT.reconcile(self.root, "write")
        for identity in ("agent@tj-agents", "cli@tj-agents", "python@tj-agents"):
            self.assertIn(identity, self.selected())
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


@pytest.fixture
def profile_roster() -> dict[str, object]:
    """The bundled roster with personal and work marked as generic profiles and a specific profile of work."""
    snapshot = ADOPT.lock_io.load_json(ADOPT.LOCK)
    entries = snapshot["marketplace"]["plugins"]
    for entry in entries:
        if entry["name"] in {"personal", "work"}:
            entry["generic_profile"] = True
    entries.append({"name": "example-employer", "repository": "example-employer", "revision": "a" * 40,
                    "requires": ["work"], "profile": "specific", "extends": "work"})
    roster: dict[str, object] = ADOPT.load_roster(snapshot)
    return roster


@pytest.fixture
def project(tmp_path: Path) -> Path:
    (tmp_path / ".agents").mkdir()
    return tmp_path


def selection(root: Path) -> list[str]:
    plugins: list[str] = json.loads((root / ".agents" / "plugins.json").read_text(encoding="utf-8"))["plugins"]
    return plugins


def test_load_roster_reads_generic_profiles(profile_roster: dict[str, object]) -> None:
    assert profile_roster["generic_profiles"] == {"personal", "work"}


def test_run_selects_a_single_generic_profile(project: Path, profile_roster: dict[str, object]) -> None:
    ADOPT.run(project, [], ["personal"], "write", roster=profile_roster)
    assert "personal@tj-agents" in selection(project)


def test_run_adds_the_generic_profile_a_specific_profile_extends(
    project: Path, profile_roster: dict[str, object]
) -> None:
    ADOPT.run(project, [], ["example-employer"], "write", roster=profile_roster)
    assert {"example-employer@tj-agents", "work@tj-agents"} <= set(selection(project))


@pytest.mark.parametrize("plugins", [["personal", "work"], ["personal", "example-employer"]])
def test_run_refuses_two_generic_profiles_without_writing(
    project: Path, profile_roster: dict[str, object], plugins: list[str]
) -> None:
    with pytest.raises(LockError, match="one generic profile, not personal and work"):
        ADOPT.run(project, [], plugins, "write", roster=profile_roster)
    assert not (project / ".agents" / "plugins.json").exists()


def test_reconcile_refuses_an_existing_selection_of_two_generic_profiles(
    project: Path, profile_roster: dict[str, object]
) -> None:
    path = project / ".agents" / "plugins.json"
    path.write_text(json.dumps({
        "schema_version": 1,
        "marketplaces": {"tj-agents": {"repository": "tj-agents/marketplace"}},
        "plugins": ["personal@tj-agents", "work@tj-agents"],
    }), encoding="utf-8")
    before = path.read_bytes()
    with mock.patch.object(ADOPT, "current_roster", return_value=profile_roster), \
            pytest.raises(LockError, match="one generic profile"):
        ADOPT.reconcile(project, "write")
    assert path.read_bytes() == before


if __name__ == "__main__":
    unittest.main()
