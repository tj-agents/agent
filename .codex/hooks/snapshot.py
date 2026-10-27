import hashlib
from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import re
import runpy
import shutil
import sys
import time
import uuid

MARKER = ".snapshot-digest"
RETENTION_SECONDS = 3 * 24 * 60 * 60
SNAPSHOT_NAME = re.compile(r"[0-9a-f]{64}(\.[0-9a-f]{32})?")


def fail(message):
    print(message, file=sys.stderr)
    raise SystemExit(2)


def digest_tree(root):
    excluded = {"hooks/codex.json"}
    files = []
    directories = [(root, "")]
    while directories:
        directory, prefix = directories.pop()
        with os.scandir(directory) as entries:
            for entry in entries:
                relative = prefix + entry.name
                if entry.is_symlink():
                    fail("Codex hook package contains a symbolic link")
                if entry.is_dir(follow_symlinks=False):
                    directories.append((entry.path, relative + "/"))
                elif entry.is_file(follow_symlinks=False):
                    files.append((relative, entry.path))
                else:
                    fail("Codex hook package contains an unsupported entry")
    files = [(relative, Path(path)) for relative, path in sorted(files) if relative not in excluded]
    digest = hashlib.sha256()
    if files:
        with ThreadPoolExecutor(max_workers=min(16, len(files))) as reader:
            for start in range(0, len(files), 16):
                batch = files[start:start + 16]
                for (relative, _), content in zip(batch, reader.map(Path.read_bytes, [path for _, path in batch])):
                    try:
                        text = content.decode("utf-8")
                    except UnicodeDecodeError:
                        pass
                    else:
                        content = text.replace("\r\n", "\n").replace("\r", "\n").encode("utf-8")
                    digest.update(relative.encode("utf-8"))
                    digest.update(b"\0")
                    digest.update(str(len(content)).encode("ascii"))
                    digest.update(b"\0")
                    digest.update(content)
    return "sha256:" + digest.hexdigest()


def verified(root, expected):
    if not root.is_dir() or root.is_symlink() or digest_tree(root) != expected:
        fail("Codex hook package integrity check failed")


def current(snapshot, expected):
    marker = snapshot / MARKER
    try:
        if snapshot.is_symlink() or marker.read_text(encoding="utf-8") != expected:
            return False
    except OSError:
        return False
    try:
        os.utime(marker)
    except OSError:
        pass
    return True


def create(root, expected, snapshot_dir, key):
    snapshot = snapshot_dir / key
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    temporary = snapshot_dir / (key + "." + uuid.uuid4().hex)
    try:
        shutil.copytree(root, temporary, symlinks=True)
        if digest_tree(temporary) != expected:
            raise OSError("Codex hook snapshot copy differs from its package")
        (temporary / MARKER).write_text(expected, encoding="utf-8")
        try:
            temporary.rename(snapshot)
        except OSError:
            if current(snapshot, expected):
                return
            snapshot.rename(snapshot_dir / (key + "." + uuid.uuid4().hex))
            temporary.rename(snapshot)
    finally:
        shutil.rmtree(temporary, ignore_errors=True)


def prune(snapshot_dir, key):
    cutoff = time.time() - RETENTION_SECONDS
    for entry in snapshot_dir.iterdir():
        if entry.name == key or not SNAPSHOT_NAME.fullmatch(entry.name):
            continue
        try:
            marker = entry / MARKER
            if (marker if marker.is_file() else entry).stat().st_mtime < cutoff:
                marker.unlink(missing_ok=True)
                shutil.rmtree(entry)
        except OSError:
            pass


def snapshot_root(root, expected, snapshot_dir):
    root = Path(root)
    snapshot_dir = Path(snapshot_dir)
    key = hashlib.sha256((str(root) + "\0" + expected).encode("utf-8")).hexdigest()
    snapshot = snapshot_dir / key
    if current(snapshot, expected):
        return snapshot
    verified(root, expected)
    try:
        create(root, expected, snapshot_dir, key)
    except OSError:
        return root
    try:
        prune(snapshot_dir, key)
    except OSError:
        pass
    return snapshot


def main():
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
        snapshot_dir = Path(configured).resolve() if configured else Path(data).resolve() / "hook-snapshots"
        selected = snapshot_root(root, expected, snapshot_dir)
    else:
        verified(root, expected)
        selected = root
    target = selected / relative
    if not target.is_file():
        fail("Codex hook script is unavailable")
    sys.path.insert(0, str(target.parent))
    sys.argv = [str(target), *(value.replace(str(root), str(selected), 1) for value in sys.argv[5:])]
    runpy.run_path(str(target), run_name="__main__")


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as error:
        fail("Codex hook snapshot failed: " + str(error))
