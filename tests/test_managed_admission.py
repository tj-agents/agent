import contextlib
import importlib
import io
import json
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import patch

import host_plugins
import managed_admission as admission
import plugin_autoupdate
import plugin_refresh
import pytest
import required_plugins
from host import Host
from tj_agent import content
from tj_agent.plugins import sources

codex_plugins = importlib.import_module("codex.plugins")
NATIVE_INVENTORY = required_plugins.inventory
NATIVE_ENSURE_HOOKS = codex_plugins.ensure_hooks


class AdmissionFixture:
    def __init__(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.project = self.root / "project"
        self.project.mkdir()
        (self.project / ".agents").mkdir()
        (self.project / ".agents/plugins.json").write_text(
            json.dumps(
                {"schema_version": 1, "plugins": ["agent@tj-agents", "base@tj-agents"]}
            )
        )
        self.market = self.root / "marketplace"
        self.market.mkdir()
        self.catalog = {
            "plugins": [
                self.entry("agent", "agent", "a"),
                self.entry("base", "core", "c"),
            ]
        }
        self.roots = {}
        guard_command = "python managed_admission.py hook"
        for name in ("agent", "base"):
            root = self.root / name
            (root / "hooks").mkdir(parents=True)
            (root / "harness.json").write_text(
                json.dumps(
                    {
                        "plugin": "tj-agents/" + name,
                        "requires": {
                            "hooks": [
                                {
                                    "path": "managed_admission.py",
                                    "hosts": ["claude", "codex"],
                                }
                            ]
                        },
                    }
                )
            )
            for host in Host:
                (root / "hooks" / (host.value + ".json")).write_text(
                    json.dumps(
                        {
                            "hooks": {
                                event: [{"hooks": [{"command": guard_command}]}]
                                for event in (
                                    "SessionStart",
                                    "UserPromptSubmit",
                                    "PreToolUse",
                                )
                            }
                        }
                    )
                )
            for host in Host:
                manifest = root / host.manifest_directory / "plugin.json"
                manifest.parent.mkdir()
                manifest.write_text(
                    json.dumps({"name": name, "hooks": f"./hooks/{host.value}.json"}),
                    encoding="utf-8",
                )
            (root / "value.txt").write_text("selected")
            self.roots[name + "@tj-agents"] = root
        self.expected = {
            identity.split("@")[0]: plugin_refresh.package_tree(root)
            for identity, root in self.roots.items()
        }
        self.stack = contextlib.ExitStack()
        self.stack.enter_context(
            patch.dict(
                os.environ,
                {
                    "AGENT_STATE_DIRECTORY": str(self.root / "state"),
                    "CODEX_HOME": str(self.root / "codex"),
                    "CLAUDE_CONFIG_DIR": str(self.root / "claude"),
                },
            )
        )
        self.stack.enter_context(
            patch.object(
                sources,
                "tree",
                side_effect=lambda source, *args: self.expected[
                    source["path"].split("/")[-1]
                ],
            )
        )
        self.stack.enter_context(
            patch.object(plugin_refresh, "run", side_effect=self.git)
        )
        self.stack.enter_context(
            patch.object(required_plugins, "inventory", side_effect=self.inventory)
        )
        for plugins in host_plugins.PLUGINS.values():
            self.stack.enter_context(
                patch.object(plugins, "resolve_executable", return_value="fixture")
            )
            self.stack.enter_context(
                patch.object(
                    plugins, "marketplace_location", return_value=str(self.market)
                )
            )
            self.stack.enter_context(
                patch.object(plugins, "ensure_hooks", return_value=None)
            )
            self.stack.enter_context(
                patch.object(
                    plugins,
                    "location",
                    side_effect=lambda entry, project: (
                        entry["identity"],
                        Path(entry["root"]),
                    ),
                )
            )
            self.stack.enter_context(
                patch.object(
                    plugins,
                    "installations",
                    side_effect=lambda project, entries: entries,
                )
            )
        self.save_catalog()

    def entry(self, name: str, producer: str, revision: str) -> dict[str, Any]:
        return {
            "name": name,
            "source": {
                "source": "git-subdir",
                "url": f"https://github.com/tj-agents/{producer}.git",
                "path": f"./plugins/{name}",
                "sha": revision * 40,
            },
        }

    def save_catalog(self) -> None:
        for plugins in host_plugins.PLUGINS.values():
            path = self.market / plugins.CATALOG
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(self.catalog))

    def inventory(self, *args: object, **kwargs: object) -> list[dict[str, object]]:
        return [
            {"enabled": True, "identity": identity, "root": str(root)}
            for identity, root in self.roots.items()
        ]

    def git(self, command: list[str], project: Path) -> str:
        if "ls-remote" in command:
            return "f" * 40 + " refs/heads/main"
        if "show" in command:
            if command[-1].endswith(":plugins.json"):
                return json.dumps(
                    {
                        "plugins": [
                            {
                                "name": entry["name"],
                                "requires": entry.get("requires", []),
                            }
                            for entry in self.catalog["plugins"]
                        ]
                    }
                )
            return json.dumps(self.catalog)
        return "f" * 40

    def verify(self, host: Host = Host.CODEX) -> dict[str, Any]:
        result = admission.verify(self.project, host, "fixture", self.roots)
        assert result is not None
        return result

    def bind(
        self, host: Host = Host.CODEX, roots: dict[str, Path] | None = None
    ) -> None:
        for root in (roots or self.roots).values():
            admission.gate(self.project, host, "session", "SessionStart", root)


