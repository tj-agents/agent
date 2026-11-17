"""tj status over a fixture store and dummy hosts."""
import contextlib
import importlib
import io
import json
from dataclasses import dataclass, field
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
from typing import Any
import unittest
from unittest import mock

import pytest
from launch_contract import Liveness

agent_cli = importlib.import_module('agent_cli')
agent_store = importlib.import_module('agent_store')
lifecycle = importlib.import_module('lifecycle')
tj_status = importlib.import_module('tj_status')

ITEM = 'github:github.com/tj-agents/agent:98'


class DummyHost:
    def __init__(self, live):
        self.live = live
        self.respawned = []

    def liveness(self, *, strict=False):
        return self.live

    def respawn(self, background):
        self.respawned.append(background)

    def resume_arguments(self, directory, title, session_id):
        return ['resume', session_id]


class Fixture(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='lifecycle ')
        self.addCleanup(temp.cleanup)
        self.worktree = str(Path(temp.name))
        self.sessions = [
            self.session('claude', 'live', 'active'),
            self.session('claude', 'background', 'active', title='agent#98 bg'),
            self.session('codex', 'dead', 'active', title='agent#98 review', parent='claude:live'),
            self.session('codex', 'finished', 'ended'),
        ]
        self.hosts = {
            agent_cli.Host.CLAUDE: DummyHost({'live': Liveness(True, 'busy'),
                                              'background': Liveness(False, background='bg-1')}),
            agent_cli.Host.CODEX: DummyHost({}),
        }
        self.cards = {ITEM: {'ref': ITEM, 'title': 'Cut over', 'state': 'In Progress'}}
        self.launched = []
        for target, value in ((agent_store, 'run'), (lifecycle, 'tj_item'), (agent_cli, 'host_launch'),
                              (agent_cli, 'launch')):
            patcher = mock.patch.object(target, value, getattr(self, f'fake_{value}'))
            patcher.start()
            self.addCleanup(patcher.stop)

    def session(self, host, native, state, title=None, parent=None):
        return {'session': f'{host}:{native}', 'host': host, 'native_id': native, 'state': state,
                'title': title, 'parent': parent, 'worktree': self.worktree, 'cwd': self.worktree,
                'links': [{'item': {'ref': ITEM}}]}

    def fake_run(self, *arguments):
        if arguments == ('sessions',):
            return {'sessions': self.sessions}
        if arguments == ('launches',):
            return {'launches': []}
        if arguments == ('items',):
            return {'items': [{'item': {'ref': ITEM}}]}
        if arguments[:2] == ('sessions', '--item'):
            return {'item': {'ref': ITEM}, 'sessions': self.sessions}
        if arguments[0] == 'show':
            return {'session': next(s for s in self.sessions if s['session'] == arguments[1])}
        raise AssertionError(arguments)

    def fake_tj_item(self, verb, *arguments, cwd=None):
        if verb == 'mine':
            return []
        if verb == 'prs':
            return {'prs': []}
        return self.cards[arguments[0]]

    def fake_host_launch(self, host):
        return self.hosts[agent_cli.Host(host)]

    def fake_launch(self, host, directory, title, arguments, item=None, parent=None, want_session=False):
        self.launched.append({'host': str(host), 'directory': str(directory), 'title': title,
                              'arguments': arguments, 'item': item, 'parent': parent})
        return {'launch': {'launch_id': f'L{len(self.launched)}'}}

    def main(self, module, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), mock.patch.object(lifecycle.Path, 'cwd', return_value=Path(self.worktree)):
            code = module.main(list(argv))
        return code, out.getvalue()


