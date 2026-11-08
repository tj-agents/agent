import contextlib
import importlib.util
import io
import json
import os
from collections.abc import Iterator
from contextlib import ExitStack
from dataclasses import asdict, dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import mock

import pytest
import test_codex_followup as followup
import test_native_completion as completion
import test_startup_binding as startup
from tj_agent.host import Host


@pytest.fixture
def binding() -> Iterator[Any]:
    with ExitStack() as stack:
        fixture: Any = startup.StartupBindingTests()
        fixture.addCleanup = stack.callback
        fixture.setUp()
        stack.enter_context(
            mock.patch.object(
                startup.SESSIONS[Host.CODEX],
                "handoff_executable",
                side_effect=lambda target, _project, _environment, **_options: target,
            )
        )
        yield fixture.binding


@pytest.fixture
def caller() -> Iterator[Any]:
    with ExitStack() as stack:
        fixture: Any = followup.FollowupTests()
        fixture.addCleanup = stack.callback
        fixture.setUp()
        yield fixture


def test_owned_backend_prepare_failure_never_starts_a_frontend(binding: Any) -> None:
    with (
        mock.patch.object(
            startup.SESSIONS[Host.CODEX],
            "prepare_frontend",
            side_effect=RuntimeError("owned endpoint unavailable"),
        ),
        mock.patch.object(binding.subprocess, "Popen") as spawn,
        pytest.raises(binding.StartupBindingError, match="could not be prepared"),
    ):
        binding.run_session(
            Host.CODEX,
            "native",
            Path.cwd(),
            [],
            require_session_start=True,
            hook_roots={},
        )
    spawn.assert_not_called()


def test_handoff_runtime_failure_prevents_plugin_checks_and_native_launch(
    binding: Any, tmp_path: Path
) -> None:
    with (
        mock.patch.object(
            startup.SESSIONS[Host.CODEX],
            "handoff_executable",
            side_effect=RuntimeError("Codex handoff requires native Codex 0.166.0"),
        ),
        mock.patch.object(binding.required_plugins, "check_launch_roots") as plugins,
        mock.patch.object(binding, "run_session") as launch,
        contextlib.redirect_stderr(io.StringIO()) as error,
    ):
        assert (
            binding.main(
                [
                    "--host",
                    "codex",
                    "--project",
                    str(tmp_path),
                    "--target",
                    "native",
                    "--require-session-start",
                ]
            )
            == 1
        )
    assert "0.166.0" in error.getvalue()
    plugins.assert_not_called()
    launch.assert_not_called()


def test_handoff_runtime_is_shared_by_preflight_backend_and_frontend(
    binding: Any, tmp_path: Path
) -> None:
    adapter = startup.SESSIONS[Host.CODEX]
    process = mock.Mock(pid=72)
    process.wait.return_value = 0
    with (
        mock.patch.object(adapter, "handoff_executable", return_value="frozen-native"),
        mock.patch.object(
            binding.required_plugins, "check_launch_roots", return_value=(tmp_path, {})
        ) as plugins,
        mock.patch.object(
            binding.host_plugins.PLUGINS[Host.CODEX], "ensure_hooks"
        ) as hooks,
        mock.patch.object(
            adapter, "prepare_frontend", return_value=([], {}, {})
        ) as prepare,
        mock.patch.object(adapter, "attach_frontend"),
        mock.patch.object(adapter, "release_frontend"),
        mock.patch.object(
            adapter, "handoff_recipient", return_value={"token": "fixture"}
        ),
        mock.patch.object(binding, "_acknowledged", return_value=True),
        mock.patch.object(binding, "_prelaunch_terminal", return_value=None),
        mock.patch.object(binding.subprocess, "Popen", return_value=process) as spawn,
    ):
        assert (
            binding.main(
                [
                    "--host",
                    "codex",
                    "--project",
                    str(tmp_path),
                    "--target",
                    "old-native",
                    "--require-session-start",
                ]
            )
            == 0
        )
    assert [call.args[2] for call in plugins.call_args_list] == ["frozen-native"]
    hooks.assert_called_once_with(tmp_path.resolve(), "frozen-native")
    assert prepare.call_args.kwargs["executable"] == "frozen-native"
    assert spawn.call_args.args[0] == ["frozen-native"]


