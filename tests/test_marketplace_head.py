import base64
import io
import json
import subprocess
import urllib.error
import urllib.request
import urllib.response
from dataclasses import dataclass, field
from email.message import Message
from pathlib import Path

import marketplace_head
import pytest
from tj_cli import process

OLD_HEAD = "a" * 40
NEW_HEAD = "b" * 40
SECRET = "fixture-password-not-a-token-prefix"


@dataclass
class Git:
    origin: str = "https://github.com/team/market.git"
    credentials: str = f"username=ordinary-user\npassword={SECRET}\n"
    credential_status: int = 0
    failure: bool = False
    calls: list[list[str]] = field(default_factory=list)

    def run(
        self, command: list[str], **options: object
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append(command)
        assert options["timeout"] == marketplace_head.TIMEOUT_SECONDS
        environment = options["env"]
        assert isinstance(environment, dict)
        assert environment["GIT_CONFIG_GLOBAL"] == "caller-config"
        assert environment["GIT_TERMINAL_PROMPT"] == "0"
        assert environment["GCM_INTERACTIVE"] == "Never"
        arguments = command[3:]
        if arguments == ["rev-parse", "HEAD"]:
            return subprocess.CompletedProcess(command, 0, OLD_HEAD, "")
        if arguments == ["remote", "get-url", "origin"]:
            return subprocess.CompletedProcess(command, 0, self.origin, "")
        assert arguments == ["credential", "fill"]
        assert options["capture_output"] is True
        assert (
            options["input"]
            == "protocol=https\nhost=github.com\npath=team/market.git\n\n"
        )
        if self.failure:
            raise subprocess.TimeoutExpired(command, 5, output=SECRET, stderr=SECRET)
        return subprocess.CompletedProcess(
            command, self.credential_status, self.credentials, SECRET
        )


@dataclass
class API:
    body: bytes = field(default_factory=lambda: json.dumps({"sha": OLD_HEAD}).encode())
    status: int = 200
    failure: bool = False
    requests: list[urllib.request.Request] = field(default_factory=list)

    def open(self, request: urllib.request.Request) -> urllib.response.addinfourl:
        self.requests.append(request)
        if self.failure:
            raise urllib.error.URLError(f"secret-bearing exception: {SECRET}")
        headers = Message()
        if self.status == 302:
            headers["Location"] = "https://other.example/steal"
        return Response(io.BytesIO(self.body), headers, request.full_url, self.status)


class Response(urllib.response.addinfourl):
    msg = "fixture response"


@pytest.fixture
def transport(monkeypatch: pytest.MonkeyPatch) -> tuple[Git, API]:
    git = Git()
    api = API()

    def https_open(
        _handler: urllib.request.HTTPSHandler, request: urllib.request.Request
    ) -> urllib.response.addinfourl:
        assert request.timeout == marketplace_head.TIMEOUT_SECONDS
        return api.open(request)

    monkeypatch.setattr(process, "run", git.run)
    monkeypatch.setattr(urllib.request.HTTPSHandler, "https_open", https_open)
    return git, api


def behind(tmp_path: Path) -> bool:
    return marketplace_head.behind(tmp_path, {"GIT_CONFIG_GLOBAL": "caller-config"})


@pytest.mark.parametrize(
    "origin",
    [
        "https://github.com/team/market.git",
        "https://userinfo:ignored-secret@github.com/team/market",
        "git@github.com:team/market.git",
        "github.com:team/market.git",
        "git@GitHub.com:team/market.git",
        "ssh://git@github.com/team/market.git",
        "ssh://git@github.com:22/team/market.git",
    ],
)
def test_fresh_head_uses_configured_git_credentials_and_fixed_api_host(
    tmp_path: Path, transport: tuple[Git, API], origin: str
) -> None:
    git, api = transport
    git.origin = origin
    assert not behind(tmp_path)
    request = api.requests[0]
    assert request.full_url == "https://api.github.com/repos/team/market/commits/HEAD"
    encoded = base64.b64encode(f"ordinary-user:{SECRET}".encode()).decode()
    assert request.get_header("Authorization") == f"Basic {encoded}"
    assert "ignored-secret" not in str(request.headers)
    assert not any("ls-remote" in call for call in git.calls)


@pytest.mark.parametrize("username", ["x-access-token", "actual-login"])
def test_installation_token_does_not_require_a_token_prefix(
    tmp_path: Path, transport: tuple[Git, API], username: str
) -> None:
    git, api = transport
    git.credentials = f"username={username}\npassword={SECRET}\n"
    assert not behind(tmp_path)
    expected = base64.b64encode(f"{username}:{SECRET}".encode()).decode()
    assert api.requests[0].get_header("Authorization") == f"Basic {expected}"


@pytest.mark.parametrize("status", [0, 128])
def test_absent_git_credentials_allow_anonymous_public_head(
    tmp_path: Path, transport: tuple[Git, API], status: int
) -> None:
    git, api = transport
    git.credentials = ""
    git.credential_status = status
    assert not behind(tmp_path)
    assert api.requests[0].get_header("Authorization") is None


def test_each_check_observes_a_changed_head_without_cached_success(
    tmp_path: Path, transport: tuple[Git, API]
) -> None:
    _, api = transport
    assert not behind(tmp_path)
    api.body = json.dumps({"sha": NEW_HEAD}).encode()
    assert behind(tmp_path)
    api.body = json.dumps({"sha": OLD_HEAD}).encode()
    assert not behind(tmp_path)
    assert len(api.requests) == 3


@pytest.mark.parametrize(
    "body", [b"torn json", b"[]", b"{}", b'{"sha": null}', b'{"sha": "bad"}']
)
def test_invalid_api_response_requires_native_refresh(
    tmp_path: Path, transport: tuple[Git, API], body: bytes
) -> None:
    _, api = transport
    api.body = body
    assert behind(tmp_path)


@pytest.mark.parametrize("status", [401, 403, 404, 429, 500])
def test_api_failure_never_reports_current_or_discloses_credentials(
    tmp_path: Path,
    transport: tuple[Git, API],
    capsys: pytest.CaptureFixture[str],
    status: int,
) -> None:
    _, api = transport
    api.status = status
    api.body = SECRET.encode()
    assert behind(tmp_path)
    assert capsys.readouterr() == ("", "")


def test_redirect_does_not_forward_credentials_to_another_host(
    tmp_path: Path, transport: tuple[Git, API]
) -> None:
    _, api = transport
    api.status = 302
    assert behind(tmp_path)
    assert [request.full_url for request in api.requests] == [
        "https://api.github.com/repos/team/market/commits/HEAD"
    ]


@pytest.mark.parametrize(
    "failure", ["credential-timeout", "http-error", "partial", "helper-failed"]
)
def test_secret_bearing_failures_are_not_diagnostic_output(
    tmp_path: Path,
    transport: tuple[Git, API],
    capsys: pytest.CaptureFixture[str],
    failure: str,
) -> None:
    git, api = transport
    if failure == "credential-timeout":
        git.failure = True
    elif failure == "http-error":
        api.failure = True
    elif failure == "partial":
        git.credentials = f"password={SECRET}"
    else:
        git.credential_status = 2
    assert behind(tmp_path)
    assert capsys.readouterr() == ("", "")


@pytest.mark.parametrize(
    "origin",
    [
        "https://other.example/team/market.git",
        "https://github.com.other.example/team/market.git",
        "https://github.com@other.example/team/market.git",
        "http://github.com/team/market.git",
        "file:///team/market",
        "https://github.com/team/../market",
        "https://github.com/team/..",
        "https://github.com/team/market?token=ignored-secret",
        "https://github.com/team/market#ignored-secret",
    ],
)
def test_unsupported_origin_does_not_request_credentials_or_http(
    tmp_path: Path, transport: tuple[Git, API], origin: str
) -> None:
    git, api = transport
    git.origin = origin
    assert behind(tmp_path)
    assert len(git.calls) == 2
    assert not api.requests
