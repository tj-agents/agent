import hashlib
import importlib.util
import json
import os
import py_compile
import shutil
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any
from unittest.mock import patch

import managed_admission as admission
import plugin_refresh
import pytest
from tj_agent.plugins import sources

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "plugins/agent/.agents/agent/runtime"
ENTRYPOINTS = {
    "managed_admission.py": "host_plugins",
    "launch_check.py": "host",
    "startup_binding.py": "launch_check",
    "reload_plugins.py": "agent_cli",
}
CODEX_PLUGINS = importlib.import_module("codex.plugins")
HOOK_TRUST = importlib.import_module("codex.hook_trust")


class TrustFixture:
    def __init__(self, root: Path) -> None:
        self.project = root.resolve()
        self.publisher = self.project / "publisher"
        self.source = self.publisher / "plugins/agent"
        self.installed = self.project / "installed"
        (self.project / ".agents").mkdir()
        (self.project / ".agents/plugins.json").write_text(
            json.dumps({"schema_version": 1, "plugins": ["agent@tj-agents"]}),
            encoding="utf-8",
        )
        (self.source / ".codex-plugin").mkdir(parents=True)
        (self.source / ".codex-plugin/plugin.json").write_text(
            json.dumps({"name": "agent", "hooks": "./hooks/codex.json"}),
            encoding="utf-8",
        )
        (self.source / "hooks").mkdir()
        (self.source / "hooks/codex.json").write_text(
            json.dumps(
                {
                    "hooks": {
                        "SessionStart": [
                            {
                                "hooks": [
                                    {"type": "command", "command": "python guard.py"}
                                ]
                            }
                        ]
                    }
                }
            ),
            encoding="utf-8",
        )
        (self.source / "guard.py").write_text("VALUE = 42\n", encoding="utf-8")
        for command in (
            ["init", "--quiet"],
            ["config", "user.name", "Fixture"],
            ["config", "user.email", "fixture@example.invalid"],
            ["add", "."],
            ["commit", "--quiet", "-m", "Published trust fixture"],
        ):
            self.git(command)
        self.revision = self.git(["rev-parse", "HEAD"])
        shutil.copytree(self.source, self.installed)
        self.hook = {
            "pluginId": "agent@tj-agents",
            "source": "plugin",
            "isManaged": False,
            "enabled": True,
            "trustStatus": "untrusted",
            "sourcePath": str(self.installed / "hooks/codex.json"),
            "key": "agent@tj-agents:hooks/codex.json:session_start:0:0",
            "currentHash": "sha256:" + "a" * 64,
            "eventName": "sessionStart",
            "matcher": None,
            "handlerType": "command",
            "command": "python guard.py",
        }
        self.writes: list[dict[str, Any]] = []

    def git(self, arguments: list[str]) -> str:
        return subprocess.run(
            ["git", "-C", str(self.publisher), *arguments],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
        ).stdout.strip()

    def native_request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if method == "config/read":
            return {"config": {}}
        if method == "hooks/list":
            return {"data": [{"cwd": str(self.project), "hooks": [dict(self.hook)]}]}
        assert method == "config/batchWrite"
        self.writes.append(params)
        self.hook["trustStatus"] = "trusted"
        return {}

    def native_run(self, arguments: list[str], cwd: Path, timeout: int = 45) -> str:
        if arguments[0] == "fixture-codex":
            assert arguments[1:] == ["plugin", "list", "--json"]
            return json.dumps(
                {
                    "installed": [
                        {
                            "pluginId": "agent@tj-agents",
                            "installed": True,
                            "enabled": True,
                            "marketplaceSource": {
                                "sourceType": "git",
                                "source": "https://github.com/tj-agents/marketplace.git",
                            },
                            "source": {
                                "source": "git-subdir",
                                "url": "https://github.com/tj-agents/agent.git",
                                "path": "plugins/agent",
                                "sha": self.revision,
                            },
                        }
                    ]
                }
            )
        if "fetch" in arguments:
            arguments = [*arguments[:-2], str(self.publisher), arguments[-1]]
        return subprocess.run(
            arguments,
            cwd=cwd,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=timeout,
        ).stdout.strip()

    def ensure_hooks(self) -> None:
        with (
            patch.object(HOOK_TRUST, "run", side_effect=self.native_run),
            patch.object(HOOK_TRUST, "AppServer") as server,
        ):
            server.return_value.__enter__.return_value.request.side_effect = (
                self.native_request
            )
            assert (
                CODEX_PLUGINS.ensure_hooks(
                    self.project,
                    "fixture-codex",
                    required_roots={"agent@tj-agents": self.installed},
                )
                is None
            )