def test_ordinary_launch_does_not_resolve_a_handoff_runtime(binding: Any) -> None:
    adapter = startup.SESSIONS[Host.CODEX]
    process = mock.Mock(pid=72)
    process.wait.return_value = 0
    with (
        mock.patch.object(adapter, "handoff_executable") as resolve,
        mock.patch.object(adapter, "prepare_frontend", return_value=([], {}, None)),
        mock.patch.object(adapter, "attach_frontend"),
        mock.patch.object(adapter, "release_frontend"),
        mock.patch.object(binding.subprocess, "Popen", return_value=process),
    ):
        assert binding.run_session(Host.CODEX, "native", Path.cwd(), []) == 0
    resolve.assert_not_called()


@pytest.mark.parametrize("directory_option", ["relative", "absolute", "none"])
def test_native_directory_is_resolved_once_through_owned_startup(
    binding: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    directory_option: str,
) -> None:
    adapter = startup.SESSIONS[Host.CODEX]
    backend = __import__("codex.backend", fromlist=["Lease"])
    original = tmp_path / "original checkout"
    selected = original / "selected café"
    selected.mkdir(parents=True)
    effective = original if directory_option == "none" else selected
    arguments = {
        "relative": ["-C", selected.name],
        "absolute": ["--cd", str(selected)],
        "none": [],
    }[directory_option] + ["-c", 'model="fixture"', "prompt"]
    monkeypatch.setenv("AGENT_STATE_DIRECTORY", str(tmp_path / "state"))
    child = mock.Mock(pid=72)
    child.wait.return_value = 0
    lease = mock.Mock(backend={"kind": "handoff", "socket_path": "/fixture/s"})

    def roots(
        project: Path, host: Any, _target: str, native: list[str]
    ) -> tuple[Any, dict[str, Path]]:
        return binding.required_plugins.launch_context(project, host, native)[0], {}

    with (
        mock.patch.object(
            binding.required_plugins, "check_launch_roots", side_effect=roots
        ),
        mock.patch.object(
            binding.host_plugins.PLUGINS[Host.CODEX], "ensure_hooks"
        ) as hooks,
        mock.patch.object(backend, "Lease", return_value=lease) as prepare_backend,
        mock.patch.object(adapter.client, "await_backend") as readiness,
        mock.patch.object(adapter, "attach_frontend"),
        mock.patch.object(
            adapter, "handoff_recipient", return_value={"token": "fixture"}
        ),
        mock.patch.object(binding, "_acknowledged", return_value=True),
        mock.patch.object(binding, "_prelaunch_terminal", return_value=None),
        mock.patch.object(binding.subprocess, "Popen", return_value=child) as frontend,
    ):
        assert (
            binding.main(
                [
                    "--host",
                    "codex",
                    "--project",
                    str(original),
                    "--target",
                    "native",
                    "--require-session-start",
                    "--",
                    *arguments,
                ]
            )
            == 0
        )

    assert prepare_backend.call_args.args[1] == effective.resolve()
    assert prepare_backend.call_args.args[4] == ["-c", 'model="fixture"']
    assert readiness.call_args.args[0]["launch_cwd"] == str(effective.resolve())
    assert frontend.call_args.kwargs["cwd"] == original.resolve()
    assert frontend.call_args.args[0][-len(arguments) :] == arguments
    hooks.assert_called_once_with(effective.resolve(), "native")


