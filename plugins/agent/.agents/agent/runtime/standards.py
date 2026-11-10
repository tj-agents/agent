import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

START = "<!-- BEGIN tj-agents:standards -->"
END = "<!-- END tj-agents:standards -->"
NAME = re.compile(r"[a-z][a-z0-9-]*\Z")


class StandardsError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class Standard:
    identity: str
    applicability: tuple[str, ...]


def read(root: Path, identity: str) -> list[Standard]:
    name = identity.partition("@")[0]
    if not NAME.fullmatch(name):
        raise StandardsError(f"Invalid plugin identity: {identity}")
    source = root / ".agents" / name
    if not source.is_dir():
        raise StandardsError(f"Cannot read canonical skills for {identity}: {source}")
    found = []
    for path in sorted(source.rglob("SKILL.md")):
        if path.relative_to(source).parts[0] not in {
            "convention",
            "contract",
            "policy",
        }:
            continue
        try:
            with path.open(encoding="utf-8-sig") as stream:
                if stream.readline().strip() != "---":
                    raise StandardsError(f"{path}: missing skill metadata")
                metadata = {}
                for line in stream:
                    if line.strip() == "---":
                        break
                    key, separator, value = line.partition(":")
                    if separator:
                        metadata[key.strip()] = value.strip().strip("\"'")
                else:
                    raise StandardsError(f"{path}: unterminated skill metadata")
        except (OSError, UnicodeError) as error:
            raise StandardsError(f"Cannot read standard {path}: {error}") from error
        if metadata.get("kind") not in {"convention", "contract", "policy"}:
            continue
        if metadata.get("profile", "core") != "core":
            continue
        skill_name = metadata.get("name", "")
        if not NAME.fullmatch(skill_name):
            raise StandardsError(f"{path}: invalid standard name")
        applicability = metadata.get("applicability", "When relevant to this task")
        found.append(Standard(f"{name}:{skill_name}", (applicability,)))
    return found


def render(roots: Mapping[str, Path]) -> str:
    groups: dict[tuple[str, ...], list[str]] = {}
    for identity, root in sorted(roots.items()):
        for standard in read(root, identity):
            groups.setdefault(standard.applicability, []).append(standard.identity)
    lines = [
        START,
        "## Repository standards",
        "",
        "Before the first edit, read the full standards relevant to your changes",
        "from the index below. Invoke the skill or read its advertised",
        "SKILL.md and its canonical definition. A skill listing is not a read.",
        "Load only the applicable standards and their needed references; reassess when",
        "the task changes. Report any missing required skill and repair this repo's",
        "plugin installation before editing the files it governs.",
        "",
        "Selected plugins: " + ", ".join(f"`{name}`" for name in sorted(roots)) + ".",
        "",
    ]
    for applicability, names in sorted(groups.items()):
        label = "; ".join(" ".join(item.split()) for item in applicability)
        lines.append(
            f"- {label}: " + ", ".join(f"`{name}`" for name in sorted(set(names)))
        )
    lines.extend(["", END, ""])
    return "\n".join(lines)


def document(original: str, block: str) -> str:
    starts, ends = original.count(START), original.count(END)
    if starts != ends or starts > 1:
        raise StandardsError("AGENTS.md has malformed repository standards markers")
    if starts:
        before, _, rest = original.partition(START)
        _, separator, after = rest.partition(END)
        if not separator or END in before:
            raise StandardsError("AGENTS.md has malformed repository standards markers")
        original = before + after.lstrip("\r\n")
    return block.rstrip() + "\n\n" + original.lstrip("\r\n")


def update(project: Path, roots: Mapping[str, Path], *, check: bool = False) -> bool:
    path = project / "AGENTS.md"
    if path.is_symlink() or path.is_junction():
        raise StandardsError(f"Refusing redirected instruction file: {path}")
    try:
        original = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        original = ""
    expected = document(original, render(roots))
    changed = expected != original
    if changed and not check:
        path.write_text(expected, encoding="utf-8", newline="\n")
    return changed
