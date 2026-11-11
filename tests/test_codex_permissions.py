import ntpath
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from types import SimpleNamespace
from typing import cast

import pytest
from codex import permissions
from launch_errors import LaunchError


def config(projects: dict[str, object]) -> dict[str, object]:
    return {"config": {"projects": projects}, "layers": []}


@pytest.fixture(autouse=True)
def no_git_process(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*args: object) -> None:
        pytest.fail("Permission tests must not invoke Git or Store processes")

    monkeypatch.setattr(permissions.worktrees, "repository_root", forbidden)
    monkeypatch.setattr(permissions.worktrees, "linked_root", forbidden)


@pytest.mark.parametrize("shadow", [{}, {"trust_level": "untrusted"}])
def test_cwd_project_shadows_trusted_repository(
    tmp_path: Path, shadow: dict[str, str]
) -> None:
    directory = tmp_path / "nested"
    directory.mkdir()
    document = config(
        {
            str(tmp_path): {"trust_level": "trusted"},
            str(directory): shadow,
        }
    )
    with pytest.raises(LaunchError, match="existing explicit project trust"):
        permissions.require_project_trust(document, directory, windows=False)


def test_windows_canonical_project_precedes_original_spelling(tmp_path: Path) -> None:
    canonical = "\\\\?\\" + str(tmp_path.resolve())
    document = config(
        {
            canonical: {"trust_level": "untrusted"},
            str(tmp_path): {"trust_level": "trusted"},
        }
    )
    with pytest.raises(LaunchError, match="existing explicit project trust"):
        permissions.require_project_trust(document, tmp_path, windows=True)


def test_windows_project_matching_is_ascii_only(tmp_path: Path) -> None:
    permissions.require_project_trust(
        config({str(tmp_path).upper(): {"trust_level": "trusted"}}),
        tmp_path,
        windows=True,
    )
    directory = tmp_path / "İ"
    directory.mkdir()
    with pytest.raises(LaunchError, match="explicit project trust"):
        permissions.require_project_trust(
            config(
                {
                    str(directory): {"trust_level": "untrusted"},
                    str(directory).lower(): {"trust_level": "trusted"},
                }
            ),
            directory,
            windows=True,
        )


def test_windows_project_refuses_multiple_case_aliases(tmp_path: Path) -> None:
    with pytest.raises(LaunchError, match="ambiguous"):
        permissions.require_project_trust(
            config(
                {
                    str(tmp_path).upper(): {"trust_level": "trusted"},
                    str(tmp_path).lower(): {"trust_level": "trusted"},
                }
            ),
            tmp_path,
            windows=True,
        )