class StatusTests(Fixture):
    def test_linked_cross_repository_prs_are_distinct_from_branch_pr(self):
        prs = [{'url': f'https://github.com/tj-agents/{repo}/pull/{number}',
                'state': 'open', 'head': 'feature', 'title': repo}
               for repo, number in [('cli', 1), ('agent', 2)]]
        branch_pr = {'number': 19, 'state': 'CLOSED', 'ci': None, 'url': 'https://example.com/19'}
        with mock.patch.object(lifecycle, 'tj_item', return_value={'prs': prs}), \
                mock.patch.object(lifecycle, 'branch', return_value='branch'), \
                mock.patch.object(lifecycle, 'pull_request', return_value=branch_pr):
            [row] = tj_status.report([lifecycle.Item(ITEM, None)])
        self.assertEqual(branch_pr, row['pr'])
        self.assertEqual(['tj-agents/cli', 'tj-agents/agent'], [pr['repository'] for pr in row['linked_prs']])
        text = tj_status.render_items([row])
        self.assertIn('branch pr #19 closed', text)
        self.assertIn('linked pr tj-agents/cli open', text)
        self.assertIn('linked pr tj-agents/agent open', text)

    def test_linked_pr_failure_is_distinct_from_empty(self):
        with mock.patch.object(lifecycle, 'tj_item', side_effect=lifecycle.LifecycleError('offline')):
            [row] = tj_status.report([lifecycle.Item(ITEM, None)])
        self.assertIsNone(row['linked_prs'])
        self.assertEqual('offline', row['linked_prs_error'])
        self.assertIn('linked prs unavailable: offline', tj_status.render_items([row]))
        with mock.patch.object(lifecycle, 'tj_item', return_value={'prs': []}):
            [row] = tj_status.report([lifecycle.Item(ITEM, None)])
        self.assertEqual([], row['linked_prs'])
        self.assertIsNone(row['linked_prs_error'])
        self.assertIn('linked prs none', tj_status.render_items([row]))
        with mock.patch.object(lifecycle, 'tj_item', return_value={'prs': {}}):
            [row] = tj_status.report([lifecycle.Item(ITEM, None)])
        self.assertIsNone(row['linked_prs'])
        self.assertIn('invalid linked pull request list', row['linked_prs_error'])

    def test_azure_devops_linked_pr_is_retained_and_displayed(self):
        url = 'https://dev.azure.com/org/project/_git/repo/pullrequest/42'
        with mock.patch.object(lifecycle, 'tj_item', return_value={'prs': [{'url': url, 'state': 'open'}]}):
            [row] = tj_status.report([lifecycle.Item(ITEM, None)])
        self.assertEqual(url, row['linked_prs'][0]['url'])
        self.assertIsNone(row['linked_prs'][0]['repository'])
        self.assertIsNone(row['linked_prs_error'])
        self.assertIn(f'linked pr open  {url}', tj_status.render_items([row]))

    def test_invalid_linked_pr_url_type_reports_unavailable(self):
        with mock.patch.object(lifecycle, 'tj_item', return_value={'prs': [{'url': 123, 'state': 'open'}]}):
            [row] = tj_status.report([lifecycle.Item(ITEM, None)])
        self.assertIsNone(row['linked_prs'])
        self.assertIn('linked prs unavailable', tj_status.render_items([row]))

    def test_item_and_report_lookups_overlap_and_preserve_order(self):
        refs = ['first', 'second']
        barrier = threading.Barrier(2)
        def store(*arguments):
            ref = arguments[-1]
            barrier.wait(timeout=5)
            return {'item': {'ref': ref}, 'sessions': []}
        with mock.patch.object(lifecycle, 'liveness', return_value={}), \
                mock.patch.object(agent_store, 'run', side_effect=store), \
                mock.patch.object(lifecycle, 'tj_item', return_value={'state': 'Open'}):
            items = lifecycle.open_items(refs)
        self.assertEqual(refs, [item.ref for item in items])
        barrier = threading.Barrier(2)
        def linked(_verb, ref, cwd=None):
            barrier.wait(timeout=5)
            return {'prs': [{'url': f'https://github.com/tj-agents/{ref}/pull/1', 'state': 'open'}]}
        with mock.patch.object(lifecycle, 'tj_item', side_effect=linked):
            rows = tj_status.report(items)
        self.assertEqual(refs, [row['item'] for row in rows])
        self.assertEqual(['tj-agents/first', 'tj-agents/second'],
                         [row['linked_prs'][0]['repository'] for row in rows])

    def test_item_store_failure_remains_fatal(self):
        with mock.patch.object(lifecycle, 'liveness', return_value={}), \
                mock.patch.object(agent_store, 'run', side_effect=agent_store.AgentStoreError('error', 'store failed')), \
                self.assertRaises(agent_store.AgentStoreError):
            lifecycle.open_items(['first'])

    def test_status_lists_open_items_with_each_agent_from_host_liveness(self):
        with mock.patch.object(lifecycle, 'branch', return_value='Feature/98-AgentStoreCutover'), \
                mock.patch.object(lifecycle, 'pull_request', return_value=None):
            code, out = self.main(tj_status, '--json')
        self.assertEqual(0, code)
        [row] = json.loads(out)['items']
        self.assertEqual((ITEM, 'In Progress', self.worktree), (row['item'], row['card'], row['worktree']))
        self.assertEqual(['busy', 'dead', 'dead', 'ended'], [agent['status'] for agent in row['agents']])

    def test_items_the_tracker_marks_done_are_not_open(self):
        self.cards[ITEM]['state'] = 'Done'
        self.assertEqual([], lifecycle.open_items(cwd=Path(self.worktree)))

    def test_ci_rolls_up_failing_before_pending_before_passing(self):
        self.assertEqual('failing', lifecycle.checks([{'conclusion': 'SUCCESS'}, {'conclusion': 'FAILURE'}]))
        self.assertEqual('pending', lifecycle.checks([{'conclusion': 'SUCCESS'}, {'status': 'IN_PROGRESS'}]))
        self.assertEqual('passing', lifecycle.checks([{'conclusion': 'SUCCESS'}, {'conclusion': 'SKIPPED'}]))
        self.assertIsNone(lifecycle.checks([]))


