import re
from pathlib import Path
from urllib.parse import urlparse


_SSH_SCP_RE = re.compile(r"^[^/@\s]+@(?P<host>[^:/\s]+):(?P<path>.+)$")


def _host_and_path(url):
    if url.startswith(("ssh://", "https://", "http://", "git://")):
        parsed = urlparse(url)
        host = (parsed.hostname or "").casefold()
        return (host or None), parsed.path
    match = _SSH_SCP_RE.match(url)
    if not match:
        return None, None
    return match.group("host").casefold(), match.group("path")


def normalise_remote(url):
    url = (url or "").strip()
    if not url:
        return None
    host, path = _host_and_path(url)
    if not host or not path:
        return None
    path = path.strip("/")
    if path.endswith(".git"):
        path = path[: -len(".git")]
    if not path:
        return None
    if host in ("ssh.dev.azure.com", "vs-ssh.visualstudio.com"):
        parts = [part for part in path.split("/") if part]
        if parts and parts[0] == "v3":
            parts = parts[1:]
        if len(parts) != 3:
            return None
        org, project, repository = parts
        host = "dev.azure.com"
        path = f"{org}/{project}/_git/{repository}"
    elif host.endswith(".visualstudio.com"):
        org = host[: -len(".visualstudio.com")]
        if not org or "." in org:
            return None
        parts = [part for part in path.split("/") if part]
        if parts and parts[0].casefold() == "defaultcollection":
            parts = parts[1:]
        if len(parts) == 2 and parts[0] == "_git":
            repository = parts[1]
            project = repository
        elif len(parts) == 3 and parts[1] == "_git":
            project, _git_segment, repository = parts
        else:
            return None
        host = "dev.azure.com"
        path = f"{org}/{project}/_git/{repository}"
    return f"{host}/{path}".casefold()


def repository_identity(cwd, git):
    origin = git(cwd, "config", "--get", "remote.origin.url")
    repository = normalise_remote(origin) if origin else None
    if repository:
        return repository
    common_dir = git(cwd, "rev-parse", "--git-common-dir")
    if not common_dir:
        return None
    path = Path(common_dir)
    if not path.is_absolute():
        path = Path(cwd) / path
    try:
        return str(path.resolve())
    except OSError:
        return None