@pytest.mark.parametrize(
    "scenario",
    [
        "capable",
        "managed",
        "missing",
        "unreachable",
        "mismatch",
        "relative",
        "old",
        "cli-mismatch",
        "probe-failed",
        "prerelease",
        "invalid-json",
    ],
)
def test_handoff_runtime_uses_only_verified_public_version_metadata(
    tmp_path: Path, scenario: str
) -> None:
    adapter = startup.SESSIONS[Host.CODEX]
    selected = tmp_path / "selected codex.exe"
    managed = tmp_path / "managed café codex.exe"
    for path in (selected, managed):
        path.write_bytes(b"native fixture")
        path.chmod(0o700)
    selected_version = "0.166.0" if scenario == "capable" else "0.163.0-alpha.1"
    metadata = {
        "status": "running",
        "cliVersion": selected_version,
        "managedCodexPath": str(managed),
        "managedCodexVersion": "0.166.0",
    }
    if scenario == "missing":
        metadata.pop("managedCodexPath")
    elif scenario == "relative":
        metadata["managedCodexPath"] = "codex.exe"
    elif scenario == "cli-mismatch":
        metadata["cliVersion"] = "0.162.0"
    installed = {"old": "0.165.0", "prerelease": "0.166.0-alpha.1"}.get(
        scenario, "0.166.0"
    )
    metadata["managedCodexVersion"] = "0.167.0" if scenario == "mismatch" else installed
    environment = {"CODEX_HOME": str(tmp_path / "selected profile")}

    def native(arguments: list[str], **kwargs: Any) -> SimpleNamespace:
        assert kwargs["env"] == environment
        assert kwargs["cwd"] == tmp_path
        assert kwargs["timeout"] == 15
        if arguments == [str(selected.resolve()), "--version"]:
            return SimpleNamespace(returncode=0, stdout=f"codex-cli {selected_version}")
        if arguments == [str(managed.resolve()), "--version"]:
            return SimpleNamespace(
                returncode=int(scenario == "probe-failed"),
                stdout=f"codex-cli {installed}",
            )
        assert arguments == [str(selected.resolve()), "app-server", "daemon", "version"]
        if scenario == "unreachable":
            raise adapter.subprocess.TimeoutExpired(arguments, 15)
        return SimpleNamespace(
            returncode=0,
            stdout="invalid" if scenario == "invalid-json" else json.dumps(metadata),
        )

    with mock.patch.object(
        adapter.handoff_runtime.process, "run", side_effect=native
    ) as run:
        if scenario in {"capable", "managed"}:
            assert adapter.handoff_executable(
                str(selected), tmp_path, environment
            ) == str((selected if scenario == "capable" else managed).resolve())
            assert run.call_count == (1 if scenario == "capable" else 3)
        else:
            with pytest.raises(RuntimeError, match=r"requires.*0\.166\.0.*paginated"):
                adapter.handoff_executable(str(selected), tmp_path, environment)

    if scenario == "managed":
        with (
            mock.patch.object(
                adapter.handoff_runtime.process, "run", side_effect=native
            ) as run,
            pytest.raises(RuntimeError, match="selected runtime"),
        ):
            adapter.handoff_executable(
                str(selected), tmp_path, environment, allow_managed=False
            )
        run.assert_called_once()


@pytest.mark.parametrize("host", [Host.CLAUDE, Host.CODEX])
def test_required_hook_failure_preserves_each_host_startup_contract(
    binding: Any,
    host: Any,
) -> None:
    adapter = startup.SESSIONS[host]
    process = mock.Mock(pid=72)
    process.wait.return_value = 0
    with (
        mock.patch.object(
            binding.required_plugins,
            "check_launch_roots",
            side_effect=RuntimeError("hook fixture failed"),
        ),
        mock.patch.object(adapter, "prepare_frontend", return_value=([], {}, None)),
        mock.patch.object(adapter, "attach_frontend"),
        mock.patch.object(adapter, "release_frontend"),
        mock.patch.object(binding.subprocess, "Popen", return_value=process) as spawn,
    ):
        if host == Host.CODEX:
            with pytest.raises(
                binding.StartupBindingError,
                match="required hooks could not be verified",
            ):
                binding.run_session(
                    host, "native", Path.cwd(), [], require_session_start=True
                )
            spawn.assert_not_called()
        else:
            assert (
                binding.run_session(
                    host, "native", Path.cwd(), [], require_session_start=True
                )
                == 0
            )
            spawn.assert_called_once()


