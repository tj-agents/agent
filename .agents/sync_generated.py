#!/usr/bin/env python3
"""Generate a plugin repository's host discovery entries, marketplaces and
installable payloads."""

import argparse
import ast
import hashlib
import json
import posixpath
import re
import shutil
import stat
import subprocess
import tomllib
from collections.abc import Iterator
from pathlib import Path, PurePosixPath
from typing import Any, TypedDict

FRONTMATTER = re.compile(r"\A---\n(?P<header>.*?)\n---\n(?P<body>.*)\Z", re.DOTALL)
NAME = re.compile(r"^[a-z][a-z0-9-]*$")
KIND = re.compile(r"^[a-z]+$")
REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]+/([a-z][a-z0-9-]*)$")
DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
URL_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")
BARE_SKILL_REFERENCE = re.compile(r"`([a-z][a-z0-9-]+)`\s+skill(?:'s)?")
MARKDOWN_LINK = re.compile(r"\]\(([^)\s]+)\)")
SKILL_DIRECTORY_REFERENCE = re.compile(r"<skill-directory>/([^\s`'\")\]]+)")
REQUIRED_METADATA = (
    "name",
    "description",
    "kind",
    "domain",
    "profile",
    "applicability",
    "requires",
    "provenance",
)
REPOSITORY_TYPES = ("stack", "tool", "utility", "profile")
RESERVED_AGENT_DIRS = {"continuation", "plugins", "tiers", "tests", "hooks"}
IGNORED_PARTS = {"__pycache__"}
IGNORED_SUFFIXES = {".pyc"}
HOSTS = ("claude", "codex")
ADAPTER_ROOTS = {"claude": ".claude/skills", "codex": ".codex/skills"}
MANIFEST_ROOT = ".agents/plugins/manifests"
MARKETPLACE_OUTPUTS = {
    "claude": ".claude-plugin/marketplace.json",
    "codex": ".agents/plugins/marketplace.json",
}
MARKETPLACE_FIELDS = {
    "claude": {"description", "owner"},
    "codex": {"interface"},
}
CODEX_POLICY = {"installation": "INSTALLED_BY_DEFAULT", "authentication": "ON_INSTALL"}
CODEX_HOOK_LOADER = ".codex/hooks/snapshot.py"
CODEX_HOOK_BOOTSTRAP = ".codex/hooks/bootstrap.py"
BOOTSTRAP_DIGEST = "LOADER_DIGEST"
BOOTSTRAP_STATEMENTS = (ast.Import, ast.ImportFrom, ast.Assign, ast.Expr)
COMMAND_UNSAFE = frozenset('"$`\\\n\r')
HOST_ROOTS = (".claude", ".codex")
RESOURCE_ROOTS = (".agents", *HOST_ROOTS)
CODEX_HOOK_MANIFEST = "hooks/codex.json"
CODEX_HOOK_PACKAGE_LOADER = "hooks/codex/snapshot.py"
CODEX_WINDOWS_COMMAND_BUDGET = 8191
WINDOWS_MAX_PATH = 260
GENERATED_ROOTS = (
    ".claude/skills",
    ".codex/skills",
    "plugins",
    ".claude-plugin/marketplace.json",
    ".agents/plugins/marketplace.json",
    ".agents/INDEX.md",
)
PAYLOAD_FIELDS = {
    "payloads",
    "dependencies",
    "compatibilitySkillAliases",
    "resources",
    "host_adapters",
}
PLUGIN_RESOURCE_DIRS = {"runtime", "resources"}
TJ_AGENTS_MARKETPLACE = "tj-agents"
TJ_AGENTS_MARKETPLACE_REPOSITORY = "tj-agents/marketplace"
DEPENDENCY = re.compile(rf"^{TJ_AGENTS_MARKETPLACE}/[a-z0-9][a-z0-9-]*$")
CLAUDE_DEPENDENCY = re.compile(rf"^[a-z0-9][a-z0-9-]*(@{TJ_AGENTS_MARKETPLACE})?$")
STACK_CORE_DEPENDENCIES = (
    "tj-agents/base",
    "tj-agents/engineering",
)

type JSONObject = dict[str, Any]


class SkillRecord(TypedDict):
    plugin: str
    name: str
    kind: str
    body: str
    metadata: dict[str, str]
    directory: str
    files: list[str]


type PluginSkills = dict[str, SkillRecord]
type Plugins = dict[str, PluginSkills]
type Dependencies = dict[str, list[str]]


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8").replace("\r\n", "\n")


def load(path: Path) -> Any:
    try:
        return json.loads(read(path))
    except FileNotFoundError:
        raise ValueError(f"Missing {path.as_posix()}") from None
    except json.JSONDecodeError as error:
        raise ValueError(f"{path.as_posix()}: invalid JSON ({error.msg})") from None


def dump(value: Any) -> str:
    return json.dumps(value, indent=2, ensure_ascii=False) + "\n"


def ignored(path: PurePosixPath | Path) -> bool:
    return (
        bool(IGNORED_PARTS.intersection(path.parts)) or path.suffix in IGNORED_SUFFIXES
    )


def is_link_or_junction(path: Path) -> bool:
    if path.is_symlink():
        return True
    is_junction = getattr(path, "is_junction", None)
    if is_junction is not None and is_junction():
        return True
    try:
        attributes = getattr(path.lstat(), "st_file_attributes", 0)
    except FileNotFoundError:
        return False
    return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def load_config(root: Path) -> JSONObject:
    config = load(root / ".agents/plugins/template.json")
    if not isinstance(config, dict):
        raise ValueError(".agents/plugins/template.json: must be a JSON object")
    if config.get("schema_version") != 1:
        raise ValueError(".agents/plugins/template.json: schema_version must be 1")
    if config.get("type") not in REPOSITORY_TYPES:
        raise ValueError(
            ".agents/plugins/template.json: type must be one of "
            f"{', '.join(REPOSITORY_TYPES)}"
        )
    match = REPOSITORY.fullmatch(str(config.get("repository", "")))
    if match is None:
        raise ValueError(
            ".agents/plugins/template.json: repository must be owner/name "
            "with a lowercase name"
        )
    unknown = set(config) - {"schema_version", "type", "repository"}
    if unknown:
        raise ValueError(
            f".agents/plugins/template.json: unknown fields {sorted(unknown)}"
        )
    return {**config, "namespace": match.group(1)}


def metadata(body: str, source: str) -> dict[str, str]:
    match = FRONTMATTER.match(body)
    if match is None:
        raise ValueError(f"{source}: missing frontmatter")
    values: dict[str, str] = {}
    for line in match.group("header").splitlines():
        if ":" in line:
            key, value = line.split(":", 1)
            values[key.strip()] = value.strip()
    for field in REQUIRED_METADATA:
        if not values.get(field):
            raise ValueError(f"{source}: missing {field}")
    for field in ("name", "profile"):
        if not NAME.fullmatch(values[field]):
            raise ValueError(f"{source}: {field} must be a lowercase name")
    if not KIND.fullmatch(values["kind"]):
        raise ValueError(f"{source}: kind must be one lowercase word")
    return values


def is_skill_folder(path: Path) -> bool:
    return (
        path.is_dir()
        and not ignored(path)
        and not is_link_or_junction(path)
        and (path / "SKILL.md").is_file()
    )