@pytest.fixture
def case() -> Iterator[AdmissionFixture]:
    fixture = AdmissionFixture()
    try:
        yield fixture
    finally:
        fixture.stack.close()
        fixture.temporary.cleanup()


def test_exact_pair_and_all_guards_admit_both_hosts_without_event_native_calls(
    case: AdmissionFixture,
) -> None:
    for host in Host:
        document = case.verify(host)
        assert document["producers"] == {
            "tj-agents/agent": "a" * 40,
            "tj-agents/core": "c" * 40,
        }
        assert document["guards"] == ["agent@tj-agents", "base@tj-agents"]
        case.bind(host)
        with (
            patch.object(
                required_plugins,
                "inventory",
                side_effect=AssertionError("event inventory"),
            ),
            patch.object(
                plugin_refresh, "package_tree", side_effect=AssertionError("event hash")
            ),
        ):
            for event in ("UserPromptSubmit", "PreToolUse"):
                admission.gate(
                    case.project, host, "session", event, case.roots["agent@tj-agents"]
                )


def test_both_mixed_directions_block_and_interrupted_retry_completes(
    case: AdmissionFixture,
) -> None:
    for identity in case.roots:
        changed = case.roots[identity] / "value.txt"
        changed.write_text("old publication")
        with pytest.raises(admission.AdmissionError, match="differs from selected"):
            case.verify()
        changed.write_text("selected")
        assert case.verify() is not None
        admission.receipt_path(case.project, Host.CODEX).unlink()


def test_partial_install_cannot_create_receipt_and_retry_succeeds(
    case: AdmissionFixture,
) -> None:
    base = case.roots.pop("base@tj-agents")
    with pytest.raises(admission.AdmissionError, match="partially installed"):
        case.verify()
    assert not admission.receipt_path(case.project, Host.CODEX).exists()
    case.roots["base@tj-agents"] = base
    assert case.verify() is not None


def test_session_start_does_not_wait_on_other_guards_but_ordinary_work_does(
    case: AdmissionFixture,
) -> None:
    case.verify()
    agent = case.roots["agent@tj-agents"]
    admission.gate(case.project, Host.CODEX, "session", "SessionStart", agent)
    with pytest.raises(OSError):
        admission.gate(case.project, Host.CODEX, "session", "PreToolUse", agent)
    admission.gate(
        case.project,
        Host.CODEX,
        "session",
        "SessionStart",
        case.roots["base@tj-agents"],
    )
    admission.gate(case.project, Host.CODEX, "session", "PreToolUse", agent)


def test_old_snapshot_cannot_borrow_new_receipt(case: AdmissionFixture) -> None:
    case.verify()
    snapshot = case.root / "snapshot"
    shutil.copytree(case.roots["agent@tj-agents"], snapshot)
    (snapshot / ".snapshot-digest").write_text("sha256:" + "0" * 64)
    (snapshot / "value.txt").write_text("old publication")
    with pytest.raises(admission.AdmissionError, match="snapshot bytes"):
        admission.gate(case.project, Host.CODEX, "session", "SessionStart", snapshot)