def test_owned_frontend_attach_failure_stops_child_and_releases_lease(
    binding: Any,
) -> None:
    process = mock.Mock(pid=72)
    process.poll.return_value = None
    ticket = {"token": "fixture"}
    with (
        mock.patch.object(binding.subprocess, "Popen", return_value=process),
        mock.patch.object(
            startup.SESSIONS[Host.CODEX],
            "prepare_frontend",
            return_value=([], {}, ticket),
        ),
        mock.patch.object(
            startup.SESSIONS[Host.CODEX],
            "attach_frontend",
            side_effect=OSError("attach failed"),
        ),
        mock.patch.object(startup.SESSIONS[Host.CODEX], "release_frontend") as release,
        pytest.raises(binding.StartupBindingError, match="could not be attached"),
    ):
        binding.run_session(
            Host.CODEX,
            "native",
            Path.cwd(),
            [],
            require_session_start=True,
            hook_roots={},
        )
    process.terminate.assert_called_once()
    release.assert_called_once_with(ticket)


def test_ack_without_recipient_stays_one_unconfirmed_launch(binding: Any) -> None:
    process = mock.Mock(pid=72)
    process.poll.return_value = None
    process.wait.return_value = 0
    receipt = {"token": "verified", "recipient": {"thread_id": "exact"}}
    running, unconfirmed = mock.Mock(), mock.Mock()
    with (
        mock.patch.object(binding.subprocess, "Popen", return_value=process) as spawn,
        mock.patch.object(
            startup.SESSIONS[Host.CODEX],
            "prepare_frontend",
            return_value=([], {}, {}),
        ),
        mock.patch.object(startup.SESSIONS[Host.CODEX], "attach_frontend"),
        mock.patch.object(startup.SESSIONS[Host.CODEX], "release_frontend"),
        mock.patch.object(
            startup.SESSIONS[Host.CODEX],
            "handoff_recipient",
            side_effect=[None, receipt],
        ),
        mock.patch.object(binding, "_acknowledged", return_value=True),
        mock.patch.object(binding.time, "sleep"),
    ):
        assert (
            binding.run_session(
                Host.CODEX,
                "native",
                Path.cwd(),
                [],
                require_session_start=True,
                hook_roots={},
                timeout=0,
                on_running=running,
                on_unconfirmed=unconfirmed,
            )
            == 0
        )
    spawn.assert_called_once()
    unconfirmed.assert_called_once_with(72)
    running.assert_called_once_with(72, client_binding=receipt)
    process.terminate.assert_not_called()


def test_old_followup_receipt_never_opens_transport(caller: Any) -> None:
    caller.receipt["version"] = 1
    caller.receipt_path.write_text(json.dumps(caller.receipt), encoding="utf-8")
    with (
        mock.patch.object(followup.FOLLOWUP, "Proxy") as proxy,
        contextlib.redirect_stdout(io.StringIO()),
    ):
        assert followup.FOLLOWUP.main(caller.arguments()) == 2
    proxy.assert_not_called()


def owned_receipt(caller: Any) -> dict[str, Any]:
    return {
        "token": "a" * 64,
        "recipient": {"thread_id": followup.THREAD, "cwd": caller.receipt["cwd"]},
        "profile": caller.receipt["codex_home"],
        "backend": {"socket_path": "fixture-socket"},
        "frontend_message_id": "frontend-anchor",
        "executable": "fixture-native",
    }


