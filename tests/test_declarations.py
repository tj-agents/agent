import json
import os
import subprocess
from pathlib import Path

import pytest
from tj_agent import declarations
from tj_agent.declarations import Employer, Origin

EMPLOYER = {"all": [{"remote_host": "example.test"}, {"remote": "^example-employer/"}]}


@pytest.fixture
def repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    root = tmp_path / "repository"
    subprocess.run(["git", "init", "--quiet", str(root)], check=True, timeout=15)
    return root


@pytest.fixture
def cache(tmp_path: Path) -> Path:
    root = tmp_path / "profile" / "plugins" / "cache"
    root.mkdir(parents=True)
    return root


def set_origin(root: Path, url: str) -> None:
    subprocess.run(
        ["git", "-C", str(root), "config", "remote.origin.url", url],
        check=True,
        timeout=15,
    )


def install(
    cache: Path, plugin: str, data: object, version: str = "1", **entry: str
) -> Path:
    payload = cache / "test-market" / plugin / version
    payload.mkdir(parents=True)
    (payload / "tier.json").write_text(json.dumps(data), encoding="utf-8")
    registry_path = cache.parent / "installed_plugins.json"
    registry = (
        json.loads(registry_path.read_text(encoding="utf-8"))
        if registry_path.exists()
        else {"plugins": {}}
    )
    registry["plugins"].setdefault(plugin + "@test-market", []).append(
        {"installPath": str(payload), **entry}
    )
    registry_path.write_text(json.dumps(registry), encoding="utf-8")
    return payload


@pytest.mark.parametrize(
    "url",
    [
        "https://example.test/owner/repo.git",
        "https://EXAMPLE.TEST./owner/repo.git",
        "ssh://git@example.test/owner/repo.git",
        "ssh://git@EXAMPLE.TEST:2222/owner/repo.git",
        "git@example.test:owner/repo.git",
        "git@example.test:/owner/repo.git",
        "git@EXAMPLE.TEST.:owner/repo.git",
    ],
)
def test_parse_origin_normalises_network_urls(url: str) -> None:
    assert declarations.parse_origin(url) == Origin("example.test", "owner/repo")


@pytest.mark.parametrize(
    "url",
    [
        "",
        "../owner/repo.git",
        "/owner/repo.git",
        "C:/owner/repo.git",
        "C:\\owner\\repo.git",
        "file:///example.test/owner/repo.git",
        "file://example.test/owner/repo.git",
        "example.test/owner/repo.git",
        "example.test:owner/repo.git",
        "https:///owner/repo.git",
        "https://example.test",
        " https://example.test/owner/repo.git",
        "https://example.test/owner/repo.git ",
        "ssh://git@example.test/",
        "https://example.test:/owner/repo.git",
        "https://example.test:bad/owner/repo.git",
        "https://example.test:70000/owner/repo.git",
        "https://example.test:0/owner/repo.git",
        "https://example.test/owner/repo.git?host=evil",
        "https://example.test/owner/repo.git#fragment",
        "https://example.test/owner/../repo.git",
        "https://example.test/owner//repo.git",
        "https://example..test/owner/repo.git",
        "https://%65xample.test/owner/repo.git",
        "https://[example.test/owner/repo.git",
        "ftp://example.test/owner/repo.git",
        "git@@example.test:owner/repo.git",
        "https://example.test/owner/repo.git\nsecond",
        "https://example.test/owner/repo.git\x00",
    ],
)
def test_parse_origin_rejects_local_and_malformed_urls(url: str) -> None:
    assert declarations.parse_origin(url) is None


@pytest.mark.parametrize(
    ("node", "valid"),
    [
        (EMPLOYER, True),
        ({"any": [{"remote": "^a/"}, {"remote_host": "example.test"}]}, True),
        ({"remote_host": "https://example.test"}, False),
        ({"remote": "["}, False),
        ({"all": []}, False),
        ({"file": "marker"}, False),
        ({"all": [{"remote": "^a/"}, {"file": "marker"}]}, False),
        ({"remote": "^a/", "remote_host": "example.test"}, False),
    ],
)
def test_origin_predicate_accepts_only_origin_operators(
    node: object, valid: bool
) -> None:
    assert declarations.origin_predicate(node) is valid


@pytest.mark.parametrize(
    ("origin", "evidence"),
    [
        (
            Origin("example.test", "example-employer/service"),
            ("origin host example.test", "origin example-employer/service"),
        ),
        (Origin("example.test.evil", "example-employer/service"), None),
        (Origin("example.test", "someone/service"), None),
        (None, None),
    ],
)
def test_origin_evidence_requires_every_part_of_all(
    origin: Origin | None, evidence: tuple[str, ...] | None
) -> None:
    assert declarations.origin_evidence(EMPLOYER, origin) == evidence


def test_origin_evidence_reports_each_matching_part_of_any() -> None:
    node = {"any": [{"remote_host": "other.test"}, {"remote": "^EXAMPLE-employer/"}]}
    assert declarations.origin_evidence(
        node, Origin("example.test", "example-employer/service")
    ) == ("origin example-employer/service",)