def test_exact_snapshot_binds_once_then_cheap_event_checks(
    case: AdmissionFixture,
) -> None:
    case.verify()
    snapshot = case.root / "snapshot"
    shutil.copytree(case.roots["agent@tj-agents"], snapshot)
    (snapshot / ".snapshot-digest").write_text("sha256:" + "0" * 64)
    cache = snapshot / "__pycache__/module.cpython-312.pyc"
    cache.parent.mkdir()
    cache.write_bytes(b"generated before binding")
    case.bind(roots={"agent": snapshot, "base": case.roots["base@tj-agents"]})
    cache.write_bytes(b"generated after binding")
    with patch.object(
        plugin_refresh, "package_tree", side_effect=AssertionError("event hash")
    ):
        admission.gate(case.project, Host.CODEX, "session", "PreToolUse", snapshot)


def test_expired_and_package_hook_catalog_or_configuration_changes_invalidate(
    case: AdmissionFixture,
) -> None:
    case.verify()
    with (
        patch.object(
            time,
            "time",
            return_value=time.time() + admission.MAX_AGE + 1,
        ),
        pytest.raises(admission.AdmissionError, match="expired"),
    ):
        admission.cached(case.project, Host.CODEX)
    for path in (
        case.roots["agent@tj-agents"] / "value.txt",
        case.roots["base@tj-agents"] / "hooks/codex.json",
        case.market / host_plugins.PLUGINS[Host.CODEX].CATALOG,
        case.project / ".codex/config.toml",
    ):
        previous = path.read_bytes() if path.is_file() else None
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("changed")
        with pytest.raises(admission.AdmissionError, match="identity changed"):
            admission.cached(case.project, Host.CODEX)
        if previous is not None:
            path.write_bytes(previous)
        else:
            path.unlink()
        admission.receipt_path(case.project, Host.CODEX).unlink()
        case.verify()


def test_stale_catalog_and_unpinned_or_missing_guard_refuse(
    case: AdmissionFixture,
) -> None:
    with (
        patch.object(
            plugin_refresh,
            "run",
            side_effect=lambda command, project: (
                "e" * 40 if "ls-remote" in command else case.git(command, project)
            ),
        ),
        pytest.raises(admission.AdmissionError, match="stale"),
    ):
        case.verify()
    case.catalog["plugins"][0]["source"].pop("sha")
    case.save_catalog()
    with pytest.raises(admission.AdmissionError, match="immutable"):
        case.verify()
    case.catalog["plugins"][0] = case.entry("agent", "agent", "a")
    case.save_catalog()
    (case.roots["base@tj-agents"] / "hooks/codex.json").write_text(
        json.dumps({"hooks": {}})
    )
    case.expected["base"] = plugin_refresh.package_tree(case.roots["base@tj-agents"])
    with pytest.raises(admission.AdmissionError, match="guards"):
        case.verify()


def test_ambiguous_inventory_refuses(case: AdmissionFixture) -> None:
    with (
        patch.object(required_plugins, "inventory", return_value=case.inventory() * 2),
        pytest.raises(admission.AdmissionError, match="ambiguous"),
    ):
        case.verify()


def test_old_updater_obeys_selected_agent_source_and_unpinned_catalog_refuses(
    case: AdmissionFixture,
) -> None:
    with (
        patch.object(
            plugin_autoupdate, "marketplace_manifest", return_value=case.catalog
        ),
        patch.object(sources, "tree", return_value={}) as tree,
    ):
        plugin_autoupdate.published_agent_tree()
        assert tree.call_args.args[0]["sha"] == "a" * 40
        assert "ref" not in tree.call_args.args[0]
    case.catalog["plugins"][0]["source"].pop("sha")
    with (
        patch.object(
            plugin_autoupdate, "marketplace_manifest", return_value=case.catalog
        ),
        pytest.raises(admission.AdmissionError),
    ):
        plugin_autoupdate.published_agent_tree()


def test_refresh_maintenance_bypasses_admission_and_startup_failure_is_specific(
    case: AdmissionFixture,
) -> None:
    reload_plugins = importlib.import_module("reload_plugins")
    with (
        patch.object(required_plugins, "reconcile_selection"),
        patch.object(required_plugins, "check", return_value=case.roots) as check,
    ):
        reload_plugins.prepare_required(case.project, Host.CODEX, "fixture")
        assert not check.call_args.kwargs["admission"]
    binding = importlib.import_module("startup_binding")
    with (
        patch.object(
            binding.launch_check,
            "prepare_launch_roots",
            side_effect=admission.AdmissionError("partial publication"),
        ),
        patch.object(binding, "run_session") as launch,
        contextlib.redirect_stderr(io.StringIO()) as output,
    ):
        assert (
            binding.main(
                [
                    "--host",
                    "codex",
                    "--project",
                    str(case.project),
                    "--target",
                    "fixture",
                ]
            )
            == 1
        )
        launch.assert_not_called()
        assert "managed admission refused" in output.getvalue()