def load_skill(
    root: Path, plugin: str, kind: str, directory: Path, name: str, namespace: str
) -> SkillRecord:
    path = directory / "SKILL.md"
    relative = path.relative_to(root).as_posix()
    raw = path.read_bytes()
    if b"\xef\xbb\xbf" in raw:
        raise ValueError(f"{relative}: embedded UTF-8 BOM")
    body = raw.decode("utf-8").replace("\r\n", "\n")
    values = metadata(body, relative)
    if values["name"] != name:
        raise ValueError(
            f"{relative}: name must be {name}, its folder path below the kind "
            "folder joined by hyphens"
        )
    if values["kind"] != kind:
        raise ValueError(f"{relative}: kind must equal its kind folder")
    if values["domain"] != namespace:
        raise ValueError(f"{relative}: domain must be {namespace}")
    members = {item.name for item in directory.iterdir() if is_skill_folder(item)}
    files: list[str] = []
    for item in sorted(directory.rglob("*")):
        part = item.relative_to(directory)
        if ignored(part) or part.parts[0] in members:
            continue
        if is_link_or_junction(item):
            raise ValueError(
                f"{item.relative_to(root).as_posix()}: links are not shipped"
            )
        if item.is_file() and item != path:
            files.append(part.as_posix())
    return {
        "plugin": plugin,
        "name": values["name"],
        "kind": kind,
        "body": body,
        "metadata": values,
        "directory": directory.relative_to(root).as_posix(),
        "files": files,
    }


def collect_skills(
    root: Path,
    plugin: str,
    kind: str,
    directory: Path,
    family: tuple[str, ...],
    namespace: str,
    skills: PluginSkills,
) -> None:
    location = directory.relative_to(root).as_posix()
    if is_link_or_junction(directory):
        raise ValueError(f"{location}: links are not shipped")
    if not directory.is_dir() or not NAME.fullmatch(directory.name):
        raise ValueError(
            f"{location}: kind and family folders hold only skill folders "
            "and family folders"
        )
    parts = (*family, directory.name)
    if (directory / "SKILL.md").is_file():
        skill = load_skill(root, plugin, kind, directory, "-".join(parts), namespace)
        if skill["name"] in skills:
            raise ValueError(f"{plugin}: duplicate skill {skill['name']}")
        skills[skill["name"]] = skill
        members = [
            item for item in sorted(directory.iterdir()) if is_skill_folder(item)
        ]
    else:
        members = sorted(path for path in directory.iterdir() if not ignored(path))
        if not members:
            raise ValueError(f"{location}: a family folder holds skill folders")
    for member in members:
        collect_skills(root, plugin, kind, member, parts, namespace, skills)


def resource_source_parts(source: str) -> tuple[str | None, str]:
    path = PurePosixPath(source)
    if (
        "\\" in source
        or ":" in source
        or "\x00" in source
        or path.is_absolute()
        or ".." in path.parts
        or path.as_posix() != source
        or len(path.parts) < 2
        or path.parts[0] not in RESOURCE_ROOTS
    ):
        raise ValueError(
            f".agents/plugins/payloads.json: unsafe resource source {source!r}"
        )
    if path.parts[1] == "hooks":
        return None, "hooks"
    if path.parts[0] in HOST_ROOTS:
        if path.parts[1] == "runtime":
            return None, "runtime"
        raise ValueError(
            ".agents/plugins/payloads.json: a host resource source must be below "
            f"{path.parts[0]}/hooks or {path.parts[0]}/runtime"
        )
    if len(path.parts) >= 3 and NAME.fullmatch(path.parts[1]):
        return path.parts[1], path.parts[2]
    raise ValueError(
        ".agents/plugins/payloads.json: resource source must be .agents/hooks, "
        ".claude/hooks, .claude/runtime, .codex/hooks, .codex/runtime or a path "
        "below .agents/<plugin>"
    )


def mapped_resource_directories(payloads: Any) -> set[tuple[str, str]]:
    if not isinstance(payloads, dict):
        return set()
    resources = payloads.get("resources", [])
    if not isinstance(resources, list):
        raise ValueError(".agents/plugins/payloads.json: resources must be a list")
    mapped: set[tuple[str, str]] = set()
    for resource in resources:
        if isinstance(resource, dict) and set(resource) == {
            "plugin",
            "distribution",
            "destination",
        }:
            continue
        if not isinstance(resource, dict) or not isinstance(
            resource.get("source"), str
        ):
            raise ValueError(
                ".agents/plugins/payloads.json: each resource declares plugin, "
                "source and destination"
            )
        owner, directory = resource_source_parts(resource["source"])
        if owner is not None and directory in PLUGIN_RESOURCE_DIRS:
            mapped.add((owner, directory))
    return mapped


def discover(
    root: Path, namespace: str, mapped_resources: set[tuple[str, str]]
) -> Plugins:
    agents = root / ".agents"
    plugins: Plugins = {}
    for plugin_dir in sorted(
        path for path in agents.iterdir() if path.is_dir() and not ignored(path)
    ):
        if plugin_dir.name in RESERVED_AGENT_DIRS:
            continue
        plugin = plugin_dir.name
        if not NAME.fullmatch(plugin):
            raise ValueError(
                f".agents/{plugin}: a plugin folder must be a lowercase name"
            )
        skills: PluginSkills = {}
        for kind_dir in sorted(
            path for path in plugin_dir.iterdir() if not ignored(path)
        ):
            if kind_dir.name in PLUGIN_RESOURCE_DIRS:
                if (plugin, kind_dir.name) not in mapped_resources:
                    raise ValueError(
                        f".agents/{plugin}/{kind_dir.name}: non-skill resource "
                        "directories must be declared in payloads resources"
                    )
                continue
            if not kind_dir.is_dir() or not NAME.fullmatch(kind_dir.name):
                raise ValueError(
                    f".agents/{plugin}/{kind_dir.name}: only kind folders "
                    "belong in a plugin folder"
                )
            for entry in sorted(
                path for path in kind_dir.iterdir() if not ignored(path)
            ):
                collect_skills(
                    root, plugin, kind_dir.name, entry, (), namespace, skills
                )
        if not skills:
            raise ValueError(
                f".agents/{plugin}: a plugin folder needs at least one skill"
            )
        plugins[plugin] = skills
    if not plugins:
        raise ValueError("No plugin folder with skills under .agents/")
    return plugins