@dataclass
class StatusBackend:
    sessions: list[dict[str, Any]] = field(default_factory=list)
    launches: list[dict[str, Any]] = field(default_factory=list)
    receipts: dict[str, str | Exception] = field(default_factory=dict)
    commands: list[list[str]] = field(default_factory=list)
    item_calls: list[tuple[str, ...]] = field(default_factory=list)
    live: dict[str, Liveness] = field(default_factory=dict)
    tracker_error: str | None = None
    host_error: str | None = None
    store_error: str | None = None
    card_state: str = "In Progress"

    def add(self, host: str = "codex", native: str = "first", item: str | None = None) -> dict[str, Any]:
        handle = f"{host}:{native}"
        launch_id = f"launch-{native}"
        self.sessions.append({
            "session": handle, "host": host, "native_id": native,
            "launch_id": launch_id, "title": "Stale session title", "state": "active",
            "links": [{"item": {"ref": item}}] if item else [],
        })
        self.launches.append({
            "launch_id": launch_id, "session": handle, "host": host,
            "title": f"Session {native}", "parent": None,
        })
        self.live[native] = Liveness(True, "idle")
        document = {
            "schema": 1, "identity": {"launch_id": launch_id, "session_handle": handle},
            "revision": 1, "hold": False,
            "state": {"kind": "active", "turn": "turn-1", "intent": None},
            "withdrawals": [], "archive": [],
            "applied": [{"id": "input-1", "operation": "new_input", "facts": "turn-1"}],
        }
        self.receipts[launch_id] = json.dumps(document)
        return document

    def store(self, *arguments: str) -> dict[str, Any]:
        if self.store_error == arguments[0]:
            raise agent_store.AgentStoreError("fixture", "store unavailable")
        match arguments:
            case ("sessions",):
                return {"sessions": self.sessions}
            case ("sessions", "--item", ref):
                canonical = ITEM if ref == "98" else ref
                sessions = []
                for row in self.sessions:
                    if any(link["item"]["ref"] == canonical for link in row["links"]):
                        sessions.append(row)
                return {"item": {"ref": canonical}, "sessions": sessions}
            case ("launches",):
                return {"launches": self.launches}
            case _:
                raise AssertionError(arguments)

    def command(self, arguments: list[str], cwd: Path | None = None) -> str:
        assert arguments[:3] == ["tj", "receipt", "--launch"]
        assert len(arguments) == 4
        self.commands.append(arguments)
        response = self.receipts[arguments[-1]]
        if isinstance(response, Exception):
            raise response
        return response

    def items(self, verb: str, *arguments: str, cwd: Path | None = None) -> object:
        self.item_calls.append((verb, *arguments))
        if self.tracker_error:
            raise lifecycle.LifecycleError(self.tracker_error)
        match verb:
            case "resolve":
                return {"ref": arguments[0], "title": "Tracked work", "state": self.card_state}
            case "prs":
                return {"prs": []}
            case _:
                raise AssertionError(verb)

    def liveness(self, *, strict: bool = False) -> dict[str, Liveness]:
        assert strict
        if self.host_error:
            raise agent_cli.LaunchError(self.host_error)
        return self.live