def test_host_denial_shapes_and_timeout_are_fail_closed(case: AdmissionFixture) -> None:
    prompt = admission.denial("UserPromptSubmit", "pending")
    assert prompt["hookSpecificOutput"]["additionalContext"] == "pending"
    assert "decision" not in prompt
    denied = admission.denial("PreToolUse", "pending")
    assert denied["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "continue" not in denied
    payload = json.dumps(
        {
            "hook_event_name": "PreToolUse",
            "session_id": "session",
            "cwd": str(case.project),
        }
    )
    with (
        patch.object(sys, "argv", ["guard", "hook", "--host", "codex"]),
        patch.object(sys, "stdin", io.StringIO(payload)),
        patch.object(threading.Thread, "start"),
        patch.object(queue.Queue, "get", side_effect=queue.Empty),
        contextlib.redirect_stdout(io.StringIO()) as output,
    ):
        assert admission.main() == 0
        assert (
            json.loads(output.getvalue())["hookSpecificOutput"]["permissionDecision"]
            == "deny"
        )


def test_setup_check_contract_reaches_the_shared_admission_checker(
    case: AdmissionFixture,
) -> None:
    with (
        patch.object(
            sys,
            "argv",
            [
                "guard",
                "setup",
                "--check",
                "--host",
                "codex",
                "--project",
                str(case.project),
            ],
        ),
        patch.object(admission, "verify", return_value=None) as verify,
    ):
        assert admission.main() == 0
        verify.assert_called_once_with(case.project, Host.CODEX)


def test_native_in_use_markers_do_not_invalidate_admission(
    case: AdmissionFixture,
) -> None:
    case.verify()
    marker = case.roots["agent@tj-agents"] / ".in_use/123"
    marker.parent.mkdir()
    marker.write_text("native session", encoding="utf-8")
    admission.cached(case.project, Host.CODEX)
    case.bind()
    marker.write_text("updated marker", encoding="utf-8")
    admission.gate(
        case.project, Host.CODEX, "session", "PreToolUse", case.roots["agent@tj-agents"]
    )


def test_required_check_propagates_specific_admission_failure(
    case: AdmissionFixture,
) -> None:
    with (
        patch.object(required_plugins, "inventory_roots", return_value=case.roots),
        patch.object(
            admission, "verify", side_effect=admission.AdmissionError("partial pair")
        ),
        pytest.raises(admission.AdmissionError, match="partial pair"),
    ):
        required_plugins.check(case.project, Host.CODEX, "fixture")


def test_launch_check_refuses_specific_admission_failure(
    case: AdmissionFixture,
) -> None:
    launch = importlib.import_module("launch_check")
    with (
        patch.object(
            launch,
            "prepare_launch",
            side_effect=admission.AdmissionError("partial pair"),
        ),
        contextlib.redirect_stderr(io.StringIO()) as output,
    ):
        assert (
            launch.main(
                [
                    "--host",
                    "codex",
                    "--project",
                    str(case.project),
                    "--target",
                    "fixture",
                ]
            )
            == 1
        )
        assert "launching anyway" not in output.getvalue()


def test_standalone_relevant_selection_does_not_force_unselected_packages(
    case: AdmissionFixture,
) -> None:
    root = case.root / "docs"
    shutil.copytree(case.roots["base@tj-agents"], root)
    harness = admission.read(root / "harness.json")
    harness["plugin"] = "tj-agents/docs"
    (root / "harness.json").write_text(json.dumps(harness), encoding="utf-8")
    for host in Host:
        path = root / host.manifest_directory / "plugin.json"
        manifest = admission.read(path)
        manifest["name"] = "docs"
        path.write_text(json.dumps(manifest), encoding="utf-8")
    case.catalog["plugins"].append(case.entry("docs", "core", "c"))
    case.save_catalog()
    case.expected["docs"] = plugin_refresh.package_tree(root)
    case.roots["docs@tj-agents"] = root
    (case.project / ".agents/plugins.json").write_text(
        json.dumps({"schema_version": 1, "plugins": ["docs@tj-agents"]}),
        encoding="utf-8",
    )
    document = case.verify()
    assert set(document["packages"]) == {"docs@tj-agents"}
    assert document["guards"] == ["docs@tj-agents"]
    admission.gate(case.project, Host.CODEX, "session", "SessionStart", root)
    admission.gate(case.project, Host.CODEX, "session", "PreToolUse", root)


def test_agent_selection_requires_declared_base_closure_without_reverse_dependency(
    case: AdmissionFixture,
) -> None:
    case.catalog["plugins"][0]["requires"] = ["base"]
    case.save_catalog()
    selection = case.project / ".agents/plugins.json"
    selection.write_text(
        json.dumps({"schema_version": 1, "plugins": ["agent@tj-agents"]}),
        encoding="utf-8",
    )
    base = case.roots.pop("base@tj-agents")
    with pytest.raises(admission.AdmissionError, match=r"partially installed.*base"):
        case.verify()
    case.roots["base@tj-agents"] = base
    assert set(case.verify()["guards"]) == {"agent@tj-agents", "base@tj-agents"}
    admission.receipt_path(case.project, Host.CODEX).unlink()
    selection.write_text(
        json.dumps({"schema_version": 1, "plugins": ["base@tj-agents"]}),
        encoding="utf-8",
    )
    case.roots.pop("agent@tj-agents")
    assert case.verify()["guards"] == ["base@tj-agents"]


def test_package_mutation_during_verification_cannot_publish_receipt(
    case: AdmissionFixture,
) -> None:
    original = plugin_refresh.package_tree

    def changed(root: Path) -> dict[str, bytes]:
        tree: dict[str, bytes] = original(root)
        (root / "value.txt").write_text("raced publication", encoding="utf-8")
        return tree

    with (
        patch.object(plugin_refresh, "package_tree", side_effect=changed),
        pytest.raises(admission.AdmissionError, match="changed during admission"),
    ):
        case.verify()
    assert not admission.receipt_path(case.project, Host.CODEX).exists()


def test_unrelated_selected_stack_follows_frozen_relevant_dependency_closure(
    case: AdmissionFixture,
) -> None:
    cpp = case.root / "cpp"
    shutil.copytree(case.roots["base@tj-agents"], cpp)
    harness = admission.read(cpp / "harness.json")
    harness["plugin"] = "tj-agents/cpp"
    (cpp / "harness.json").write_text(json.dumps(harness), encoding="utf-8")
    for host in Host:
        manifest = cpp / host.manifest_directory / "plugin.json"
        document = admission.read(manifest)
        document["name"] = "cpp"
        manifest.write_text(json.dumps(document), encoding="utf-8")
    case.roots["cpp@tj-agents"] = cpp
    case.catalog["plugins"].append(
        {
            "name": "cpp",
            "requires": ["agent"],
            "source": {
                "source": "git-subdir",
                "url": "https://github.com/tj-agents/cpp.git",
                "path": "./plugins/cpp",
                "ref": "main",
            },
        }
    )
    case.catalog["plugins"][0]["requires"] = ["base"]
    case.save_catalog()
    (case.project / ".agents/plugins.json").write_text(
        json.dumps({"schema_version": 1, "plugins": ["cpp@tj-agents"]}),
        encoding="utf-8",
    )
    document = case.verify()
    assert set(document["guards"]) == {"agent@tj-agents", "base@tj-agents"}
    assert document["freshness"]["cpp@tj-agents"] == content.digest(
        plugin_refresh.package_tree(cpp)
    )
    (cpp / "value.txt").write_text("changed", encoding="utf-8")
    with pytest.raises(admission.AdmissionError, match="identity changed"):
        admission.cached(case.project, Host.CODEX)


@pytest.mark.parametrize("host", list(Host))
@pytest.mark.parametrize("startup", [False, True])
@pytest.mark.parametrize("failure", ["timeout", "invalid-json"])
def test_managed_inventory_failure_blocks_before_native_launch(
    case: AdmissionFixture, host: Host, startup: bool, failure: str
) -> None:
    launch = importlib.import_module("launch_check")
    binding = importlib.import_module("startup_binding")
    result = subprocess.CompletedProcess(["fixture"], 0, "invalid", "")
    with (
        patch.object(required_plugins, "reconcile_selection"),
        patch.object(required_plugins, "inventory", NATIVE_INVENTORY),
        patch.object(
            required_plugins.subprocess,
            "run",
            side_effect=subprocess.TimeoutExpired("fixture", 20)
            if failure == "timeout"
            else None,
            return_value=result,
        ),
        patch.object(binding, "run_session") as native,
        patch.object(admission, "verify") as verify,
        contextlib.redirect_stderr(io.StringIO()) as output,
    ):
        entrypoint = binding.main if startup else launch.main
        assert (
            entrypoint(
                [
                    "--host",
                    host.value,
                    "--project",
                    str(case.project),
                    "--target",
                    "fixture",
                ]
            )
            == 1
        )
        native.assert_not_called()
        verify.assert_not_called()
        assert "Managed readiness" in output.getvalue()


def test_unrelated_inventory_failure_preserves_degraded_launch(
    case: AdmissionFixture,
) -> None:
    launch = importlib.import_module("launch_check")
    (case.project / ".agents/plugins.json").write_text(
        json.dumps({"schema_version": 1, "plugins": ["other@example"]}),
        encoding="utf-8",
    )
    with (
        patch.object(required_plugins, "reconcile_selection"),
        patch.object(
            required_plugins,
            "inventory_roots",
            side_effect=required_plugins.RequiredPluginsError("inventory unavailable"),
        ),
        contextlib.redirect_stderr(io.StringIO()) as output,
    ):
        assert (
            launch.main(
                [
                    "--host",
                    "codex",
                    "--project",
                    str(case.project),
                    "--target",
                    "fixture",
                ]
            )
            == 0
        )
        assert "launching anyway" in output.getvalue()


@pytest.mark.parametrize("host", list(Host))
def test_native_readiness_receives_every_admitted_dependency(
    case: AdmissionFixture, host: Host
) -> None:
    case.catalog["plugins"][0]["requires"] = ["base"]
    case.save_catalog()
    (case.project / ".agents/plugins.json").write_text(
        json.dumps({"schema_version": 1, "plugins": ["agent@tj-agents"]}),
        encoding="utf-8",
    )
    with patch.object(
        host_plugins.PLUGINS[host], "ensure_hooks", return_value=None
    ) as ready:
        document = case.verify(host)
        assert ready.call_args.kwargs["required_roots"] == case.roots
        assert set(document["guards"]) == set(case.roots)


def test_codex_default_readiness_retains_cached_admitted_dependencies(
    case: AdmissionFixture,
) -> None:
    document = case.verify()
    with (
        patch.object(
            required_plugins,
            "check",
            return_value={"agent@tj-agents": case.roots["agent@tj-agents"]},
        ),
        patch.object(admission, "verify", return_value=document),
        patch.object(
            codex_plugins.hook_trust, "required_definitions", return_value=["guard"]
        ),
        patch.object(codex_plugins.hook_trust, "apply_trust") as trust,
    ):
        assert NATIVE_ENSURE_HOOKS(case.project, "fixture") is None
        assert set(trust.call_args.kwargs["required"]) == set(case.roots)


@pytest.mark.parametrize("startup", [False, True])
def test_later_managed_hook_failure_blocks_launch_and_acknowledgement(
    case: AdmissionFixture, startup: bool
) -> None:
    launch = importlib.import_module("launch_check")
    binding = importlib.import_module("startup_binding")
    with (
        patch.object(
            launch, "prepare_launch_roots", return_value=(case.project, case.roots)
        ),
        patch.object(launch, "check", return_value="current"),
        patch.object(
            codex_plugins,
            "ensure_hooks",
            side_effect=RuntimeError("readiness unavailable"),
        ),
        patch.object(binding, "run_session") as native,
        contextlib.redirect_stderr(io.StringIO()),
    ):
        entrypoint = binding.main if startup else launch.main
        assert (
            entrypoint(
                [
                    "--host",
                    "codex",
                    "--project",
                    str(case.project),
                    "--target",
                    "fixture",
                ]
            )
            == 1
        )
        native.assert_not_called()


@pytest.mark.parametrize("host", list(Host))
def test_host_adapter_owns_configuration_watches(
    case: AdmissionFixture, host: Host
) -> None:
    sentinel = case.root / "adapter-settings"
    with patch.object(
        host_plugins.PLUGINS[host], "configuration_paths", return_value=[sentinel]
    ):
        document = case.verify(host)
        assert str(sentinel) in document["watch"]
        assert admission.configuration_paths(case.project, host) == [
            *(
                root / ".agents/plugins.json"
                for root in (case.project, *case.project.parents)
            ),
            sentinel,
        ]


@pytest.mark.parametrize("host", list(Host))
def test_hook_reads_the_host_session_variable(
    case: AdmissionFixture, host: Host
) -> None:
    payload = json.dumps({"hook_event_name": "SessionStart", "cwd": str(case.project)})
    with (
        patch.object(sys, "argv", ["guard", "hook", "--host", host.value]),
        patch.object(sys, "stdin", io.StringIO(payload)),
        patch.dict(os.environ, {host.session_variable: "adapter-session"}),
        patch.object(
            admission, "package_root", return_value=case.roots["agent@tj-agents"]
        ),
        patch.object(admission, "gate") as gate,
    ):
        assert admission.main() == 0
        assert gate.call_args.args[2] == "adapter-session"


def test_generated_caches_do_not_break_full_admission_or_bound_hot_guards(
    case: AdmissionFixture,
) -> None:
    root = case.roots["agent@tj-agents"]
    cache = root / "__pycache__/module.cpython-312.pyc"
    cache.parent.mkdir()
    (root / "nested").mkdir()
    cache.write_bytes(b"ordinary generated cache")
    case.verify()
    case.bind()
    cache.write_bytes(b"updated generated cache")
    nested = root / "nested/__pycache__/module.cpython-312.pyc"
    nested.parent.mkdir(parents=True)
    nested.write_bytes(b"new cache")
    with (
        patch.object(
            plugin_refresh, "package_tree", side_effect=AssertionError("hot hash")
        ),
        patch.object(
            required_plugins, "inventory", side_effect=AssertionError("hot inventory")
        ),
    ):
        admission.gate(case.project, Host.CODEX, "session", "PreToolUse", root)
    admission.receipt_path(case.project, Host.CODEX).unlink()
    assert case.verify() is not None


@pytest.mark.parametrize(
    "relative",
    [
        "module.py",
        "module.pyc",
        "__pycache__/legacy.pyc",
        "__pycache__/unexpected.txt",
        "__pycache__/nested/module.pyc",
    ],
)
def test_source_standalone_bytecode_and_unexpected_cache_contents_invalidate(
    case: AdmissionFixture, relative: str
) -> None:
    case.verify()
    path = case.roots["agent@tj-agents"] / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"changed")
    with pytest.raises(admission.AdmissionError, match="identity changed"):
        admission.cached(case.project, Host.CODEX)
    with pytest.raises(admission.AdmissionError, match="differs from selected"):
        case.verify()