def validate_payloads(payloads: JSONObject, plugins: Plugins) -> dict[str, str]:
    if not isinstance(payloads, dict):
        raise ValueError(".agents/plugins/payloads.json: must be a JSON object")
    unknown = set(payloads) - PAYLOAD_FIELDS
    if unknown:
        raise ValueError(
            f".agents/plugins/payloads.json: unknown fields {sorted(unknown)}"
        )
    declarations = payloads.get("payloads")
    if not isinstance(declarations, dict) or not declarations:
        raise ValueError(
            ".agents/plugins/payloads.json: payloads must be a nonempty object"
        )
    publications = {}
    for package, owners in sorted(declarations.items()):
        if not isinstance(package, str) or not NAME.fullmatch(package):
            raise ValueError(
                f".agents/plugins/payloads.json: invalid package {package!r}"
            )
        if (
            not isinstance(owners, list)
            or len(owners) != 1
            or not isinstance(owners[0], str)
            or owners[0] not in plugins
        ):
            raise ValueError(
                f".agents/plugins/payloads.json: {package} must select exactly "
                "one canonical source"
            )
        publications[package] = owners[0]
    for source in plugins:
        if publications.get(source) != source:
            raise ValueError(
                f".agents/plugins/payloads.json: {source} must retain its "
                "identity publication"
            )
    dependencies = payloads.get("dependencies", {})
    if not isinstance(dependencies, dict):
        raise ValueError(
            ".agents/plugins/payloads.json: dependencies must be an object"
        )
    for plugin, needs in dependencies.items():
        if (
            plugin not in plugins
            or not isinstance(needs, list)
            or not all(isinstance(need, str) for need in needs)
            or len(needs) != len(set(needs))
        ):
            raise ValueError(
                f".agents/plugins/payloads.json: invalid dependencies for {plugin}"
            )
        for need in needs:
            if need == plugin:
                raise ValueError(
                    f".agents/plugins/payloads.json: {plugin} cannot depend on {need}"
                )
            if need in publications and publications[need] != need:
                raise ValueError(
                    f".agents/plugins/payloads.json: {plugin} cannot depend "
                    f"on local alias {need}"
                )
            if need not in publications and not DEPENDENCY.fullmatch(need):
                raise ValueError(
                    f".agents/plugins/payloads.json: {plugin} cannot depend on "
                    f"{need}; another repository's package is "
                    f"{TJ_AGENTS_MARKETPLACE}/<package>"
                )
    validate_local_dependency_cycles(dependencies, plugins)
    compatibility_aliases = payloads.get("compatibilitySkillAliases", {})
    if not isinstance(compatibility_aliases, dict):
        raise ValueError(
            ".agents/plugins/payloads.json: compatibilitySkillAliases must be an object"
        )
    for plugin, aliases in compatibility_aliases.items():
        if plugin not in plugins or not isinstance(aliases, dict):
            raise ValueError(
                ".agents/plugins/payloads.json: invalid compatibility aliases "
                f"for {plugin}"
            )
        skills = plugins[plugin]
        for alias, declaration in aliases.items():
            if not isinstance(declaration, dict) or set(declaration) != {
                "replacedBy",
                "removeAfter",
            }:
                raise ValueError(
                    f"{plugin}:{alias}: an alias declares exactly replacedBy "
                    "and removeAfter"
                )
            target = str(declaration["replacedBy"]).removeprefix(f"{plugin}:")
            if (
                alias not in skills
                or declaration["replacedBy"] != f"{plugin}:{target}"
                or target not in skills
                or target in aliases
            ):
                raise ValueError(
                    f"{plugin}:{alias}: an alias must forward to another "
                    "skill of the same plugin"
                )
            if not DATE.fullmatch(str(declaration["removeAfter"])):
                raise ValueError(f"{plugin}:{alias}: removeAfter must be YYYY-MM-DD")
            if (
                skills[alias]["metadata"]["profile"]
                != skills[target]["metadata"]["profile"]
            ):
                raise ValueError(
                    f"{plugin}:{alias}: an alias keeps its replacement's profile"
                )
    return publications


def safe_package_destination(destination: str) -> PurePosixPath:
    path = PurePosixPath(destination)
    if (
        "\\" in destination
        or ":" in destination
        or "\x00" in destination
        or path.is_absolute()
        or not path.parts
        or "." in path.parts
        or ".." in path.parts
        or path.as_posix() != destination
    ):
        raise ValueError(
            ".agents/plugins/payloads.json: unsafe resource destination "
            f"{destination!r}"
        )
    return path


def checked_resource_path(root: Path, source: str) -> Path:
    path = root.joinpath(*PurePosixPath(source).parts)
    if not path.exists():
        raise ValueError(
            f".agents/plugins/payloads.json: resource source {source!r} is missing"
        )
    root_path = root.resolve()
    current = root_path
    for part in path.relative_to(root).parts:
        current = current / part
        if is_link_or_junction(current):
            raise ValueError(
                ".agents/plugins/payloads.json: resource source "
                f"{source!r} contains a link or junction"
            )
    resolved = path.resolve()
    if not resolved.is_relative_to(root_path):
        raise ValueError(
            f".agents/plugins/payloads.json: resource source {source!r} escapes "
            "the repository"
        )
    return path


def resource_files(root: Path, source: str) -> list[Path]:
    path = checked_resource_path(root, source)
    if path.is_file():
        return [path]
    if not path.is_dir():
        raise ValueError(
            f".agents/plugins/payloads.json: resource source {source!r} is not a file "
            "or directory"
        )
    files: list[Path] = []
    for entry in sorted(path.rglob("*")):
        if ignored(entry.relative_to(path)):
            continue
        if is_link_or_junction(entry):
            raise ValueError(
                ".agents/plugins/payloads.json: resource source "
                f"{source!r} contains a link or junction"
            )
        if entry.is_file():
            files.append(entry)
    return files


def validate_skill_asset_source(
    root: Path, source: str, files: list[Path], skills: PluginSkills
) -> None:
    if not files:
        raise ValueError(
            ".agents/plugins/payloads.json: skill asset source must contain shipped "
            "files"
        )
    assets = {
        f"{skill['directory']}/{file}"
        for skill in skills.values()
        for file in skill["files"]
    }
    selected = {file.relative_to(root).as_posix() for file in files}
    if not selected.issubset(assets):
        raise ValueError(
            ".agents/plugins/payloads.json: resource source must be a runtime, "
            "resources, hooks or discovered skill asset path"
        )


