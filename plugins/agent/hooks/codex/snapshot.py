import contextlib
import hashlib
import os
import re
import runpy
import shutil
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import NoReturn

MARKER = ".snapshot-digest"
RETENTION_SECONDS = 3 * 24 * 60 * 60
SNAPSHOT_NAME = re.compile(r"[0-9a-f]{64}(\.[0-9a-f]{32})?")


def fail(message: str) -> NoReturn:
    print(message, file=sys.stderr)
    raise SystemExit(2)


def digest_tree(root: Path) -> str:
    excluded = {"hooks/codex.json"}
    entries: list[tuple[str, str]] = []
    directories = [(str(root), "")]
    while directories:
        directory, prefix = directories.pop()
        with os.scandir(directory) as children:
            for entry in children:
                relative = prefix + entry.name
                if entry.is_symlink():
                    fail("Codex hook package contains a symbolic link")
                if entry.is_dir(follow_symlinks=False):
                    directories.append((entry.path, relative + "/"))
                elif entry.is_file(follow_symlinks=False):
                    entries.append((relative, entry.path))
                else:
                    fail("Codex hook package contains an unsupported entry")
    files = [
        (relative, Path(path))
        for relative, path in sorted(entries)
        if relative not in excluded
    ]
    digest = hashlib.sha256()
    if files:
        with ThreadPoolExecutor(max_workers=min(16, len(files))) as reader:
            for start in range(0, len(files), 16):
                batch = files[start : start + 16]
                contents = reader.map(Path.read_bytes, [path for _, path in batch])
                for (relative, _), content in zip(batch, contents, strict=True):
                    try:
                        text = content.decode("utf-8")
                    except UnicodeDecodeError:
                        pass
                    else:
                        content = (
                            text.replace("\r\n", "\n")
                            .replace("\r", "\n")
                            .encode("utf-8")
                        )
                    digest.update(relative.encode("utf-8"))
                    digest.update(b"\0")
                    digest.update(str(len(content)).encode("ascii"))
                    digest.update(b"\0")
                    digest.update(content)
    return "sha256:" + digest.hexdigest()


def verified(root: Path, expected: str) -> None:
    if not root.is_dir() or root.is_symlink() or digest_tree(root) != expected:
        fail("Codex hook package integrity check failed")


@dataclass(frozen=True, slots=True)
class Snapshot:
    package: Path
    digest: str
    path: Path

    @classmethod
    def for_package(cls, package: Path, digest: str, snapshots: Path) -> "Snapshot":
        key = hashlib.sha256((str(package) + "\0" + digest).encode("utf-8")).hexdigest()
        return cls(package, digest, snapshots / key)

    @property
    def marker(self) -> Path:
        return self.path / MARKER

    def reuse(self) -> bool:
        try:
            if (
                self.path.is_symlink()
                or self.marker.read_text(encoding="utf-8") != self.digest
            ):
                return False
        except OSError:
            return False
        with contextlib.suppress(OSError):
            os.utime(self.marker)
        return True

    def create(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.sibling()
        try:
            shutil.copytree(self.package, temporary, symlinks=True)
            if digest_tree(temporary) != self.digest:
                raise OSError("Codex hook snapshot copy differs from its package")
            (temporary / MARKER).write_text(self.digest, encoding="utf-8")
            try:
                temporary.rename(self.path)
            except OSError:
                if self.reuse():
                    return
                self.path.rename(self.sibling())
                temporary.rename(self.path)
        finally:
            shutil.rmtree(temporary, ignore_errors=True)

    def sibling(self) -> Path:
        return self.path.with_name(self.path.name + "." + uuid.uuid4().hex)

    def prune_siblings(self) -> None:
        cutoff = time.time() - RETENTION_SECONDS
        for entry in self.path.parent.iterdir():
            if entry.name == self.path.name or not SNAPSHOT_NAME.fullmatch(entry.name):
                continue
            try:
                marker = entry / MARKER
                if (marker if marker.is_file() else entry).stat().st_mtime < cutoff:
                    marker.unlink(missing_ok=True)
                    shutil.rmtree(entry)
            except OSError:
                pass

    def resolve(self) -> Path:
        if self.reuse():
            return self.path
        verified(self.package, self.digest)
        try:
            self.create()
        except OSError:
            return self.package
        with contextlib.suppress(OSError):
            self.prune_siblings()
        return self.path


def main() -> None:
    root = Path(sys.argv[1])
    expected = sys.argv[2]
    plugin = sys.argv[3]
    script = Path(sys.argv[4])
    relative = script.relative_to(root)
    if root.parent.name != plugin or relative.is_absolute() or ".." in relative.parts:
        fail("Codex hook package identity is invalid")
    data = os.environ.get("PLUGIN_DATA")
    configured = os.environ.get("TJ_AGENTS_STARTUP_SNAPSHOT_DIRECTORY")
    if data or configured:
        snapshots = (
            Path(configured).resolve()
            if configured
            else Path(data or "").resolve() / "hook-snapshots"
        )
        selected = Snapshot.for_package(root, expected, snapshots).resolve()
    else:
        verified(root, expected)
        selected = root
    target = selected / relative
    if not target.is_file():
        fail("Codex hook script is unavailable")
    sys.path.insert(0, str(target.parent))
    sys.argv = [
        str(target),
        *(value.replace(str(root), str(selected), 1) for value in sys.argv[5:]),
    ]
    runpy.run_path(str(target), run_name="__main__")


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as error:  # noqa: BLE001
        fail("Codex hook snapshot failed: " + str(error))