@pytest.fixture
def trust_case(tmp_path: Path) -> TrustFixture:
    return TrustFixture(tmp_path)


@pytest.mark.parametrize("status", ["untrusted", "modified"])
def test_codex_readiness_trusts_committed_source_with_generated_cache(
    trust_case: TrustFixture, status: str
) -> None:
    trust_case.hook["trustStatus"] = status
    compiled = py_compile.compile(str(trust_case.installed / "guard.py"), doraise=True)
    assert compiled is not None
    cache = Path(compiled)
    assert cache.is_file()
    assert plugin_refresh.package_tree(
        trust_case.installed
    ) == plugin_refresh.package_tree(trust_case.source)
    trust_case.ensure_hooks()
    assert trust_case.writes == [
        {
            "edits": [
                {
                    "keyPath": "hooks.state",
                    "value": {
                        trust_case.hook["key"]: {
                            "trusted_hash": trust_case.hook["currentHash"]
                        }
                    },
                    "mergeStrategy": "upsert",
                }
            ],
            "reloadUserConfig": True,
        }
    ]


@pytest.mark.parametrize(
    "relative",
    [
        "guard.py",
        "guard.pyc",
        "__pycache__/legacy.pyc",
        "__pycache__/unexpected.py",
        "__pycache__/nested/guard.cpython-312.pyc",
    ],
)
def test_codex_readiness_rejects_source_and_unexpected_bytecode_changes(
    trust_case: TrustFixture, relative: str
) -> None:
    path = trust_case.installed / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"unpublished")
    with pytest.raises(RuntimeError, match="differs from its Git source"):
        trust_case.ensure_hooks()
    assert trust_case.writes == []


@pytest.fixture
def snapshot() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "coherence_snapshot", ROOT / ".codex/hooks/snapshot.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def poisoned_module(path: Path, mode: py_compile.PycInvalidationMode) -> None:
    path.write_text('print("CACHED"); raise SystemExit(0)\n', encoding="utf-8")
    before = path.stat()
    py_compile.compile(str(path), doraise=True, invalidation_mode=mode)
    path.write_text('print("SOURCE"); raise SystemExit(0)\n', encoding="utf-8")
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))