def distribution_files(root: Path, name: str) -> list[tuple[Path, tuple[str, ...]]]:
    """Read a locked, non-editable pure-Python dependency without importing it."""
    if not NAME.fullmatch(name):
        raise ValueError(f"Invalid Python distribution resource: {name!r}")
    for filename in ("pyproject.toml", "uv.lock"):
        path = root / filename
        if not path.is_file() or is_link_or_junction(path):
            raise ValueError(
                f"Python distribution resources require a regular {filename}"
            )
    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    declared = {
        re.sub(r"[-_.]+", "-", match[1]).lower()
        for requirement in project.get("project", {}).get("dependencies", [])
        if isinstance(requirement, str)
        and (match := re.match(r"\s*([A-Za-z0-9][A-Za-z0-9_.-]*)", requirement))
    }
    if name not in declared:
        raise ValueError(
            f"Python distribution resource {name!r} is not a runtime dependency"
        )
    uv = shutil.which("uv")
    if uv is None:
        raise ValueError("uv is required to resolve Python distribution resources")
    inspect = """import importlib.metadata, json, re, sys, sysconfig
from pathlib import Path
environment = str(Path(sys.executable).parent.parent)
library = sysconfig.get_path('purelib', scheme='venv',
    vars={'base': environment, 'platbase': environment})
distribution = next(item for item in importlib.metadata.distributions(path=[library])
    if re.sub(r'[-_.]+', '-', item.metadata['Name']).lower() == sys.argv[1])
wheel = distribution.read_text('WHEEL') or ''
print(json.dumps({'name': distribution.metadata['Name'],
    'pure': 'Root-Is-Purelib: true' in wheel and 'Tag: py3-none-any' in wheel,
    'root': str(distribution.locate_file('')),
    'files': [str(path) for path in distribution.files or []]}))
"""
    try:
        result = subprocess.run(
            [
                uv,
                "run",
                "--locked",
                "--no-editable",
                "--project",
                str(root),
                "python",
                "-I",
                "-S",
                "-B",
                "-c",
                inspect,
                name,
            ],
            cwd=root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=180,
            check=True,
        )
        metadata = json.loads(result.stdout)
    except (OSError, subprocess.SubprocessError, ValueError) as error:
        raise ValueError(
            f"Cannot resolve locked Python distribution {name!r}: {error}"
        ) from error
    if not isinstance(metadata, dict) or metadata.get("pure") is not True:
        raise ValueError(
            f"Python distribution resource {name!r} must be a pure Python wheel"
        )
    if re.sub(r"[-_.]+", "-", str(metadata.get("name", ""))).lower() != name:
        raise ValueError(
            f"Resolved Python distribution identity does not match {name!r}"
        )
    base = metadata.get("root")
    records = metadata.get("files")
    if (
        not isinstance(base, str)
        or not Path(base).is_absolute()
        or not isinstance(records, list)
    ):
        raise ValueError(f"Invalid installed Python distribution metadata for {name!r}")
    directory = Path(base)
    resolved_directory = directory.resolve(strict=True)
    files: list[tuple[Path, tuple[str, ...]]] = []
    seen: set[str] = set()
    for record in records:
        if not isinstance(record, str):
            raise ValueError(f"Invalid Python distribution record for {name!r}")
        relative = PurePosixPath(record)
        if (
            relative.is_absolute()
            or ".." in relative.parts
            or "\\" in record
            or ":" in record
            or "\x00" in record
            or relative.as_posix() != record
        ):
            raise ValueError(f"Unsafe Python distribution record: {record!r}")
        if ignored(relative):
            continue
        if any(
            part.endswith(".dist-info") for part in relative.parts
        ) and relative.name in {
            "INSTALLER",
            "REQUESTED",
            "RECORD",
            "RECORD.jws",
            "RECORD.p7s",
            "direct_url.json",
            "uv_cache.json",
        }:
            continue
        if relative.suffix == ".pth":
            raise ValueError(
                "Python distribution resources cannot require .pth import hooks"
            )
        source = directory.joinpath(*relative.parts)
        current = source
        while current != directory:
            if is_link_or_junction(current):
                raise ValueError(f"Linked Python distribution resource: {record!r}")
            current = current.parent
        if not source.is_file() or not source.resolve(strict=True).is_relative_to(
            resolved_directory
        ):
            raise ValueError(
                f"Missing or escaped Python distribution resource: {record!r}"
            )
        if record in seen:
            raise ValueError(f"Duplicate Python distribution record: {record!r}")
        seen.add(record)
        files.append((source, relative.parts))
    if not files:
        raise ValueError(f"Python distribution resource {name!r} is empty")
    return sorted(files, key=lambda item: item[1])


def validate_resources(
    root: Path,
    payloads: JSONObject,
    publications: dict[str, str],
    plugins: Plugins,
) -> dict[str, list[tuple[Path, str]]]:
    resources = payloads.get("resources", [])
    if not isinstance(resources, list):
        raise ValueError(".agents/plugins/payloads.json: resources must be a list")
    mapped: dict[str, list[tuple[Path, str]]] = {}
    destinations: set[str] = set()
    distributions: dict[str, list[tuple[Path, tuple[str, ...]]]] = {}
    for resource in resources:
        if not isinstance(resource, dict) or set(resource) not in (
            {"plugin", "source", "destination"},
            {"plugin", "distribution", "destination"},
        ):
            raise ValueError(
                ".agents/plugins/payloads.json: each resource declares exactly "
                "plugin, destination and either source or distribution"
            )
        package, destination = (
            resource["plugin"],
            resource["destination"],
        )
        if not isinstance(package, str) or package not in publications:
            raise ValueError(
                ".agents/plugins/payloads.json: resource plugin must be a declared "
                "package"
            )
        if not isinstance(destination, str):
            raise ValueError(
                ".agents/plugins/payloads.json: each resource declares string "
                "plugin, source and destination"
            )
        target = safe_package_destination(destination)
        if "distribution" in resource:
            name = resource["distribution"]
            if not isinstance(name, str):
                raise ValueError(
                    "A Python distribution resource requires a distribution name"
                )
            if name not in distributions:
                distributions[name] = distribution_files(root, name)
            selected = distributions[name]
        else:
            source = resource["source"]
            if not isinstance(source, str):
                raise ValueError("A file resource requires a string source")
            owner, directory = resource_source_parts(source)
            if owner is not None and owner not in plugins:
                raise ValueError(
                    ".agents/plugins/payloads.json: resource source must name a "
                    "declared canonical plugin"
                )
            source_path = checked_resource_path(root, source)
            files = resource_files(root, source)
            if owner is not None and directory not in PLUGIN_RESOURCE_DIRS:
                validate_skill_asset_source(root, source, files, plugins[owner])
            selected = [
                (
                    file,
                    ()
                    if source_path.is_file()
                    else file.relative_to(source_path).parts,
                )
                for file in files
            ]
        for file, suffix in selected:
            generated = PurePosixPath(
                "plugins", package, *target.parts, *suffix
            ).as_posix()
            if generated in destinations:
                raise ValueError(
                    ".agents/plugins/payloads.json: duplicate resource output "
                    f"{generated}"
                )
            destinations.add(generated)
            mapped.setdefault(package, []).append((file, generated))
    return mapped


def validate_host_adapters(
    root: Path, payloads: JSONObject, plugins: Plugins
) -> dict[tuple[str, str, str], str]:
    adapters = payloads.get("host_adapters", [])
    if not isinstance(adapters, list):
        raise ValueError(".agents/plugins/payloads.json: host_adapters must be a list")
    result: dict[tuple[str, str, str], str] = {}
    for adapter in adapters:
        if not isinstance(adapter, dict) or set(adapter) != {"skill", "host", "source"}:
            raise ValueError(
                ".agents/plugins/payloads.json: each host adapter declares exactly "
                "skill, host and source"
            )
        qualified, host, source = (
            adapter["skill"],
            adapter["host"],
            adapter["source"],
        )
        if not isinstance(qualified, str) or ":" not in qualified:
            raise ValueError(
                ".agents/plugins/payloads.json: host adapter skill must be plugin:name"
            )
        plugin, name = qualified.split(":", 1)
        if plugin not in plugins or name not in plugins[plugin]:
            raise ValueError(
                ".agents/plugins/payloads.json: host adapter skill must name an "
                "existing canonical skill"
            )
        if host not in HOSTS or not isinstance(source, str):
            raise ValueError(
                ".agents/plugins/payloads.json: host adapter host must be claude or "
                "codex with a string source"
            )
        expected = f".agents/plugins/adapters/{host}/{plugin}/{name}.md"
        if source != expected:
            raise ValueError(
                f".agents/plugins/payloads.json: host adapter source must be {expected}"
            )
        key = (plugin, name, host)
        if key in result:
            raise ValueError(
                ".agents/plugins/payloads.json: duplicate host adapter for "
                f"{qualified} on {host}"
            )
        path = checked_resource_path(root, source)
        if not path.is_file():
            raise ValueError(
                ".agents/plugins/payloads.json: host adapter source "
                f"{source!r} is not a file"
            )
        body = read(path)
        if not body.strip() or body.startswith("---\n"):
            raise ValueError(
                ".agents/plugins/payloads.json: host adapter source must be a "
                "nonempty extension body, not a canonical skill"
            )
        result[key] = body
    return result