def test_repository_origin_reads_the_configured_origin(repository: Path) -> None:
    assert declarations.repository_origin(repository) is None
    set_origin(repository, "git@example.test:Owner/Repo.git")
    assert declarations.repository_origin(repository) == Origin(
        "example.test", "owner/repo"
    )


def test_cache_roots_promote_only_a_plugin_root_inside_a_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    cache = home / ".codex" / "plugins" / "cache"
    native = cache / "tj-agents" / "base" / "local"
    native.mkdir(parents=True)
    scratch = home / "scratch" / "engineering"
    scratch.mkdir(parents=True)
    for variable in (
        "CLAUDE_PLUGIN_ROOT",
        "PLUGIN_ROOT",
        "CLAUDE_CONFIG_DIR",
        "CODEX_HOME",
    ):
        monkeypatch.delenv(variable, raising=False)
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setenv("PLUGIN_ROOT", str(scratch))
    assert scratch.parents[2] not in declarations.cache_roots()
    monkeypatch.setenv("PLUGIN_ROOT", str(native))
    assert declarations.cache_roots()[0] == cache.resolve()


@pytest.mark.parametrize(
    "payload",
    [
        Path("tj-agents/agent/1.0.0"),
        Path("tj-agents/engineering/1.0.0/dependencies/agent"),
    ],
)
def test_cache_roots_put_the_running_payload_cache_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, payload: Path
) -> None:
    home = tmp_path / "home"
    own = home / ".codex" / "plugins" / "cache"
    module = (
        own / payload / ".agents" / "agent" / "runtime" / "tj_agent" / "declarations.py"
    )
    module.parent.mkdir(parents=True)
    (home / ".claude" / "plugins" / "cache").mkdir(parents=True)
    for variable in (
        "CLAUDE_PLUGIN_ROOT",
        "PLUGIN_ROOT",
        "CLAUDE_CONFIG_DIR",
        "CODEX_HOME",
    ):
        monkeypatch.delenv(variable, raising=False)
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setattr(declarations, "__file__", str(module))
    assert declarations.cache_roots() == [
        own.resolve(),
        (home / ".claude" / "plugins" / "cache").resolve(),
    ]


def test_installed_prefers_the_most_specific_project_scope(
    tmp_path: Path, cache: Path
) -> None:
    project = tmp_path / "repos" / "app"
    project.mkdir(parents=True)
    install(cache, "dotnet", {"tier": "dotnet"}, "user")
    install(
        cache,
        "dotnet",
        {"tier": "dotnet"},
        "outer",
        projectPath=str(tmp_path / "repos"),
    )
    install(cache, "dotnet", {"tier": "dotnet"}, "inner", projectPath=str(project))
    install(
        cache,
        "dotnet",
        {"tier": "dotnet"},
        "other",
        projectPath=str(tmp_path / "elsewhere"),
    )
    assert [
        item.path.parent.name
        for item in declarations.installed([cache], project / "src")
    ] == ["inner"]


def test_installed_never_lets_an_orphaned_version_shadow_a_live_one(
    tmp_path: Path,
) -> None:
    first, second = tmp_path / "first", tmp_path / "second"
    for root, version in ((first, "zzz-new"), (second, "live")):
        payload = root / "market" / "dotnet" / version
        payload.mkdir(parents=True)
        (payload / "tier.json").write_text("{}", encoding="utf-8")
    (first / "market" / "dotnet" / "zzz-new" / declarations.ORPHAN_MARKER).touch()
    (live,) = declarations.installed([first, second])
    assert (live.id, live.path.parent.name, live.orphaned) == (
        "dotnet@market",
        "live",
        False,
    )


def test_installed_employers_reads_live_origin_employer_declarations(
    tmp_path: Path, cache: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(declarations, "cache_roots", lambda: [cache])
    employer = {
        "schema_version": 3,
        "tier": "employer-a",
        "applies": "stack-present",
        "owner_repository": ["Publisher/Employer-A"],
        "detect": EMPLOYER,
        "employer": {"context_skill": "communication-context"},
    }
    install(cache, "employer-a", employer)
    install(
        cache,
        "stack",
        {
            "schema_version": 3,
            "tier": "stack",
            "applies": "stack-present",
            "detect": EMPLOYER,
        },
    )
    install(cache, "marker", dict(employer, detect={"file": "marker"}))
    orphaned = install(cache, "orphaned", employer)
    (orphaned / declarations.ORPHAN_MARKER).touch()
    assert declarations.installed_employers(tmp_path) == [
        Employer("employer-a", "test-market", EMPLOYER, ("publisher/employer-a",))
    ]


@pytest.mark.parametrize(
    ("url", "evidence"),
    [
        (
            "git@example.test:example-employer/service.git",
            ("origin host example.test", "origin example-employer/service"),
        ),
        (
            "https://github.com/publisher/example-employer.git",
            ("source repository publisher/example-employer",),
        ),
        ("git@example.test:someone/service.git", None),
    ],
)
def test_matching_employers_matches_the_origin_or_the_employers_own_repository(
    repository: Path, url: str, evidence: tuple[str, ...] | None
) -> None:
    employer = Employer(
        "example-employer", "test-market", EMPLOYER, ("publisher/example-employer",)
    )
    set_origin(repository, url)
    expected = [] if evidence is None else [(employer, evidence)]
    assert declarations.matching_employers(repository, [employer]) == expected