@pytest.fixture
def status_backend(monkeypatch: pytest.MonkeyPatch) -> StatusBackend:
    backend = StatusBackend()
    monkeypatch.setattr(agent_store, "run", backend.store)
    monkeypatch.setattr(lifecycle, "command", backend.command)
    monkeypatch.setattr(lifecycle, "tj_item", backend.items)
    monkeypatch.setattr(lifecycle, "branch", lambda _worktree: None)
    monkeypatch.setattr(lifecycle, "pull_request", lambda *_arguments: None)
    monkeypatch.setattr(agent_cli, "host_launch", lambda _host: SimpleNamespace(liveness=backend.liveness))
    return backend


def outcome() -> dict[str, Any]:
    return {
        "turn": "turn-1", "report": "Changes delivered", "receipt": {"event": "end-1"},
        "outbox": {"delivery": {"kind": "recorded", "receipt": {"id": "parent-stored"}},
                   "tracker": {"kind": "pending"}, "cleanup": {"kind": "pending"}},
    }


def test_status_includes_independent_sessions_without_work_items(
    status_backend: StatusBackend, capsys: pytest.CaptureFixture[str],
) -> None:
    receipt = status_backend.add()
    status_backend.sessions[0]["parent"] = "claude:stale-parent"
    assert tj_status.main(["--json"]) == 0
    document = json.loads(capsys.readouterr().out)
    [session] = document["sessions"]
    assert document["items"] == []
    assert session["session"] == "codex:first"
    assert session["title"] == "Session first"
    assert session["relationship"] == "independent"
    assert session["parent"] is None
    assert session["liveness"] == "idle"
    assert session["managed"]["state"] == "active"
    assert session["managed"]["receipt"] == receipt
    assert status_backend.commands == [["tj", "receipt", "--launch", "launch-first"]]
    assert tj_status.main([]) == 0
    text = capsys.readouterr().out
    assert "Session first  codex:first" in text
    assert "host codex: idle; lifecycle: active" in text
    assert "launch launch-first; independent" in text


def test_status_shows_held_result_and_exact_pending_steps(
    status_backend: StatusBackend, tmp_path: Path,
) -> None:
    document = status_backend.add(host="claude")
    document["hold"] = True
    document["state"] = {"kind": "ready", "outcome": outcome()}
    status_backend.receipts["launch-first"] = json.dumps(document)
    status_backend.launches[0]["parent"] = "codex:parent"
    result = tj_status.snapshot(None, tmp_path)
    [session] = result.sessions
    assert session.managed.receipt == document
    assert session.relationship == "parented"
    text = tj_status.render(result)
    for expected in ("lifecycle: ready; hold: active", "parent codex:parent",
                     "Final result recorded", "delivery: recorded", "tracker: pending", "cleanup: pending", "tj hold --release"):
        assert expected in text
    assert "completed" not in text


@pytest.mark.parametrize("reason", ["user", "child", "ci", "approval"])
def test_status_shows_wait_reason_without_inferred_completion(
    status_backend: StatusBackend, tmp_path: Path, reason: str,
) -> None:
    document = status_backend.add()
    document["state"] = {"kind": "waiting", "turn": "turn-1",
                         "reason": {"kind": reason, "description": "External answer required"},
                         "report": "Waiting for an answer", "finish_requested": True}
    status_backend.receipts["launch-first"] = json.dumps(document)
    text = tj_status.render(tj_status.snapshot(None, tmp_path))
    assert f"Waiting for {reason}: External answer required" in text
    assert "lifecycle: waiting" in text
    assert "pending work prevents closeout" in text
    assert "completed" not in text


@pytest.mark.parametrize("response", [
    lifecycle.LifecycleError("no native turn recorded"),
    lifecycle.LifecycleError("tj receipt is unavailable"),
    "{", "null", "[]", '{"schema": 2}',
    '{"schema":1,"identity":{"launch_id":"foreign","session_handle":"codex:first"}}',
])
def test_status_keeps_missing_or_corrupt_receipts_unverified(
    status_backend: StatusBackend, tmp_path: Path, response: str | Exception,
) -> None:
    status_backend.add()
    status_backend.receipts["launch-first"] = response
    result = tj_status.snapshot(None, tmp_path)
    [session] = result.sessions
    assert session.managed.state == "unverified"
    assert session.managed.hold is None
    assert session.managed.receipt is None
    text = tj_status.render(result)
    assert "tj receipt --launch launch-first" in text
    assert "completed" not in text