@pytest.mark.parametrize("entrypoint,dependency", ENTRYPOINTS.items())
@pytest.mark.parametrize(
    "mode",
    [
        py_compile.PycInvalidationMode.TIMESTAMP,
        py_compile.PycInvalidationMode.UNCHECKED_HASH,
    ],
)
@pytest.mark.parametrize("use_runpy", [False, True])
def test_managed_entrypoints_ignore_existing_bytecode(
    tmp_path: Path,
    entrypoint: str,
    dependency: str,
    mode: py_compile.PycInvalidationMode,
    use_runpy: bool,
) -> None:
    script = tmp_path / entrypoint
    shutil.copyfile(RUNTIME / entrypoint, script)
    poisoned_module(tmp_path / (dependency + ".py"), mode)
    arguments = [str(script)]
    if use_runpy:
        arguments = [
            "-c",
            f"import runpy,sys; sys.path.insert(0, {str(tmp_path)!r}); "
            f"runpy.run_path({str(script)!r}, run_name='__main__')",
        ]
    result = subprocess.run(
        [sys.executable, "-B", *arguments],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "SOURCE"


@pytest.mark.parametrize("entrypoint", ENTRYPOINTS)
def test_library_import_does_not_change_interpreter_cache_state(
    entrypoint: str,
) -> None:
    script = RUNTIME / entrypoint
    program = (
        f"import runpy,sys; sys.path.insert(0, {str(RUNTIME)!r}); "
        "before=(sys.pycache_prefix,sys.dont_write_bytecode); "
        f"runpy.run_path({str(script)!r},run_name='library_probe'); "
        "assert (sys.pycache_prefix,sys.dont_write_bytecode)==before"
    )
    result = subprocess.run(
        [sys.executable, "-c", program],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    "mode",
    [
        py_compile.PycInvalidationMode.TIMESTAMP,
        py_compile.PycInvalidationMode.UNCHECKED_HASH,
    ],
)
@pytest.mark.parametrize("reuse", [False, True])
def test_codex_bootstrap_executes_source_in_cold_and_reused_snapshots(
    tmp_path: Path, snapshot: Any, mode: py_compile.PycInvalidationMode, reuse: bool
) -> None:
    package = tmp_path / "agent/version"
    package.mkdir(parents=True)
    loader = package / "hooks/codex/snapshot.py"
    loader.parent.mkdir(parents=True)
    loader.write_bytes((ROOT / ".codex/hooks/snapshot.py").read_bytes())
    target = package / "probe.py"
    target.write_text("import payload\n", encoding="utf-8")
    poisoned_module(package / "payload.py", mode)
    digest = snapshot.digest_tree(package)
    snapshots = tmp_path / "snapshots"
    selected = snapshot.Snapshot.for_package(package, digest, snapshots)
    if reuse:
        selected.resolve()
        poisoned_module(selected.path / "payload.py", mode)
    bootstrap = (ROOT / ".codex/hooks/bootstrap.py").read_text(encoding="utf-8")
    bootstrap = bootstrap.replace(
        "LOADER_DIGEST",
        repr(hashlib.sha256(loader.read_bytes().replace(b"\r\n", b"\n")).hexdigest()),
    )
    environment = {**os.environ, "TJ_AGENTS_STARTUP_SNAPSHOT_DIRECTORY": str(snapshots)}
    result = subprocess.run(
        [
            sys.executable,
            "-B",
            "-c",
            bootstrap,
            str(package),
            digest,
            "agent",
            str(target),
        ],
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "SOURCE"
    assert selected.path.is_dir()


@pytest.mark.parametrize(
    "mode",
    [
        py_compile.PycInvalidationMode.TIMESTAMP,
        py_compile.PycInvalidationMode.UNCHECKED_HASH,
    ],
)
def test_claude_dispatcher_executes_source(
    tmp_path: Path, mode: py_compile.PycInvalidationMode
) -> None:
    dispatcher = tmp_path / "hook_dispatch.py"
    shutil.copyfile(ROOT / ".agents/hooks/hook_dispatch.py", dispatcher)
    target = tmp_path / "probe.py"
    target.write_text("import payload\n", encoding="utf-8")
    poisoned_module(tmp_path / "payload.py", mode)
    result = subprocess.run(
        [sys.executable, "-B", str(dispatcher), "--hook", str(target)],
        input=json.dumps({"hook_event_name": "SessionStart"}),
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "CACHED" not in result.stdout + result.stderr
    assert (
        json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"] == "SOURCE"
    )


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
def test_snapshot_identity_retains_source_and_unexpected_cache_contents(
    tmp_path: Path, snapshot: Any, relative: str
) -> None:
    before = snapshot.digest_tree(tmp_path)
    path = tmp_path / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"identity")
    assert snapshot.digest_tree(tmp_path) != before
    assert relative in plugin_refresh.package_tree(tmp_path)


def test_snapshot_copy_and_digest_ignore_only_generated_cache_bytes(
    tmp_path: Path, snapshot: Any
) -> None:
    package = tmp_path / "package"
    package.mkdir()
    (package / "module.py").write_text("value = 1\n", encoding="utf-8")
    digest = snapshot.digest_tree(package)
    cache = package / "__pycache__/module.cpython-312.pyc"
    cache.parent.mkdir()
    cache.write_bytes(b"cache")
    assert snapshot.digest_tree(package) == digest
    selected = snapshot.Snapshot.for_package(package, digest, tmp_path / "snapshots")
    selected.resolve()
    assert not (selected.path / cache.relative_to(package)).exists()
    assert selected.reuse()


@pytest.mark.parametrize(
    "relative", ["__pycache__", "__pycache__/module.pyc", ".in_use/hidden"]
)
def test_cache_and_native_marker_exclusions_do_not_hide_links(
    tmp_path: Path, snapshot: Any, relative: str
) -> None:
    package = tmp_path / "package"
    package.mkdir()
    target = tmp_path / "target"
    target.mkdir()
    link = package / relative
    link.parent.mkdir(parents=True, exist_ok=True)
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError as error:
        if os.name != "nt":
            pytest.skip(f"symbolic links unavailable: {error}")
        subprocess.run(
            ["cmd.exe", "/c", "mklink", "/J", str(link), str(target)],
            capture_output=True,
            check=True,
        )
    with pytest.raises(admission.AdmissionError, match="link"):
        admission.signatures([package])
    with pytest.raises(plugin_refresh.RefreshError, match="symlink"):
        plugin_refresh.package_tree(package)
    with pytest.raises(RuntimeError, match="link"):
        HOOK_TRUST.package_files(package)
    with pytest.raises(SystemExit):
        snapshot.digest_tree(package)
    with pytest.raises(SystemExit):
        snapshot.generated_caches(str(link.parent), [link.name])


def test_publisher_checkout_materializes_original_bytes_with_replacement_refs(
    tmp_path: Path,
) -> None:
    publisher = tmp_path / "publisher"
    publisher.mkdir()

    def run(command: list[str], project: Path) -> str:
        return subprocess.run(
            command,
            cwd=project,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        ).stdout.strip()

    def git(*arguments: str) -> str:
        return run(
            ["git", "--no-replace-objects", "-C", str(publisher), *arguments], tmp_path
        )

    git("init")
    git("config", "user.name", "Fixture")
    git("config", "user.email", "fixture@example.test")
    value = publisher / "plugins/agent/value.txt"
    value.parent.mkdir(parents=True)
    value.write_text("original", encoding="utf-8")
    git("add", ".")
    git("commit", "-m", "Original publication")
    original = git("rev-parse", "HEAD")
    value.write_text("replacement", encoding="utf-8")
    git("add", ".")
    git("commit", "-m", "Replacement publication")
    replacement = git("rev-parse", "HEAD")

    def fetch_local(command: list[str], project: Path) -> str:
        if "fetch" not in command:
            return run(command, project)
        checkout = Path(command[command.index("-C") + 1])
        prefix = ["git", "-C", str(checkout)]
        run([*prefix, "fetch", str(publisher), original], project)
        run([*prefix, "fetch", str(publisher), replacement], project)
        run([*prefix, "replace", original, replacement], project)
        (checkout / ".git/FETCH_HEAD").write_text(original + "\n", encoding="ascii")
        return ""

    source = {
        "source": "git-subdir",
        "url": "https://github.com/tj-agents/agent.git",
        "sha": original,
        "path": "plugins/agent",
    }
    with patch.object(plugin_refresh, "run", side_effect=fetch_local):
        assert sources.tree(source, publisher, tmp_path, tmp_path / "staging", {}) == {
            "value.txt": b"original"
        }


def test_host_adapters_share_the_canonical_cli_module() -> None:
    canonical = importlib.import_module("agent_cli")
    for host in ("claude", "codex"):
        adapter = importlib.import_module(host + ".plugins")
        assert adapter.agent_cli is canonical
        assert adapter.agent_cli.LaunchError is canonical.LaunchError


@pytest.mark.parametrize("platform", ["linux", "darwin"])
def test_observer_visibility_fixture_skips_windows_apis(platform: str) -> None:
    spec = importlib.util.spec_from_file_location(
        "portable_observer_fixture", ROOT / "tests/fixtures/hidden_observer.py"
    )
    assert spec is not None and spec.loader is not None
    fixture = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fixture)
    with (
        patch.object(sys, "platform", platform),
        patch.object(
            fixture.ctypes,
            "WinDLL",
            side_effect=AssertionError("Windows API"),
            create=True,
        ),
    ):
        assert fixture.visible_windows() == []
        assert fixture.console_state() == {
            "console": None,
            "visible": False,
            "windows": [],
        }