def validate_local_dependency_cycles(
    dependencies: Dependencies, plugins: Plugins
) -> None:
    states: dict[str, int] = {}
    trail: list[str] = []

    def visit(source: str) -> None:
        state = states.get(source, 0)
        if state == 1:
            cycle = [*trail[trail.index(source) :], source]
            raise ValueError(
                ".agents/plugins/payloads.json: local dependency cycle "
                f"{' -> '.join(cycle)}"
            )
        if state == 2:
            return
        states[source] = 1
        trail.append(source)
        for need in dependencies.get(source, []):
            if need in plugins:
                visit(need)
        trail.pop()
        states[source] = 2

    for source in sorted(plugins):
        visit(source)


def effective_dependencies(
    config: JSONObject, source: str, dependencies: Dependencies
) -> list[str]:
    inherited: tuple[str, ...] = (
        STACK_CORE_DEPENDENCIES if config["type"] == "stack" else ()
    )
    if config["type"] == "profile":
        inherited = ("tj-agents/base",)
    return list(dict.fromkeys((*inherited, *dependencies.get(source, []))))


def validate_publication_dependencies(
    publications: dict[str, str], dependencies: Dependencies
) -> None:
    for source, needs in dependencies.items():
        for need in needs:
            if "/" in need and need.split("/", 1)[1] in publications:
                raise ValueError(
                    f".agents/plugins/payloads.json: {source} cannot depend on "
                    f"{need}; name a package of this repository bare"
                )


def publication_aliases(package: str, aliases: JSONObject) -> JSONObject:
    return {
        name: {
            "replacedBy": f"{package}:{declaration['replacedBy'].split(':', 1)[1]}",
            "removeAfter": declaration["removeAfter"],
        }
        for name, declaration in aliases.items()
    }


def validate_tiers(root: Path, plugins: Plugins) -> None:
    tier_dir = root / ".agents/tiers"
    present = (
        sorted(path.name for path in tier_dir.iterdir()) if tier_dir.is_dir() else []
    )
    expected = sorted(f"{plugin}.json" for plugin in plugins)
    if present != expected:
        raise ValueError(f".agents/tiers must hold exactly {', '.join(expected)}")
    for plugin in plugins:
        source = f".agents/tiers/{plugin}.json"
        tier = load(root / source)
        if not isinstance(tier, dict):
            raise ValueError(f"{source}: must be a JSON object")
        unknown = set(tier) - {
            "tier",
            "applies",
            "stack",
            "detect",
            "fact_definitions",
            "employer",
            "session_context",
        }
        if unknown:
            raise ValueError(f"{source}: unknown tier fields {sorted(unknown)}")
        if tier.get("tier") != plugin:
            raise ValueError(f"{source}: tier must be {plugin}")
        detect = tier.get("detect")
        if tier.get("applies") == "always":
            if detect is not None:
                raise ValueError(
                    f"{source}: an always-applicable tier declares no detect markers"
                )
        elif tier.get("applies") == "stack-present":
            if not isinstance(detect, dict) or len(detect) != 1:
                raise ValueError(
                    f"{source}: a stack-present tier needs one detect predicate"
                )
        else:
            raise ValueError(f"{source}: applies must be always or stack-present")


def load_manifests(
    root: Path, plugins: dict[str, str], repository: str
) -> tuple[dict[str, dict[str, JSONObject]], dict[str, JSONObject]]:
    expected = sorted([f"{plugin}.json" for plugin in plugins] + ["marketplace.json"])
    manifests: dict[str, dict[str, JSONObject]] = {}
    templates: dict[str, JSONObject] = {}
    for host in HOSTS:
        host_dir = root / MANIFEST_ROOT / host
        present = (
            sorted(path.name for path in host_dir.iterdir())
            if host_dir.is_dir()
            else []
        )
        if present != expected:
            raise ValueError(
                f"{MANIFEST_ROOT}/{host} must hold exactly {', '.join(expected)}"
            )
        manifests[host] = {
            plugin: load(host_dir / f"{plugin}.json") for plugin in plugins
        }
        templates[host] = load(host_dir / "marketplace.json")
        if not isinstance(templates[host], dict):
            raise ValueError(
                f"{MANIFEST_ROOT}/{host}/marketplace.json: must be a JSON object"
            )
        if any(not isinstance(manifest, dict) for manifest in manifests[host].values()):
            raise ValueError(
                f"{MANIFEST_ROOT}/{host}: each plugin manifest must be a JSON object"
            )
        if set(templates[host]) != MARKETPLACE_FIELDS[host]:
            expected_fields = sorted(MARKETPLACE_FIELDS[host])
            raise ValueError(
                f"{MANIFEST_ROOT}/{host}/marketplace.json declares exactly "
                f"{expected_fields}; its {TJ_AGENTS_MARKETPLACE} name and plugin "
                "entries are generated"
            )
    if not templates["codex"]["interface"].get("displayName") or not templates[
        "claude"
    ]["owner"].get("name"):
        raise ValueError("Marketplaces need an owner name and a display name")
    for plugin in plugins:
        claude, codex = manifests["claude"][plugin], manifests["codex"][plugin]
        for field in (
            "name",
            "description",
            "author",
            "repository",
            "skills",
            "keywords",
        ):
            if not codex.get(field) or codex.get(field) != claude.get(field):
                raise ValueError(
                    f"{plugin}: Claude and Codex manifests disagree on {field}"
                )
        if codex["name"] != plugin or codex["skills"] != "./skills/":
            raise ValueError(
                f"{plugin}: manifests must keep the {plugin} identity and "
                "the ./skills/ path"
            )
        if codex["repository"] != f"https://github.com/{repository}":
            raise ValueError(
                f"{plugin}: manifest repository must be https://github.com/{repository}"
            )
        for host in HOSTS:
            if "version" in manifests[host][plugin]:
                raise ValueError(f"{plugin}: the {host} manifest must omit version")
            needs = manifests[host][plugin].get("dependencies", [])
            if not isinstance(needs, list) or not all(
                isinstance(need, str) and CLAUDE_DEPENDENCY.fullmatch(need)
                for need in needs
            ):
                raise ValueError(
                    f"{plugin}: the {host} manifest dependencies are bare packages "
                    f"of this repository or <package>@{TJ_AGENTS_MARKETPLACE}"
                )
        interface = codex.get("interface") or {}
        if not claude.get("displayName") or claude["displayName"] != interface.get(
            "displayName"
        ):
            raise ValueError(
                f"{plugin}: Claude and Codex manifests disagree on display name"
            )
        if not interface.get("category"):
            raise ValueError(
                f"{plugin}: the Codex manifest interface declares a category"
            )
    return manifests, templates


def adapter_name(skill: SkillRecord, single_plugin: bool) -> str:
    return skill["name"] if single_plugin else f"{skill['plugin']}-{skill['name']}"