def test_status_rejects_completion_without_closeout_receipts(
    status_backend: StatusBackend, tmp_path: Path,
) -> None:
    document = status_backend.add()
    document["state"] = {"kind": "completed", "outcome": outcome()}
    status_backend.receipts["launch-first"] = json.dumps(document)
    [session] = tj_status.snapshot(None, tmp_path).sessions
    assert session.managed.state == "unverified"
    assert "without all closeout receipts" in session.managed.error


@pytest.mark.parametrize("step", ["delivery", "tracker", "cleanup"])
def test_status_shows_lifecycle_failures_with_the_captured_result(
    status_backend: StatusBackend, tmp_path: Path, step: str,
) -> None:
    document = status_backend.add()
    result = outcome()
    result["outbox"][step] = {"kind": "failed", "failure": {"step": step, "description": "Operation denied"}}
    document["state"] = {"kind": "attention", "reason": "Closeout failed", "report": None, "outcome": result}
    status_backend.receipts["launch-first"] = json.dumps(document)
    text = tj_status.render(tj_status.snapshot(None, tmp_path))
    assert "lifecycle: attention" in text
    assert "Needs attention: Closeout failed" in text
    assert f"{step}: failed — Operation denied" in text
    assert "Final result recorded" in text


def test_status_retains_sessions_and_item_links_when_tracker_fails(
    status_backend: StatusBackend, capsys: pytest.CaptureFixture[str],
) -> None:
    status_backend.add(item=ITEM)
    status_backend.add(native="untracked")
    status_backend.tracker_error = "Tracker offline"
    assert tj_status.main(["--json"]) == 1
    document = json.loads(capsys.readouterr().out)
    assert [row["session"] for row in document["sessions"]] == ["codex:first", "codex:untracked"]
    assert document["items"][0]["item"] == ITEM
    assert document["items"][0]["linked_prs_error"] == "Tracker offline"
    assert any("Tracker offline" in error for error in document["errors"])


def test_status_filters_items_and_sessions_using_canonical_item_reference(
    status_backend: StatusBackend, capsys: pytest.CaptureFixture[str],
) -> None:
    status_backend.add(item=ITEM)
    status_backend.add(native="untracked")
    assert tj_status.main(["98", ITEM, "--json"]) == 0
    document = json.loads(capsys.readouterr().out)
    assert [row["session"] for row in document["sessions"]] == ["codex:first"]
    assert [row["item"] for row in document["items"]] == [ITEM]
    assert len(status_backend.commands) == 1


def test_status_does_not_hide_or_complete_sessions_on_done_cards(
    status_backend: StatusBackend, tmp_path: Path,
) -> None:
    status_backend.add(item=ITEM)
    status_backend.card_state = "Done"
    result = tj_status.snapshot(None, tmp_path)
    assert result.items == []
    [session] = result.sessions
    assert session.managed.state == "active"


def test_status_queries_current_launch_without_using_historical_receipt(
    status_backend: StatusBackend, tmp_path: Path,
) -> None:
    status_backend.add()
    status_backend.launches.append({**status_backend.launches[0], "launch_id": "older"})
    tj_status.snapshot(None, tmp_path)
    assert status_backend.commands == [["tj", "receipt", "--launch", "launch-first"]]


def test_status_reports_host_failure_without_calling_session_dead(
    status_backend: StatusBackend, tmp_path: Path,
) -> None:
    status_backend.add(item=ITEM)
    status_backend.host_error = "Provider unavailable"
    result = tj_status.snapshot(None, tmp_path)
    [session] = result.sessions
    assert session.liveness == "unverified"
    assert session.managed.state == "active"
    assert result.items[0]["agents"][0]["status"] == "unverified"
    assert "Provider unavailable" in tj_status.render(result)
    assert "Host is not running" not in tj_status.render(result)


@pytest.mark.parametrize("missing", ["launches", "binding"])
def test_status_keeps_sessions_when_launch_binding_is_unavailable(
    status_backend: StatusBackend, tmp_path: Path, missing: str,
) -> None:
    status_backend.add()
    if missing == "launches":
        status_backend.store_error = missing
    else:
        status_backend.launches[0]["session"] = "codex:another"
    [session] = tj_status.snapshot(None, tmp_path).sessions
    assert session.managed.state == "unverified"
    assert session.relationship == "unverified"
    assert status_backend.commands == []


