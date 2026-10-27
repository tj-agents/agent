from __future__ import annotations

import argparse
from collections import Counter
import io
import json
import os
from pathlib import Path, PurePosixPath
import queue
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import time


GITHUB_SOURCE = re.compile(
    r"(?:https://github\.com/|git@github\.com:|ssh://git@github\.com/)?"
    r"(?P<owner>[A-Za-z0-9-]+)/(?P<repo>[A-Za-z0-9_.-]+?)(?:\.git)?/?$",
    re.IGNORECASE,
)
TRUSTED_OWNER = "tj-agents"
TIMEOUT = 45


def github_repository(source):
    match = GITHUB_SOURCE.fullmatch(source) if isinstance(source, str) else None
    if not match or match["repo"] in {".", ".."}:
        return None
    return match["owner"].casefold(), match["repo"].casefold()


def command(executable):
    if Path(executable).suffix.casefold() == ".ps1":
        shell = shutil.which("pwsh") or shutil.which("powershell.exe")
        if not shell:
            raise RuntimeError("PowerShell is required to run the Codex shim")
        return [shell, "-NoProfile", "-File", executable]
    return [executable]


def run(arguments, cwd, timeout=TIMEOUT):
    return subprocess.run(
        arguments, cwd=cwd, check=True, capture_output=True,
        text=True, encoding="utf-8", timeout=timeout,
    ).stdout.strip()


