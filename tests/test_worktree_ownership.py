import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

import agent_store
import pytest
import session_hook
from claude import launch as claude_launch
from codex import launch as codex_launch
from tj_agent.host import Host
from tj_agent.worktrees import Owner, OwnershipError, claim, current_owner, linked_root


@dataclass
class Store:
    host: Host
    launch: str | None = None
    native_id: str = "native"
    claims: list[str] = field(default_factory=list)
    corrupt: bool = False
    verbatim: bool = False

    @property
    def handle(self) -> str:
        return f"{self.host}:{self.native_id}"

    def run(self, *arguments: str) -> dict[str, object]:
        session = {
            "session": self.handle,
            "host": str(self.host),
            "native_id": self.native_id,
            "launch_id": self.launch,
        }
        if arguments[0] == "show":
            result: dict[str, object] = {"session": session}
            if "--launch-id" in arguments:
                result["launch"] = {
                    "launch_id": self.launch,
                    "host": str(self.host),
                    "session": "codex:other" if self.corrupt else self.handle,
                }
            return result
        if arguments[0] == "register":
            return {"session": session}
        assert arguments[0] == "claim-worktree"
        path = arguments[arguments.index("--path") + 1]
        assert arguments[arguments.index("--session") + 1] == self.handle
        assert ("--launch-id" in arguments) == (self.launch is not None)
        if self.launch:
            assert arguments[arguments.index("--launch-id") + 1] == self.launch
        self.claims.append(path)
        return {
            "worktree": {
                "path": "\\\\?\\" + path if self.verbatim else path,
                "session": self.handle,
                "launch_id": self.launch,
                "claimed_at": "2026-11-11T00:00:00Z",
            }
        }


def git(path: Path, *arguments: str) -> None:
    subprocess.run(
        ["git", "-C", str(path), *arguments],
        capture_output=True,
        text=True,
        check=True,
    )


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    primary = tmp_path / "primary repo"
    primary.mkdir()
    git(primary, "init", "-b", "main")
    git(
        primary,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.invalid",
        "commit",
        "--allow-empty",
        "-m",
        "Initial",
    )
    return primary


@pytest.mark.parametrize("host", list(Host))
@pytest.mark.parametrize("launch", [None, "launch-1"])
def test_current_owner_uses_exact_native_and_launch_binding(
    monkeypatch: pytest.MonkeyPatch, host: Host, launch: str | None
) -> None:
    store = Store(host, launch)
    monkeypatch.setattr(agent_store, "run", store.run)
    owner = current_owner({host.session_variable: "native"})
    assert owner == Owner(host, "native", launch)
    if launch:
        assert current_owner({"TJ_RUN_LAUNCH_ID": launch}) == owner


@pytest.mark.parametrize(
    "environ",
    [
        {"TJ_AGENT": "1"},
        {"CLAUDECODE": "1"},
        {"CODEX_THREAD_ID": "one", "CLAUDE_CODE_SESSION_ID": "two"},
    ],
)
def test_current_owner_refuses_missing_or_ambiguous_agent_identity(
    environ: dict[str, str],
) -> None:
    with pytest.raises(OwnershipError, match=r"identity|identities"):
        current_owner(environ)


def test_current_owner_allows_ordinary_human_worktree_creation() -> None:
    assert current_owner({}) is None


@pytest.mark.parametrize(
    "environ",
    [
        {"TJ_RUN_LAUNCH_ID": "launch-1"},
        {"CODEX_THREAD_ID": "native"},
    ],
)
def test_current_owner_refuses_inconsistent_store_binding(
    monkeypatch: pytest.MonkeyPatch,
    environ: dict[str, str],
) -> None:
    store = Store(Host.CODEX, "launch-1", corrupt=True)
    monkeypatch.setattr(agent_store, "run", store.run)
    with pytest.raises(OwnershipError, match="bindings do not match"):
        current_owner(environ)


def test_current_owner_does_not_fall_back_from_unknown_launch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unknown(*arguments: str) -> dict[str, object]:
        raise agent_store.AgentStoreError("not-found", "Unknown launch")

    monkeypatch.setattr(agent_store, "run", unknown)
    with pytest.raises(agent_store.AgentStoreError, match="Unknown launch"):
        current_owner({"TJ_RUN_LAUNCH_ID": "missing", "CODEX_THREAD_ID": "native"})


def test_claim_records_several_linked_roots_with_spaces(
    repository: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = Store(Host.CODEX, "launch-1")
    monkeypatch.setattr(agent_store, "run", store.run)
    owner = Owner(store.host, store.native_id, store.launch)
    roots = [tmp_path / "first linked", tmp_path / "second linked"]
    for index, root in enumerate(roots):
        git(repository, "worktree", "add", "-b", f"feature-{index}", str(root))
        assert claim(root, owner).path == str(root.resolve())
    assert store.claims == [str(root.resolve()) for root in roots]


def test_claim_refuses_primary_and_subdirectory_without_store_write(
    repository: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = Store(Host.CODEX)
    monkeypatch.setattr(agent_store, "run", store.run)
    root = tmp_path / "linked"
    git(repository, "worktree", "add", "-b", "feature", str(root))
    nested = root / "nested"
    nested.mkdir()
    for path in (repository, nested):
        with pytest.raises(OwnershipError, match="exact linked worktree root"):
            claim(path, Owner(Host.CODEX, "native", None))
    assert store.claims == []


@pytest.mark.skipif(sys.platform != "win32", reason="Windows verbatim path syntax")
def test_claim_accepts_store_canonical_windows_path_without_rewriting_it(
    repository: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "linked"
    git(repository, "worktree", "add", "-b", "feature", str(root))
    store = Store(Host.CODEX, verbatim=True)
    monkeypatch.setattr(agent_store, "run", store.run)
    result = claim(root, Owner(Host.CODEX, "native", None))
    assert result.path == "\\\\?\\" + str(root.resolve())
    assert Path(result.path).samefile(root)


@pytest.mark.parametrize("host", list(Host))
def test_session_start_claims_linked_worktree_after_registration(
    repository: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    host: Host,
) -> None:
    root = tmp_path / "linked"
    git(repository, "worktree", "add", "-b", "feature", str(root))
    store = Store(host)
    monkeypatch.setattr(agent_store, "run", store.run)
    session_hook.start(host, "native", str(root), {})
    assert store.claims == [str(root.resolve())]


def test_session_start_does_not_claim_primary_checkout(
    repository: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = Store(Host.CODEX)
    monkeypatch.setattr(agent_store, "run", store.run)
    session_hook.start(Host.CODEX, "native", str(repository), {})
    assert linked_root(repository) is None
    assert store.claims == []


def test_claude_exports_exact_identity_for_later_worktree_creation(
    tmp_path: Path,
) -> None:
    environment = tmp_path / "session environment"
    claude_launch.record_session_environment(
        "session 'with spaces",
        {
            "CLAUDE_ENV_FILE": str(environment),
        },
    )
    assert environment.read_text(encoding="utf-8") == (
        "export CLAUDE_CODE_SESSION_ID='session '\"'\"'with spaces'\n"
    )


def test_codex_does_not_write_claude_environment(tmp_path: Path) -> None:
    environment = tmp_path / "session environment"
    codex_launch.record_session_environment(
        "native",
        {
            "CLAUDE_ENV_FILE": str(environment),
        },
    )
    assert not environment.exists()