def adapter_body(
    skill: SkillRecord, adapter_dir: str, canonical_dir: str | None = None
) -> str:
    source = f"{canonical_dir or skill['directory']}/SKILL.md"
    relative = posixpath.relpath(f"/{source}", f"/{adapter_dir}")
    match = FRONTMATTER.match(skill["body"])
    assert match is not None
    title = next(
        (line for line in match.group("body").splitlines() if line.startswith("# ")),
        f"# {skill['name']}",
    )
    return (
        f"---\n{match.group('header')}\n---\n\n{title}\n\n"
        f"Read and follow the [canonical definition]({relative}) in full.\n"
        "This discovery entry is generated; edit the referenced `.agents/` "
        "definition.\n"
    )


def references(body: str) -> Iterator[str]:
    for target in MARKDOWN_LINK.findall(body):
        if URL_SCHEME.match(target) or target.startswith(("#", "/")):
            continue
        target = target.split("#", 1)[0]
        if target:
            yield target
    for target in SKILL_DIRECTORY_REFERENCE.findall(body):
        yield target.rstrip(".,;:")


def validate_skill_references(plugins: Plugins) -> None:
    qualified = re.compile(
        r"(?<![-/\w])("
        + "|".join(re.escape(plugin) for plugin in sorted(plugins))
        + r"):(?!:)([a-z][a-z0-9-]+)"
    )
    for plugin, skills in plugins.items():
        for skill in skills.values():
            for namespace, name in qualified.findall(skill["body"]):
                if name not in plugins[namespace]:
                    raise ValueError(
                        f"{plugin}:{skill['name']}: unknown skill reference "
                        f"{namespace}:{name}"
                    )
            for name in BARE_SKILL_REFERENCE.findall(skill["body"]):
                if name not in skills:
                    raise ValueError(
                        f"{plugin}:{skill['name']}: unknown skill reference "
                        f"`{name}` skill"
                    )


def validate_file_references(
    root: Path,
    plugins: Plugins,
    canonical_locations: dict[tuple[str, str], list[str]],
    adapter_locations: list[tuple[SkillRecord, str, str, bool, bool]],
    output: dict[str, bytes],
) -> None:
    def present(path: str) -> bool:
        return path in output or any(key.startswith(path + "/") for key in output)

    def validate(
        plugin: str,
        skill: SkillRecord,
        body: str,
        source_dir: str,
        shipped_dir: str,
        source_exists: bool,
        generated_required: bool,
    ) -> None:
        label = f"{plugin}:{skill['name']}"
        for target in sorted(set(references(body))):
            source = posixpath.normpath(posixpath.join(source_dir, target))
            if source == ".." or source.startswith("../"):
                raise ValueError(
                    f"{label}: {target} does not resolve from {source_dir}"
                )
            shipped = posixpath.normpath(posixpath.join(shipped_dir, target))
            if shipped_dir.startswith("plugins/"):
                package_root = "/".join(shipped_dir.split("/", 2)[:2])
                if shipped != package_root and not shipped.startswith(
                    package_root + "/"
                ):
                    raise ValueError(
                        f"{label}: {target} does not resolve from generated "
                        f"{shipped_dir}"
                    )
            if source_exists and not (root / source).exists() and not present(shipped):
                raise ValueError(
                    f"{label}: {target} does not resolve from {source_dir}"
                )
            if generated_required and not present(shipped):
                raise ValueError(
                    f"{label}: {target} does not resolve from generated {shipped_dir}"
                )

    for plugin, skills in plugins.items():
        for skill in skills.values():
            for directory in canonical_locations[(plugin, skill["name"])]:
                validate(
                    plugin,
                    skill,
                    skill["body"],
                    skill["directory"],
                    directory,
                    True,
                    True,
                )
    for skill, body, directory, source_exists, generated_required in adapter_locations:
        validate(
            skill["plugin"],
            skill,
            body,
            directory,
            directory,
            source_exists,
            generated_required,
        )


def harness_body(
    root: Path,
    package: str,
    output: dict[str, bytes],
    dependencies: list[str],
    inherit_core: bool,
) -> str | None:
    relative = f"{MANIFEST_ROOT}/harness/{package}.json"
    path = root / relative
    if not path.exists():
        return None
    try:
        body = read(path)
        manifest = json.loads(body)
    except (OSError, UnicodeError, ValueError) as error:
        raise ValueError(f"{relative}: unreadable JSON ({error})") from None
    if not isinstance(manifest, dict):
        raise ValueError(f"{relative}: must be a JSON object")
    if (
        type(manifest.get("schema_version")) is not int
        or manifest["schema_version"] != 1
    ):
        raise ValueError(f"{relative}: schema_version must be 1")
    identity = f"{TJ_AGENTS_MARKETPLACE}/{package}"
    if manifest.get("plugin") != identity:
        raise ValueError(f"{relative}: plugin must be {identity}")
    roots = manifest.get("source_roots")
    if (
        not isinstance(roots, list)
        or not roots
        or not all(isinstance(value, str) and value for value in roots)
    ):
        raise ValueError(f"{relative}: source_roots must be a nonempty list of paths")
    if len(roots) != len(set(roots)):
        raise ValueError(f"{relative}: source_roots must be unique")
    for value in roots:
        source = PurePosixPath(value)
        if (
            "\\" in value
            or ":" in value
            or "\x00" in value
            or source.is_absolute()
            or ".." in source.parts
            or source.as_posix() != value
            or len(source.parts) < 2
            or source.parts[0] != ".agents"
        ):
            raise ValueError(f"{relative}: unsafe canonical source root {value!r}")
        target = f"plugins/{package}/{value}"
        if target not in output and not any(
            name.startswith(target + "/") for name in output
        ):
            raise ValueError(
                f"{relative}: source root {value!r} is not shipped in {package}"
            )
    requires = manifest.get("requires")
    if not isinstance(requires, dict):
        raise ValueError(f"{relative}: requires must be a JSON object")
    marketplaces = requires.get("marketplaces", [])
    if not isinstance(marketplaces, list) or not all(
        isinstance(value, dict) for value in marketplaces
    ):
        raise ValueError(f"{relative}: requires.marketplaces must be a list of objects")
    listed = requires.get("plugins", [])
    if not isinstance(listed, list) or not all(
        isinstance(value, str) and DEPENDENCY.fullmatch(value) for value in listed
    ):
        raise ValueError(
            f"{relative}: requires.plugins lists only "
            f"{TJ_AGENTS_MARKETPLACE}/<package> identities"
        )
    plugins = [
        identity,
        *([] if inherit_core else listed),
        *(
            need if "/" in need else f"{TJ_AGENTS_MARKETPLACE}/{need}"
            for need in dependencies
        ),
    ]
    return dump(
        {
            **manifest,
            "requires": {
                **requires,
                "marketplaces": [
                    {
                        "id": TJ_AGENTS_MARKETPLACE,
                        "repository": TJ_AGENTS_MARKETPLACE_REPOSITORY,
                    }
                ],
                "plugins": list(dict.fromkeys(plugins)),
            },
        }
    )


def claude_dependencies(dependencies: list[str]) -> list[str]:
    result = []
    for dependency in dependencies:
        if "/" in dependency:
            marketplace, package = dependency.split("/", 1)
            result.append(f"{package}@{marketplace}")
        else:
            result.append(dependency)
    return result