class AppServer:
    def __init__(self, executable, cwd, launch_args=(), environment=None, timeout=TIMEOUT, existing=False):
        if existing:
            from codex.websocket import WebSocketStdio

        self.timeout = timeout
        self.process = subprocess.Popen(
            [*command(executable), *launch_args, "app-server", *(["proxy"] if existing else [])], cwd=cwd,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=not existing, encoding=None if existing else "utf-8", bufsize=-1 if existing else 1,
            env=dict(os.environ, **(environment or {})),
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        self.messages = queue.Queue()
        self.sequence = 0
        self.ready = threading.Event()
        self.connection_error = None
        self.websocket = None
        if existing:
            self.websocket = WebSocketStdio(self.process.stdout, self.process.stdin)
        threading.Thread(target=self.read, daemon=True).start()

    def read(self):
        try:
            if self.websocket:
                self.websocket.handshake()
                self.ready.set()
                while True:
                    self.messages.put(self.websocket.receive())
            for line in self.process.stdout:
                self.messages.put(json.loads(line))
        except (OSError, ValueError, RuntimeError) as error:
            self.connection_error = error
            self.messages.put(error)
        finally:
            self.ready.set()
            self.messages.put(None)

    def send(self, message):
        if self.websocket:
            if not self.ready.wait(self.timeout):
                raise RuntimeError("Existing Codex daemon WebSocket initialization timed out")
            if self.connection_error:
                raise RuntimeError(f"Existing Codex daemon connection failed: {self.connection_error}")
            self.websocket.send(message)
            return
        self.process.stdin.write(json.dumps(message) + "\n")
        self.process.stdin.flush()

    def request(self, method, params):
        self.sequence += 1
        request_id = self.sequence
        self.send({"id": request_id, "method": method, "params": params})
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                message = self.messages.get(timeout=max(0, deadline - time.monotonic()))
            except queue.Empty as error:
                raise RuntimeError(f"Codex {method} timed out") from error
            if message is None or isinstance(message, Exception):
                raise RuntimeError(f"Codex app server stopped during {method}")
            if message.get("id") != request_id:
                continue
            if "error" in message:
                raise RuntimeError(f"Codex {method} failed: {message['error']}")
            return message["result"]

    def __enter__(self):
        try:
            self.request("initialize", {
                "clientInfo": {"name": "tj_agents_hook_trust", "version": "1.0.0"},
                "capabilities": {"experimentalApi": True},
            })
            self.send({"method": "initialized"})
            return self
        except BaseException:
            self.close()
            raise

    def close(self):
        if self.process.poll() is None:
            self.process.terminate()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait()
        self.process.stdin.close()
        self.process.stdout.close()

    def __exit__(self, *_):
        self.close()


def source_package(plugin, cwd, published=None):
    marketplace = plugin.get("marketplaceSource", {})
    repository = github_repository(marketplace.get("source"))
    if marketplace.get("sourceType") != "git" or not repository or repository[0] != TRUSTED_OWNER:
        return None
    source = plugin.get("source", {})
    if source.get("source") == "git-subdir":
        publisher = github_repository(source.get("url"))
        relative = source.get("path")
        ref = source.get("sha") or source.get("ref") or "main"
        if (
            not publisher or publisher[0] != TRUSTED_OWNER
            or not isinstance(relative, str) or not relative or "\\" in relative
            or PurePosixPath(relative).is_absolute() or ".." in PurePosixPath(relative).parts
            or not isinstance(ref, str) or not ref or ref.startswith("-")
        ):
            raise RuntimeError(f"Invalid published plugin source: {plugin['pluginId']}")
        key = (publisher, ref, relative)
        published = {} if published is None else published
        if key not in published:
            with tempfile.TemporaryDirectory(prefix="tj-agents-hook-source-") as directory:
                root = Path(directory)
                run(["git", "init", "--quiet", str(root)], cwd)
                run(["git", "-C", str(root), "fetch", "--depth=1",
                     f"https://github.com/{publisher[0]}/{publisher[1]}.git", ref], cwd)
                revision = run(["git", "-C", str(root), "rev-parse", "FETCH_HEAD"], cwd)
                published[key] = committed_files(root, revision, relative, cwd)
        return published[key]
    if source.get("source") != "local" or not source.get("path"):
        return None
    path = Path(source["path"]).resolve(strict=True)
    root = Path(run(["git", "-C", str(path), "rev-parse", "--show-toplevel"], cwd)).resolve()
    origin = run(["git", "-C", str(root), "remote", "get-url", "origin"], cwd)
    if github_repository(origin) != repository:
        raise RuntimeError(f"Plugin source origin differs from its marketplace: {plugin['pluginId']}")
    published = {} if published is None else published
    if root not in published:
        head = run(["git", "-C", str(root), "rev-parse", "HEAD"], cwd)
        remote = run([
            "git", "-C", str(root), "ls-remote", f"https://github.com/{repository[0]}/{repository[1]}.git",
            "HEAD", "refs/heads/*", "refs/tags/*",
        ], cwd)
        if head not in {line.split()[0] for line in remote.splitlines() if line.split()}:
            raise RuntimeError(f"Plugin source revision is not published by tj-agents: {plugin['pluginId']}")
        published[root] = head
    relative = path.relative_to(root).as_posix()
    changes = run([
        "git", "-C", str(root), "status", "--porcelain", "--untracked-files=all", "--", relative,
    ], cwd)
    if changes:
        raise RuntimeError(f"Plugin source has local changes: {plugin['pluginId']}")
    return committed_files(root, published[root], relative, cwd)


def normalized_bytes(content):
    try:
        return content.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n").encode("utf-8")
    except UnicodeDecodeError:
        return content


def committed_files(root, revision, relative, cwd):
    tree = revision if relative == "." else f"{revision}:{relative}"
    archive = subprocess.run(
        ["git", "-C", str(root), "archive", "--format=tar", tree],
        cwd=cwd, check=True, capture_output=True, timeout=TIMEOUT,
    ).stdout
    result = {}
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as tree:
        for entry in tree:
            if entry.isdir():
                continue
            if not entry.isfile() or Path(entry.name).is_absolute() or ".." in Path(entry.name).parts:
                raise RuntimeError("Published plugin package contains an unsupported entry")
            result[entry.name] = normalized_bytes(tree.extractfile(entry).read())
    return result


def package_files(root):
    result = {}
    for path in root.rglob("*"):
        if path.is_symlink():
            raise RuntimeError(f"Plugin package contains a symbolic link: {path}")
        if path.is_file():
            result[path.relative_to(root).as_posix()] = normalized_bytes(path.read_bytes())
    return result


def installed_package(hook):
    path = Path(hook["sourcePath"]).resolve(strict=True)
    for parent in path.parents:
        if (parent / ".codex-plugin/plugin.json").is_file() or (parent / "plugin.json").is_file():
            return parent
    raise RuntimeError(f"Cannot identify installed plugin package: {hook['pluginId']}")


def trust_updates(inventory, entries, cwd, verified_roots=None):
    verified_roots = {root.identity: root for root in verified_roots or ()}
    candidates = {}
    for plugin in inventory.get("installed", []):
        if not isinstance(plugin, dict):
            continue
        if plugin.get("installed") and plugin.get("enabled"):
            marketplace = plugin.get("marketplaceSource", {})
            repository = github_repository(marketplace.get("source"))
            if marketplace.get("sourceType") == "git" and repository and repository[0] == TRUSTED_OWNER:
                candidates[plugin["pluginId"]] = plugin
            elif marketplace.get("sourceType") == "local" and plugin.get("pluginId") in verified_roots:
                candidates[plugin["pluginId"]] = plugin
    updates = {}
    verified = set()
    published = {}
    for entry in entries:
        if Path(entry["cwd"]).resolve() != cwd:
            continue
        if entry.get("errors"):
            raise RuntimeError("Codex could not load all hook definitions")
        for hook in entry.get("hooks", []):
            identity = hook.get("pluginId")
            if (
                identity not in candidates or hook.get("source") != "plugin"
                or hook.get("isManaged") or not hook.get("enabled")
                or hook.get("trustStatus") not in {"untrusted", "modified"}
            ):
                continue
            package = installed_package(hook)
            if (identity, package) not in verified:
                provenance = verified_roots.get(identity)
                if provenance is not None:
                    source = package_files(provenance.root)
                else:
                    source = source_package(candidates[identity], cwd, published)
                if source is None or package_files(package) != source:
                    raise RuntimeError(f"Installed plugin differs from its Git source: {identity}")
                verified.add((identity, package))
            key, current_hash = hook["key"], hook["currentHash"]
            if not key.startswith(identity + ":") or not re.fullmatch(
                r"sha256:[0-9a-f]{64}(?:/sha256:[0-9a-f]{64})?", current_hash
            ):
                raise RuntimeError(f"Invalid native hook identity: {identity}")
            updates[key] = {"trusted_hash": current_hash}
    return updates


def signature(hook):
    return hook.get("eventName"), hook.get("matcher"), hook.get("handlerType"), hook.get("command")


def required_definitions(root, identity):
    root = root.resolve()
    manifest = json.loads((root / ".codex-plugin/plugin.json").read_text(encoding="utf-8-sig"))
    sources = manifest.get("hooks")
    if sources is None:
        sources = ["hooks/hooks.json"] if (root / "hooks/hooks.json").is_file() else []
    if not isinstance(sources, list):
        sources = [sources]
    profile = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
    data = profile / "plugins/data" / identity.replace("@", "-")
    result = []
    for source in sources:
        if isinstance(source, str):
            path = (root / source).resolve()
            if not path.is_relative_to(root):
                raise RuntimeError(f"Required hook declaration escapes its plugin: {identity}")
            source = json.loads(path.read_text(encoding="utf-8-sig"))
        events = source.get("hooks", source)
        for event, groups in events.items():
            for group in groups:
                for handler in group["hooks"]:
                    kind = handler["type"]
                    parts = kind.split("_")
                    native_kind = parts[0] + "".join(part.title() for part in parts[1:])
                    cmd = handler.get("commandWindows", handler.get("command")) if os.name == "nt" else handler.get("command")
                    if isinstance(cmd, str):
                        for variable, value in (("PLUGIN_ROOT", root), ("CLAUDE_PLUGIN_ROOT", root),
                                                ("PLUGIN_DATA", data), ("CLAUDE_PLUGIN_DATA", data)):
                            cmd = cmd.replace("${" + variable + "}", str(value))
                    result.append((event[0].lower() + event[1:], group.get("matcher"), native_kind, cmd))
    return result


def require_ready(listing, cwd, required=None, expected=None):
    entries = [entry for entry in listing.get("data", [])
               if Path(entry.get("cwd", "")).resolve() == cwd]
    if not entries or any(entry.get("errors") for entry in entries):
        raise RuntimeError("Required hook inventory is missing or contains loading errors")
    hooks = {hook["key"]: hook for entry in entries for hook in entry.get("hooks", [])}
    problems = []
    for identity, definitions in (required or {}).items():
        present = Counter(signature(hook) for hook in hooks.values() if hook.get("pluginId") == identity)
        missing = Counter(definitions) - present
        if missing:
            problems.append(f"{identity}: required hook definitions missing: {list(missing.elements())}")
    for key, hook in hooks.items():
        if required is not None and hook.get("pluginId") not in required:
            continue
        if not hook.get("enabled") and required is not None:
            problems.append(f"{key}: disabled")
        elif hook.get("enabled") and not hook.get("isManaged") and hook.get("trustStatus") != "trusted":
            problems.append(f"{key}: {hook.get('trustStatus', 'unknown trust')}")
    for key, value in (expected or {}).items():
        if key not in hooks or hooks[key].get("currentHash") != value["trusted_hash"]:
            problems.append(f"{key}: contents changed during verification")
    if problems:
        raise RuntimeError("Required hooks are not ready: " + "; ".join(problems)
                           + ". Stop ordinary work; repair and verify these hooks in /hooks.")


def apply_trust(executable, cwd, preview=False, verified_roots=None, launch_args=(), environment=None,
                timeout=None, required=None):
    if required == {}:
        return {}
    limit = TIMEOUT if timeout is None else timeout
    if limit <= 0:
        raise RuntimeError("Codex hook trust deadline expired")
    with AppServer(executable, cwd, launch_args, environment, timeout=limit) as server:
        config = server.request("config/read", {"cwd": str(cwd), "includeLayers": False})["config"]
        features = config.get("features") or {}
        if features.get("hooks", features.get("codex_hooks", True)) is False:
            raise RuntimeError("Required hooks are disabled in the effective Codex configuration")
        if config.get("allow_managed_hooks_only") is True:
            raise RuntimeError("Required plugin hooks are excluded by allow_managed_hooks_only")
        listing = server.request("hooks/list", {"cwds": [str(cwd)]})
        if required is not None:
            listing = {"data": [dict(entry, hooks=[hook for hook in entry.get("hooks", [])
                                                   if hook.get("pluginId") in required])
                                for entry in listing.get("data", [])]}
        if not any(
            hook.get("source") == "plugin" and hook.get("enabled")
            and hook.get("trustStatus") in {"untrusted", "modified"}
            for entry in listing["data"] for hook in entry.get("hooks", [])
        ):
            require_ready(listing, cwd, required)
            return {}
        if not launch_args and not environment:
            inventory = json.loads(run([*command(executable), "plugin", "list", "--json"], cwd, limit))
        else:
            completed = subprocess.run(
                [*command(executable), *launch_args, "plugin", "list", "--json"], cwd=cwd,
                env=dict(os.environ, **(environment or {})), check=True, capture_output=True,
                text=True, encoding="utf-8", timeout=limit,
            )
            inventory = json.loads(completed.stdout)
        updates = trust_updates(inventory, listing["data"], cwd, verified_roots)
        if updates and not preview:
            server.request("config/batchWrite", {
                "edits": [{"keyPath": "hooks.state", "value": updates, "mergeStrategy": "upsert"}],
                "reloadUserConfig": True,
            })
            listing = server.request("hooks/list", {"cwds": [str(cwd)]})
        if not preview:
            require_ready(listing, cwd, required, expected=updates)
        return updates


def reload_running(executable, cwd, required, session_id=None, timeout=TIMEOUT):
    identity = session_id or os.environ.get("CODEX_THREAD_ID")
    if not identity or not required:
        return
    deadline = time.monotonic() + timeout
    with AppServer(executable, cwd, timeout=timeout, existing=True) as server:
        if identity not in server.request("thread/loaded/list", {}).get("data", []):
            raise RuntimeError("Current Codex conversation is not loaded in the existing daemon; "
                               "resume this same conversation in a fresh native process")
        thread = server.request("thread/read", {"threadId": identity, "includeTurns": False})["thread"]
        if thread.get("id") != identity or Path(thread.get("cwd", "")).resolve() != cwd:
            raise RuntimeError("Existing Codex conversation does not match the refresh checkout")
        server.request("config/batchWrite", {"edits": [], "reloadUserConfig": True})
        while True:
            listing = server.request("hooks/list", {"cwds": [str(cwd)]})
            try:
                require_ready(listing, cwd, required)
                return
            except RuntimeError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.1)


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--codex", required=True)
    parser.add_argument("--project", type=Path, default=Path.cwd())
    parser.add_argument("--preview", action="store_true")
    arguments = parser.parse_args(argv)
    try:
        updates = apply_trust(arguments.codex, arguments.project.resolve(strict=True), arguments.preview)
        if updates:
            action = "Would trust" if arguments.preview else "Trusted"
            print(f"standards: {action} {len(updates)} tj-agents hook definitions")
        return 0
    except (OSError, ValueError, RuntimeError, tarfile.TarError, subprocess.SubprocessError) as error:
        print(f"standards: tj-agents hook trust failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