def test_status_displays_completed_only_from_a_complete_current_receipt(
    status_backend: StatusBackend, tmp_path: Path,
) -> None:
    document = status_backend.add()
    result = outcome()
    for name in ("delivery", "tracker", "cleanup"):
        result["outbox"][name] = {"kind": "recorded", "receipt": {"id": f"{name}-receipt"}}
    document["state"] = {"kind": "completed", "outcome": result}
    status_backend.receipts["launch-first"] = json.dumps(document)
    status_backend.live.clear()
    status = tj_status.snapshot(None, tmp_path)
    [session] = status.sessions
    assert session.managed.state == "completed"
    assert session.liveness == "dead"
    assert session.issues == ()
    assert "lifecycle: completed" in tj_status.render(status)


@pytest.mark.parametrize("state", [
    {"kind": "ready"},
    {"kind": "waiting", "turn": "turn-1", "reason": []},
    {"kind": "unknown"},
])
def test_status_rejects_malformed_lifecycle_payload(
    status_backend: StatusBackend, tmp_path: Path, state: dict[str, Any],
) -> None:
    document = status_backend.add()
    document["state"] = state
    status_backend.receipts["launch-first"] = json.dumps(document)
    [session] = tj_status.snapshot(None, tmp_path).sessions
    assert session.managed.state == "unverified"
    assert session.managed.error is not None


def test_status_reports_unmanaged_sessions_without_borrowing_a_launch(
    status_backend: StatusBackend, tmp_path: Path,
) -> None:
    status_backend.add()
    status_backend.sessions[0].pop("launch_id")
    [session] = tj_status.snapshot(None, tmp_path).sessions
    assert session.launch_id is None
    assert session.relationship == "unverified"
    assert session.managed.state == "unverified"
    assert status_backend.commands == []


def test_status_rejects_retired_feed_outbox_field(
    status_backend: StatusBackend, tmp_path: Path,
) -> None:
    document = status_backend.add()
    result = outcome()
    result["outbox"]["feed"] = result["outbox"].pop("delivery")
    document["state"] = {"kind": "ready", "outcome": result}
    status_backend.receipts["launch-first"] = json.dumps(document)
    [session] = tj_status.snapshot(None, tmp_path).sessions
    assert session.managed.state == "unverified"
    assert session.managed.receipt is None


@pytest.mark.parametrize("has_session", [False, True])
def test_status_default_needs_no_tracker_without_linked_work(
    status_backend: StatusBackend, capsys: pytest.CaptureFixture[str], has_session: bool,
) -> None:
    if has_session:
        status_backend.add()
    status_backend.tracker_error = "No tracker configured"
    assert tj_status.main(["--json"]) == 0
    document = json.loads(capsys.readouterr().out)
    assert len(document["sessions"]) == int(has_session)
    assert document["items"] == []
    assert document["errors"] == []
    assert status_backend.item_calls == []
    assert tj_status.main([]) == 0
    text = capsys.readouterr().out
    assert "No open linked or explicitly requested work items" in text
    assert "tj item mine" in text
    assert "No open work items." not in text
    assert status_backend.item_calls == []


@pytest.mark.parametrize("explicit", [False, True])
@pytest.mark.parametrize("response", [
    lifecycle.LifecycleError("Authentication required"),
    lifecycle.LifecycleError("Profile configuration is malformed"),
    [],
])
def test_status_retains_linked_and_requested_tracker_failures(
    status_backend: StatusBackend, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    response: object, explicit: bool,
) -> None:
    status_backend.add(item=None if explicit else ITEM)

    def lookup(verb: str, *arguments: str, cwd: Path | None = None) -> object:
        assert arguments == (ITEM,)
        if verb == "prs":
            return {"prs": []}
        assert verb == "resolve"
        if isinstance(response, Exception):
            raise response
        return response

    monkeypatch.setattr(lifecycle, "tj_item", lookup)
    result = tj_status.snapshot(["98"] if explicit else None, tmp_path)
    assert len(result.sessions) == int(not explicit)
    assert result.items[0]["item"] == ITEM
    assert result.errors
    expected = str(response) if isinstance(response, Exception) else "invalid work item card"
    assert expected in result.errors[0]
    assert expected in tj_status.render(result)


def test_status_resolves_explicit_item_without_linked_sessions(
    status_backend: StatusBackend, capsys: pytest.CaptureFixture[str],
) -> None:
    assert tj_status.main(["98", "--json"]) == 0
    document = json.loads(capsys.readouterr().out)
    assert document["sessions"] == []
    assert document["errors"] == []
    assert document["items"][0]["item"] == ITEM
    assert status_backend.item_calls == [("resolve", ITEM), ("prs", ITEM)]