def canonical_text(data: bytes) -> bytes:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return data
    return text.replace("\r\n", "\n").replace("\r", "\n").encode("utf-8")


def package_digest(output: dict[str, bytes], package: str) -> str:
    prefix = f"plugins/{package}/"
    digest = hashlib.sha256()
    for path in sorted(output):
        relative = path.removeprefix(prefix)
        if relative in (path, CODEX_HOOK_MANIFEST):
            continue
        content = canonical_text(output[path])
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(len(content)).encode("ascii"))
        digest.update(b"\0")
        digest.update(content)
    return "sha256:" + digest.hexdigest()


class LoaderDigest(ast.NodeTransformer):
    def __init__(self, digest: str) -> None:
        self.digest = digest

    def visit_Name(self, node: ast.Name) -> ast.expr:
        if node.id == BOOTSTRAP_DIGEST:
            return ast.Constant(self.digest)
        return node


def codex_snapshot_bootstrap(root: Path, loader: bytes) -> str:
    tree = ast.parse((root / CODEX_HOOK_BOOTSTRAP).read_text(encoding="utf-8"))
    for statement in tree.body:
        if not isinstance(statement, BOOTSTRAP_STATEMENTS):
            raise ValueError(
                f"{CODEX_HOOK_BOOTSTRAP}:{statement.lineno}: only simple statements "
                "fit one command line"
            )
    tree = LoaderDigest(hashlib.sha256(loader).hexdigest()).visit(tree)
    command = ";".join(ast.unparse(statement) for statement in tree.body)
    unsafe = sorted({character for character in command if character in COMMAND_UNSAFE})
    if unsafe:
        raise ValueError(f"{CODEX_HOOK_BOOTSTRAP} compiles to unsafe {unsafe}")
    return command


def snapshot_command(command: str, prefix: str, wrapped: str, field: str) -> str:
    if not command.startswith(prefix + '"${PLUGIN_ROOT}/'):
        raise ValueError(f"Unsupported Codex hook {field}: {command}")
    return wrapped + command.removeprefix(prefix)


def bind_codex_hook_snapshots(
    root: Path, output: dict[str, bytes], packages: list[str]
) -> None:
    hooked = [
        package
        for package in packages
        if f"plugins/{package}/{CODEX_HOOK_MANIFEST}" in output
    ]
    if not hooked:
        return
    loader = canonical_text((root / CODEX_HOOK_LOADER).read_bytes())
    bootstrap = codex_snapshot_bootstrap(root, loader)
    for package in hooked:
        manifest = f"plugins/{package}/{CODEX_HOOK_MANIFEST}"
        output[f"plugins/{package}/{CODEX_HOOK_PACKAGE_LOADER}"] = loader
        expected = package_digest(output, package)
        root_argument = '"${PLUGIN_ROOT}"'
        arguments = f'-c "{bootstrap}" {root_argument} "{expected}" "{package}" '
        payload = json.loads(output[manifest])
        for groups in payload["hooks"].values():
            for group in groups:
                for hook in group["hooks"]:
                    hook["command"] = snapshot_command(
                        hook["command"],
                        "python3 -B ",
                        "python3 -I -B " + arguments,
                        "command",
                    )
                    windows = snapshot_command(
                        hook["commandWindows"],
                        "python -B ",
                        "python -I -B " + arguments,
                        "commandWindows",
                    )
                    expanded = len(windows) + windows.count("${PLUGIN_ROOT}") * (
                        WINDOWS_MAX_PATH - len("${PLUGIN_ROOT}")
                    )
                    if expanded > CODEX_WINDOWS_COMMAND_BUDGET:
                        raise ValueError(
                            f"{package}: Codex commandWindows exceeds the "
                            f"{CODEX_WINDOWS_COMMAND_BUDGET}-character cmd.exe budget "
                            "once ${PLUGIN_ROOT} expands"
                        )
                    hook["commandWindows"] = windows
        output[manifest] = dump(payload).encode("utf-8")