def test_new_followup_resolves_explicit_launch_receipt_once(caller: Any) -> None:
    frozen = owned_receipt(caller)
    runtime = mock.Mock()
    runtime.recipient_binding.return_value = frozen
    runtime.launch_binding.return_value = frozen
    runtime.frozen_binding.return_value = frozen
    runtime.native_contains.return_value = True
    proxy = followup.Session(caller.receipt)
    with (
        mock.patch.object(followup.FOLLOWUP, "client_runtime", return_value=runtime),
        mock.patch.object(followup.FOLLOWUP, "Proxy", return_value=proxy) as connect,
        mock.patch.object(followup.FOLLOWUP, "responsibility", return_value="owner"),
        contextlib.redirect_stdout(io.StringIO()),
    ):
        assert (
            followup.FOLLOWUP.main(
                [
                    *caller.arguments(),
                    "--launch-receipt",
                    str(caller.root / "launch.json"),
                ]
            )
            == 0
        )
    runtime.recipient_binding.assert_called_once()
    runtime.launch_binding.assert_called_once()
    assert connect.call_args.kwargs["binding"] == frozen
    receipt = json.loads(caller.receipt_path.read_text(encoding="utf-8"))
    assert receipt["launch_binding"] == frozen


def test_followup_retry_uses_frozen_endpoint_without_resolving_launch_again(
    caller: Any,
) -> None:
    frozen = owned_receipt(caller)
    caller.receipt["launch_binding"] = frozen
    caller.receipt_path.write_text(json.dumps(caller.receipt), encoding="utf-8")
    runtime = mock.Mock()
    runtime.frozen_binding.return_value = frozen
    runtime.native_contains.return_value = True
    proxy = followup.Session(caller.receipt)
    with (
        mock.patch.object(followup.FOLLOWUP, "client_runtime", return_value=runtime),
        mock.patch.object(followup.FOLLOWUP, "Proxy", return_value=proxy) as connect,
        contextlib.redirect_stdout(io.StringIO()),
    ):
        assert (
            followup.FOLLOWUP.main(
                [
                    *caller.arguments(),
                    "--launch-receipt",
                    str(caller.root / "another-launch.json"),
                ]
            )
            == 0
        )
    runtime.launch_binding.assert_not_called()
    assert connect.call_args.kwargs["binding"] == frozen
    assert not any(method.startswith("turn/") for method, _ in proxy.calls)


def test_missing_owned_backend_preserves_receipt_without_opening_shared_proxy(
    caller: Any,
) -> None:
    caller.receipt["launch_binding"] = owned_receipt(caller)
    caller.receipt_path.write_text(json.dumps(caller.receipt), encoding="utf-8")
    before = caller.receipt_path.read_bytes()
    runtime = mock.Mock()
    runtime.frozen_binding.side_effect = RuntimeError("backend identity changed")
    with (
        mock.patch.object(followup.FOLLOWUP, "client_runtime", return_value=runtime),
        mock.patch.object(followup.FOLLOWUP, "Proxy") as connect,
        contextlib.redirect_stdout(io.StringIO()),
    ):
        assert (
            followup.FOLLOWUP.main(["status", "--receipt", str(caller.receipt_path)])
            == 2
        )
    connect.assert_not_called()
    assert caller.receipt_path.read_bytes() == before


def test_native_followup_completion_keeps_frontend_anchor_separate() -> None:
    with ExitStack() as stack:
        fixture: Any = completion.NativeCompletionTests()
        fixture.addCleanup = stack.callback
        fixture.setUp()
        fixture.entry["frontend_message_id"] = "input"
        fixture.entry["client_message_id"] = "native-followup"
        fixture.entry["native_turn_id"] = "followup-turn"
        fixture.event("task_complete")
        fixture.event("task_started", turn="followup-turn")
        fixture.event(
            "item_completed",
            turn="followup-turn",
            item={"type": "UserMessage", "client_id": "native-followup"},
        )
        guard = completion.cli_client.NativeCompletion(fixture.entry)
        assert not guard.poll()
        fixture.event("task_complete", turn="followup-turn")
        assert guard.poll()
        fixture.event("task_started", turn="later-native-turn")
        with pytest.raises(RuntimeError, match="new native turn"):
            guard.poll()