def test_trust_uses_only_validated_nearest_main_repository(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    marker = tmp_path / ".git"
    marker.mkdir()
    (marker / "HEAD").write_text("ref: refs/heads/main", encoding="utf-8")
    directory = tmp_path / "nested"
    directory.mkdir()
    monkeypatch.setattr(permissions.worktrees, "repository_root", lambda _: tmp_path)
    permissions.require_project_trust(
        config({str(tmp_path): {"trust_level": "trusted"}}), directory, windows=False
    )
    (directory / ".git").mkdir()
    with pytest.raises(LaunchError, match="does not contain"):
        permissions.require_project_trust(
            config({str(tmp_path): {"trust_level": "trusted"}}),
            directory,
            windows=False,
        )


@dataclass(frozen=True, slots=True)
class Linked:
    main: Path
    checkout: Path
    registered: Path


@pytest.fixture
def linked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Linked:
    main, checkout = tmp_path / "main", tmp_path / "linked"
    common = main / ".git"
    registered = common / "worktrees" / "linked"
    registered.mkdir(parents=True)
    checkout.mkdir()
    (checkout / ".git").write_text(f"gitdir: {registered}", encoding="utf-8")
    (registered / "gitdir").write_text(str(checkout / ".git"), encoding="utf-8")
    (registered / "commondir").write_text("../..", encoding="utf-8")
    monkeypatch.setattr(permissions.worktrees, "repository_root", lambda _: checkout)
    monkeypatch.setattr(
        permissions.worktrees, "linked_root", lambda _: checkout.resolve()
    )
    return Linked(main, checkout, registered)


def test_linked_worktree_uses_exact_validated_main_root(linked: Linked) -> None:
    permissions.require_project_trust(
        config({str(linked.main): {"trust_level": "trusted"}}),
        linked.checkout,
        windows=False,
    )


@pytest.mark.parametrize("pointer", ["gitdir", "commondir"])
def test_linked_worktree_refuses_invalid_backlinks(
    linked: Linked, pointer: str
) -> None:
    (linked.registered / pointer).write_text(str(linked.main), encoding="utf-8")
    with pytest.raises(LaunchError, match="backlinks"):
        permissions.require_project_trust(
            config({str(linked.main): {"trust_level": "trusted"}}),
            linked.checkout,
            windows=False,
        )


def test_project_layer_presence_does_not_grant_trust(tmp_path: Path) -> None:
    document = {
        "config": {"projects": {}},
        "layers": [
            {
                "name": {"type": "project", "dotCodexFolder": str(tmp_path / ".codex")},
                "config": {},
            },
        ],
    }
    with pytest.raises(LaunchError, match="explicit project trust"):
        permissions.require_project_trust(document, tmp_path, windows=False)


@dataclass(frozen=True, slots=True)
class WindowsGit:
    checkout: PureWindowsPath
    main: PureWindowsPath
    registered: PureWindowsPath
    files: dict[str, str]
    directories: set[str]
    canonical: dict[str, str]


@pytest.fixture
def windows_git(monkeypatch: pytest.MonkeyPatch) -> WindowsGit:
    checkout, main = PureWindowsPath("C:/Agent/Case"), PureWindowsPath("C:/Agent/Main")
    registered = main / ".git/worktrees/Case"
    files = {
        str(checkout / ".git"): f"gitdir: {registered}",
        str(registered / "gitdir"): str(checkout / ".git"),
        str(registered / "commondir"): "../..",
    }
    directories = {str(main / ".git"), str(registered)}
    canonical: dict[str, str] = {}

    def resolve(path: PureWindowsPath, *, strict: bool = False) -> PureWindowsPath:
        normalized = ntpath.normpath(str(path))
        return PureWindowsPath(canonical.get(normalized, normalized))

    monkeypatch.setattr(PureWindowsPath, "resolve", resolve, raising=False)
    monkeypatch.setattr(PureWindowsPath, "absolute", resolve, raising=False)
    monkeypatch.setattr(PureWindowsPath, "is_symlink", lambda _: False, raising=False)
    monkeypatch.setattr(PureWindowsPath, "is_junction", lambda _: False, raising=False)
    monkeypatch.setattr(
        PureWindowsPath, "is_dir", lambda path: str(path) in directories, raising=False
    )
    monkeypatch.setattr(
        PureWindowsPath, "is_file", lambda path: str(path) in files, raising=False
    )
    monkeypatch.setattr(
        PureWindowsPath,
        "exists",
        lambda path: str(path) in files or str(path) in directories,
        raising=False,
    )
    monkeypatch.setattr(
        PureWindowsPath,
        "stat",
        lambda path: SimpleNamespace(st_size=len(files[str(path)])),
        raising=False,
    )
    monkeypatch.setattr(
        PureWindowsPath,
        "read_text",
        lambda path, **options: files[str(path)],
        raising=False,
    )
    monkeypatch.setattr(permissions.worktrees, "repository_root", lambda _: checkout)
    monkeypatch.setattr(permissions.worktrees, "linked_root", lambda _: checkout)
    return WindowsGit(checkout, main, registered, files, directories, canonical)


def test_windows_exact_canonical_worktree_identity_is_admitted(
    windows_git: WindowsGit,
) -> None:
    root = permissions.main_repository(cast(Path, windows_git.checkout))
    assert str(root) == str(windows_git.main)


@pytest.mark.parametrize(
    "mismatch", ["backlink", "common", "main", "linked_root", "nearest", "containment"]
)
def test_windows_case_sensitive_worktree_identity_refuses_folded_matches(
    windows_git: WindowsGit, mismatch: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    checkout, registered = windows_git.checkout, windows_git.registered
    distinct = checkout.parent / "case"
    assert checkout == distinct and str(checkout) != str(distinct)
    directory = checkout
    if mismatch == "backlink":
        windows_git.files[str(registered / "gitdir")] = str(distinct / ".git")
    elif mismatch == "common":
        windows_git.files[str(registered / "commondir")] = str(
            windows_git.main / ".GIT"
        )
    elif mismatch == "main":
        other = windows_git.main / ".GIT/worktrees/Case"
        windows_git.files[str(checkout / ".git")] = f"gitdir: {other}"
        windows_git.files[str(other / "gitdir")] = str(checkout / ".git")
        windows_git.files[str(other / "commondir")] = "../.."
        windows_git.directories.add(str(other))
    elif mismatch == "linked_root":
        monkeypatch.setattr(permissions.worktrees, "linked_root", lambda _: distinct)
    elif mismatch == "nearest":
        monkeypatch.setattr(
            permissions.worktrees, "repository_root", lambda _: distinct
        )
    elif mismatch == "containment":
        directory = checkout / "nested"
        windows_git.canonical[str(directory)] = str(distinct / "nested")
    with pytest.raises(LaunchError, match=r"backlinks|does not contain"):
        permissions.require_project_trust(
            config({str(windows_git.main): {"trust_level": "trusted"}}),
            cast(Path, directory),
            windows=True,
        )