def build(root: Path) -> dict[str, bytes]:
    config = load_config(root)
    namespace, repository = config["namespace"], config["repository"]
    payloads = load(root / ".agents/plugins/payloads.json")
    plugins = discover(root, namespace, mapped_resource_directories(payloads))
    publications = validate_payloads(payloads, plugins)
    resources = validate_resources(root, payloads, publications, plugins)
    host_adapters = validate_host_adapters(root, payloads, plugins)
    validate_tiers(root, plugins)
    manifests, templates = load_manifests(root, publications, repository)
    validate_skill_references(plugins)
    aliases = payloads.get("compatibilitySkillAliases", {})
    dependencies = payloads.get("dependencies", {})
    validate_publication_dependencies(publications, dependencies)
    single_plugin = len(plugins) == 1
    plugins_with_host_adapters = {plugin for plugin, _, _ in host_adapters}

    output: dict[str, bytes] = {}
    canonical_locations: dict[tuple[str, str], list[str]] = {}
    adapter_locations: list[tuple[SkillRecord, str, str, bool, bool]] = []
    mirrored_resources: set[tuple[Path, str]] = set()

    def emit(relative: str, data: str | bytes) -> None:
        key = PurePosixPath(relative).as_posix()
        if any(
            key == existing
            or key.startswith(existing + "/")
            or existing.startswith(key + "/")
            for existing in output
        ):
            raise ValueError(f"Duplicate generated output: {key}")
        output[key] = data.encode("utf-8") if isinstance(data, str) else data

    def emit_skill(directory: str, skill: SkillRecord, body: str) -> None:
        emit(f"{directory}/SKILL.md", body)
        for file in skill["files"]:
            emit(f"{directory}/{file}", (root / skill["directory"] / file).read_bytes())

    repository_index = [
        f"# {namespace} capabilities",
        "",
        "Generated from `.agents/<plugin>/<kind>/<family>/<member>/SKILL.md`; "
        "a skill's name is its folder path below the kind folder joined by "
        "hyphens.",
        "",
    ]
    for source, skills in plugins.items():
        ordered = sorted(skills.values(), key=lambda item: (item["kind"], item["name"]))
        for skill in ordered:
            for host, adapter_root in ADAPTER_ROOTS.items():
                adapter_dir = f"{adapter_root}/{adapter_name(skill, single_plugin)}"
                host_adapter = adapter_body(skill, adapter_dir)
                extension = host_adapters.get((source, skill["name"], host))
                if extension is not None:
                    host_adapter += "\n" + extension.rstrip() + "\n"
                emit_skill(adapter_dir, skill, host_adapter)
                adapter_locations.append(
                    (skill, host_adapter, adapter_dir, True, False)
                )
        entries = [
            f"- `{skill['name']}` — {skill['kind']} — {skill['metadata']['profile']}"
            for skill in ordered
        ]
        repository_index += [f"## {source}", ""]
        repository_index += [
            f"{entry} — `{skill['directory']}/SKILL.md`"
            for entry, skill in zip(entries, ordered, strict=True)
        ]
        repository_index.append("")

    for package, source in publications.items():
        skills = plugins[source]
        effective = effective_dependencies(config, source, dependencies)
        ordered = sorted(skills.values(), key=lambda item: (item["kind"], item["name"]))
        package_aliases = publication_aliases(package, aliases.get(source, {}))
        for skill in ordered:
            canonical_dir = f"plugins/{package}/{skill['directory']}"
            package_adapter_dir = f"plugins/{package}/skills/{skill['name']}"
            package_adapter = adapter_body(skill, package_adapter_dir, canonical_dir)
            claude_extension = host_adapters.get((source, skill["name"], "claude"))
            if claude_extension is not None:
                package_adapter += "\n" + claude_extension.rstrip() + "\n"
            emit_skill(canonical_dir, skill, skill["body"])
            emit_skill(package_adapter_dir, skill, package_adapter)
            canonical_locations.setdefault((source, skill["name"]), []).append(
                canonical_dir
            )
            adapter_locations.append(
                (skill, package_adapter, package_adapter_dir, False, True)
            )
            if source in plugins_with_host_adapters:
                codex_adapter_dir = f"plugins/{package}/codex-skills/{skill['name']}"
                codex_adapter = adapter_body(skill, codex_adapter_dir, canonical_dir)
                codex_extension = host_adapters.get((source, skill["name"], "codex"))
                if codex_extension is not None:
                    codex_adapter += "\n" + codex_extension.rstrip() + "\n"
                emit_skill(codex_adapter_dir, skill, codex_adapter)
                adapter_locations.append(
                    (skill, codex_adapter, codex_adapter_dir, False, True)
                )

        for source_file, destination in resources.get(package, []):
            data = source_file.read_bytes()
            emit(destination, data)
            relative = PurePosixPath(destination).relative_to(f"plugins/{package}")
            if len(relative.parts) < 3 or relative.parts[0] != "skills":
                continue
            skill_name = relative.parts[1]
            if skill_name not in skills:
                continue
            skill = skills[skill_name]
            suffix = PurePosixPath(*relative.parts[2:]).as_posix()
            for adapter_root in ADAPTER_ROOTS.values():
                target = f"{adapter_root}/{adapter_name(skill, single_plugin)}/{suffix}"
                if (source_file, target) not in mirrored_resources:
                    emit(target, data)
                    mirrored_resources.add((source_file, target))
            if source in plugins_with_host_adapters:
                emit(f"plugins/{package}/codex-skills/{skill_name}/{suffix}", data)

        entries = [
            f"- `{skill['name']}` — {skill['kind']} — {skill['metadata']['profile']}"
            for skill in ordered
        ]
        emit(
            f"plugins/{package}/INDEX.md",
            "\n".join([f"# {package} package capabilities", "", *entries, ""]),
        )
        profiles: dict[str, list[str]] = {}
        for skill in ordered:
            if skill["name"] not in package_aliases:
                profiles.setdefault(skill["metadata"]["profile"], []).append(
                    skill["name"]
                )
        selection = {
            "plugin": package,
            "dependencies": effective,
            "profiles": {
                profile: sorted(names) for profile, names in sorted(profiles.items())
            },
            "compatibilitySkillAliases": package_aliases,
            "skills": [
                {
                    "name": skill["name"],
                    "kind": skill["kind"],
                    "profile": skill["metadata"]["profile"],
                    "applicability": split_tags(skill["metadata"]["applicability"]),
                    "requires": split_tags(skill["metadata"]["requires"]),
                    "provenance": split_tags(skill["metadata"]["provenance"]),
                }
                for skill in sorted(ordered, key=lambda item: item["name"])
            ],
        }
        emit(f"plugins/{package}/selection.json", dump(selection))
        emit(
            f"plugins/{package}/tier.json", read(root / f".agents/tiers/{source}.json")
        )
        harness = harness_body(
            root,
            package,
            output,
            effective,
            config["type"] in ("stack", "profile"),
        )
        if harness is not None:
            emit(f"plugins/{package}/harness.json", harness)
        for host in HOSTS:
            manifest = manifests[host][package]
            if host == "claude" and config["type"] in ("stack", "profile"):
                manifest = {**manifest, "dependencies": claude_dependencies(effective)}
            if host == "codex" and source in plugins_with_host_adapters:
                manifest = {**manifest, "skills": "./codex-skills/"}
            emit(f"plugins/{package}/.{host}-plugin/plugin.json", dump(manifest))

    bind_codex_hook_snapshots(root, output, list(publications))
    emit(".agents/INDEX.md", "\n".join(repository_index))
    claude_entries, codex_entries = [], []
    for package in publications:
        claude, codex = manifests["claude"][package], manifests["codex"][package]
        category = codex["interface"]["category"]
        claude_entries.append(
            {
                "name": package,
                "source": f"./plugins/{package}",
                "description": claude["description"],
                "category": category,
                "keywords": claude["keywords"],
            }
        )
        codex_entries.append(
            {
                "name": package,
                "source": {"source": "local", "path": f"./plugins/{package}"},
                "policy": CODEX_POLICY,
                "category": category,
            }
        )
    for host, listed in (("claude", claude_entries), ("codex", codex_entries)):
        emit(
            MARKETPLACE_OUTPUTS[host],
            dump({"name": TJ_AGENTS_MARKETPLACE, **templates[host], "plugins": listed}),
        )
    validate_file_references(
        root, plugins, canonical_locations, adapter_locations, output
    )
    return output


def split_tags(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def generated_root_paths(root: Path) -> list[Path]:
    resolved_root = root.resolve()
    result: list[Path] = []
    for value in GENERATED_ROOTS:
        relative = PurePosixPath(value)
        ancestor = resolved_root
        for part in relative.parts:
            ancestor = ancestor / part
            if is_link_or_junction(ancestor):
                raise ValueError(
                    "Generated root must not be or sit under a link or "
                    f"junction: {value}"
                )
        path = resolved_root.joinpath(*relative.parts).resolve()
        if path == resolved_root or not path.is_relative_to(resolved_root):
            raise ValueError(f"Generated root escapes the repository: {value}")
        result.append(path)
    return result


def existing_files(root: Path) -> set[str]:
    result: set[str] = set()
    for value in GENERATED_ROOTS:
        path = root / value
        if path.is_dir():
            result.update(
                item.relative_to(root).as_posix()
                for item in path.rglob("*")
                if item.is_file() and not ignored(item.relative_to(root))
            )
        elif path.is_file():
            result.add(value)
    return result


def synchronize(root: Path, check: bool) -> int:
    output = build(root)
    expected = set(output)
    actual = existing_files(root)
    if check:
        stale = sorted(
            path
            for path in expected & actual
            if (root / path).read_bytes() != output[path]
        )
        problems = [
            ("MISSING", sorted(expected - actual)),
            ("STALE", stale),
            ("EXTRA", sorted(actual - expected)),
        ]
        for label, paths in problems:
            for entry in paths:
                print(f"{label}: {entry}")
        if any(paths for _, paths in problems):
            return 1
        print(f"generated outputs current: {len(expected)} files")
        return 0
    for path in generated_root_paths(root):
        if path.is_dir():
            shutil.rmtree(path)
        elif path.exists():
            path.unlink()
    for relative, data in output.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    print(f"generated {len(expected)} files")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root", type=Path, default=Path(__file__).resolve().parents[1]
    )
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    try:
        return synchronize(args.root.resolve(), args.check)
    except ValueError as error:
        print(f"error: {error}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