def test_exact_native_command_line_read_never_starts_a_shell() -> None:
    client = startup.SESSIONS[Host.CODEX].client
    with mock.patch.object(
        client.subprocess, "run", side_effect=AssertionError("unexpected shell")
    ):
        arguments = client.process_arguments(os.getpid())
    assert arguments
    assert Path(arguments[0]).stem.casefold().startswith("python")


def load_attachment_source(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@dataclass
class Attachment:
    sessions: Any
    registration: Any
    store_module: Any
    record: dict[str, Any]
    handle: str

    def snapshot(self) -> dict[str, Any] | None:
        with self.store_module.SessionStore() as store:
            session = store.get_session(self.handle)
            return None if session is None else asdict(session)

    def attach(self) -> Any:
        return self.sessions.handoff_recipient(self.record, self.registration, None)

    def change(self, changed: str) -> None:
        with self.store_module.SessionStore() as store:
            if changed == "missing":
                store.delete_session("thread")
            elif changed == "ended":
                store.update_session(
                    self.handle,
                    lambda session: session.end(
                        "handed_off", successor="codex:successor"
                    ),
                )
            else:
                store.update_session(
                    self.handle,
                    lambda session: setattr(
                        session, "profile", session.profile + "-other"
                    ),
                )


@pytest.fixture
def attachment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Attachment:
    root = Path(__file__).resolve().parents[1]
    directory = tmp_path / "attachment checkout café"
    directory.mkdir()
    monkeypatch.syspath_prepend(str(root / "plugins/agent/.agents/agent/runtime"))
    monkeypatch.setenv("AGENT_STATE_DIRECTORY", str(directory / "state"))
    monkeypatch.setenv("CODEX_HOME", str(directory / "profile"))
    sessions = load_attachment_source(
        "owned_attachment_sessions", root / ".codex/runtime/sessions.py"
    )
    registration = load_attachment_source(
        "owned_attachment_registration",
        root / ".agents/agent/utility/peer/scripts/register_session.py",
    )
    store_module = load_attachment_source(
        "owned_attachment_store", root / ".agents/agent-store/runtime/session_store.py"
    )
    record: dict[str, Any] = {
        "token": "a" * 64,
        "backend": {"kind": "handoff", "pid": 103},
        "frontend": {"pid": 104, "started_at": 104.0},
        "recipient": {"thread_id": "thread", "cwd": str(directory)},
        "profile": str(directory / "profile"),
        "launch_cwd": str(directory),
        "recording_path": str(directory / "events.jsonl"),
    }
    submitted = {
        "turn_id": "native-turn",
        "client_message_id": "latest-native-message",
        "frontend_message_id": "frontend-anchor",
        "native_rollout": str(directory / "rollout.jsonl"),
        "native_cwd": str(directory),
    }
    monkeypatch.setattr(
        sessions.client, "bind_recipient", lambda *_args: (record, submitted)
    )
    monkeypatch.setattr(registration, "process_table", dict)
    monkeypatch.setattr(registration, "build_process_lookup", lambda: lambda _pid: None)
    monkeypatch.setattr(registration, "_load_session_store", lambda: store_module)
    with store_module.SessionStore() as store:
        handle = store.start_session(
            "codex",
            "thread",
            str(directory),
            pid=11,
            pid_started_at=11.0,
            profile=str(Host.CODEX.configuration_root),
        )
        store.upsert_work_item(
            "fixture-work-item",
            "fixture/repository",
            "feature",
            "item",
            status="active",
        )
        store.claim_work_item(handle, "fixture-work-item", "owner")
        store.update_session(handle, lambda session: session.stopped())
    return Attachment(sessions, registration, store_module, record, str(handle))


def test_recipient_attachment_preserves_responsibility_turn_and_lifecycle(
    attachment: Attachment,
) -> None:
    before = attachment.snapshot()
    assert before is not None
    assert before["role"] == "owner"
    assert before["work_item"] == "fixture-work-item"
    assert before["turn_state"] == "awaiting_input"
    result = attachment.attach()
    after = attachment.snapshot()
    assert after == {**before, "pid": 104, "pid_started_at": 104.0}
    assert result["recipient"] == attachment.record["recipient"]


@pytest.mark.parametrize("changed", ["missing", "ended", "profile"])
def test_recipient_attachment_refuses_ineligible_registration_without_changing_it(
    attachment: Attachment, changed: str
) -> None:
    attachment.change(changed)
    before = attachment.snapshot()
    with pytest.raises(RuntimeError, match=r"Agent Store|registration"):
        attachment.attach()
    assert attachment.snapshot() == before


@pytest.mark.parametrize("changed", ["missing", "ended", "profile"])
def test_attachment_transaction_does_not_revive_or_reassign_a_changed_registration(
    attachment: Attachment, monkeypatch: pytest.MonkeyPatch, changed: str
) -> None:
    record_start = attachment.registration._record_session_start
    expected: list[dict[str, Any] | None] = []

    def changed_before_transaction(*args: Any, **kwargs: Any) -> Any:
        attachment.change(changed)
        expected.append(attachment.snapshot())
        return record_start(*args, **kwargs)

    monkeypatch.setattr(
        attachment.registration, "_record_session_start", changed_before_transaction
    )
    with pytest.raises(RuntimeError, match=r"Agent Store|registration"):
        attachment.attach()
    assert len(expected) == 1
    assert attachment.snapshot() == expected[0]


class PeerReadError(OSError):
    winerror: int


@pytest.mark.parametrize("winerror", [6, 87, 299])
@pytest.mark.parametrize("state", ["absent", "reused", "live", "unknown"])
def test_partial_peer_read_requires_fresh_identity_evidence(
    monkeypatch: pytest.MonkeyPatch, winerror: int, state: str
) -> None:
    root = Path(__file__).resolve().parents[1]
    monkeypatch.syspath_prepend(str(root / "plugins/agent/.agents/agent/runtime"))
    client = load_attachment_source(
        "owned_peer_read_client", root / ".codex/runtime/client.py"
    )
    peer = SimpleNamespace(pid=123, started_at=10.0)
    error = PeerReadError("peer process memory is unavailable")
    error.winerror = winerror
    current = SimpleNamespace(pid=123, started_at=11.0 if state == "reused" else 10.0)
    observed = ("present", current) if state in ("live", "reused") else (state, None)
    lookup = mock.Mock(return_value=observed)
    with mock.patch.object(client, "process_context", side_effect=error):
        if state in ("absent", "reused"):
            assert client.peer_recording_path(peer, lookup) is None
        else:
            with pytest.raises(OSError, match="peer process memory") as raised:
                client.peer_recording_path(peer, lookup)
            assert raised.value is error
    lookup.assert_called_once_with(peer.pid)


def test_peer_access_denied_is_never_treated_as_process_disappearance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = Path(__file__).resolve().parents[1]
    monkeypatch.syspath_prepend(str(root / "plugins/agent/.agents/agent/runtime"))
    client = load_attachment_source(
        "owned_peer_access_client", root / ".codex/runtime/client.py"
    )
    error = PeerReadError("peer process access denied")
    error.winerror = 5
    lookup = mock.Mock(return_value=("absent", None))
    with (
        mock.patch.object(client, "process_context", side_effect=error),
        pytest.raises(PeerReadError, match="access denied") as raised,
    ):
        client.peer_recording_path(SimpleNamespace(pid=123, started_at=10.0), lookup)
    assert raised.value is error
    lookup.assert_not_called()