def test_marketplace_replacement_refs_cannot_substitute_the_frozen_catalog(
    case: AdmissionFixture,
) -> None:
    def git(*arguments: str) -> str:
        return subprocess.run(
            ["git", "--no-replace-objects", "-C", str(case.market), *arguments],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        ).stdout.strip()

    git("init")
    git("config", "user.name", "Fixture")
    git("config", "user.email", "fixture@example.test")
    roster = {"plugins": [{"name": entry["name"]} for entry in case.catalog["plugins"]]}
    (case.market / "plugins.json").write_text(json.dumps(roster), encoding="utf-8")
    git("add", ".")
    git("commit", "-m", "Original publication")
    original = git("rev-parse", "HEAD")
    original_catalog = json.dumps(case.catalog)
    case.catalog["plugins"][0]["source"]["sha"] = "b" * 40
    case.save_catalog()
    git("add", ".")
    git("commit", "-m", "Replacement publication")
    replacement = git("rev-parse", "HEAD")
    git("replace", original, replacement)
    git("checkout", "--detach", original)

    def publication(command: list[str], project: Path) -> str:
        if "ls-remote" in command:
            return original + " refs/heads/main"
        return subprocess.run(
            command,
            cwd=project,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        ).stdout

    with patch.object(plugin_refresh, "run", side_effect=publication):
        assert case.verify()["producers"]["tj-agents/agent"] == "a" * 40
        admission.receipt_path(case.project, Host.CODEX).unlink()
        case.save_catalog()
        assert (case.market / host_plugins.PLUGINS[Host.CODEX].CATALOG).read_text(
            encoding="utf-8"
        ) != original_catalog
        with pytest.raises(admission.AdmissionError, match="catalog differs"):
            case.verify()
    assert git("replace", "-l") == original
