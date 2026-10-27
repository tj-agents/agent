import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugins/agent/.agents/agent/runtime"))
import event_watch as watch
import event_service
import install


def hint(identifier="event1", repository="tj-agents/agent"):
    return {"event": "message", "id": identifier, "message": json.dumps({"v": 1, "repo": repository})}


def current(repository="tj-agents/agent", revision="a"):
    return {"nameWithOwner": repository, "defaultBranchRef": {"target": {"oid": revision}},
            "marketplace": {"oid": "unchanged-catalog"}}


class StreamTests(unittest.TestCase):
    def test_stream_ignores_heartbeats_and_invalid_envelopes_and_keeps_a_bounded_cursor(self):
        envelope = hint()
        body = b"garbage\n[]\n" + b"\n".join(json.dumps(row).encode() for row in
                    ({"event": "keepalive"}, hint("../bad"), envelope))
        open_url = mock.Mock(return_value=io.BytesIO(body))
        self.assertEqual([envelope], list(watch.stream("t" * 40, "previous", open_url)))
        request = open_url.call_args.args[0]
        self.assertEqual("https://ntfy.sh/" + "t" * 40 + "/json?since=previous", request.full_url)
        self.assertEqual(70, open_url.call_args.kwargs["timeout"])
        for topic, cursor in (("../bad", "all"), ("t" * 40, "a&url=evil")):
            with self.assertRaises(ValueError):
                list(watch.stream(topic, cursor, open_url))

    def test_stream_rejects_an_oversized_message(self):
        with self.assertRaisesRegex(ValueError, "size limit"):
            list(watch.stream("t" * 40, "all", lambda *_, **__: io.BytesIO(b"a" * (watch.launch_check.MAX_RESPONSE + 1))))


class SubscriberFixture(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="agent subscriber ")
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.cursor = self.directory / "machine-events-watch-cursor.json"
        self.state = self.directory / "machine-events-state.json"
        self.write(self.directory / "machine-events.json", {"topic": "t" * 40})
        self.refresh = mock.Mock(return_value=0)
        self.factory = lambda state: watch.machine_events.Reconciler(state, refresh=self.refresh, policy=mock.Mock())

    def write(self, path, value):
        watch.plugins.write_json(path, value)

    def consume(self, rows=(), **kwargs):
        return watch.consume(self.directory, rows, now=lambda: 100, factory=self.factory, **kwargs)


class SubscriberTests(SubscriberFixture):
    def test_authoritative_revision_is_applied_once_and_code_only_publication_applies_again(self):
        api = mock.Mock(return_value={"data": {"repository": current()}})
        envelope = hint()
        envelope["message"] = json.dumps({"v": 1, "repo": "tj-agents/agent", "revision": "untrusted"})
        self.consume([envelope, hint("event2")], api=api)
        self.refresh.assert_called_once_with()
        self.assertEqual("a", watch.plugins.read_json(self.state)["tj-agents/agent"]["revision"])
        api.return_value = {"data": {"repository": current(revision="b")}}
        self.consume([hint("event3")], api=api)
        self.assertEqual(2, self.refresh.call_count)
        self.assertEqual({"since": "event3", "checked_at": 100}, watch.plugins.read_json(self.cursor))
        self.assertIn("name=agent", api.call_args.args[0])

    def test_failure_preserves_successful_revisions_and_does_not_acknowledge_batch(self):
        self.write(self.cursor, {"since": "old", "checked_at": 50})
        api = mock.Mock(side_effect=[{"data": {"repository": current()}}, RuntimeError("offline")])
        with self.assertRaisesRegex(RuntimeError, "offline"):
            self.consume([hint(), hint("event2", "tj-agents/core")], api=api)
        self.assertIn("tj-agents/agent", watch.plugins.read_json(self.state))
        self.assertEqual({"since": "old", "checked_at": 50}, watch.plugins.read_json(self.cursor))
        api = mock.Mock(return_value={"data": {"repository": current()}})
        self.consume([hint()], api=api)
        self.refresh.assert_called_once_with()

    def test_failed_update_is_not_saved_or_acknowledged_and_retries(self):
        api = mock.Mock(return_value={"data": {"repository": current()}})
        self.refresh.return_value = 1
        with self.assertRaisesRegex(RuntimeError, "refresh failed"):
            self.consume([hint()], api=api)
        self.assertEqual({}, watch.plugins.read_json(self.state))
        self.assertFalse(self.cursor.exists())
        self.refresh.return_value = 0
        self.consume([hint()], api=api)
        self.assertEqual(2, self.refresh.call_count)
        self.assertEqual("event1", watch.plugins.read_json(self.cursor)["since"])

    def test_invalid_hint_is_acknowledged_without_a_github_query_or_update(self):
        api = mock.Mock()
        self.consume([hint(repository="attacker/project")], api=api)
        api.assert_not_called()
        self.refresh.assert_not_called()
        self.assertEqual("event1", watch.plugins.read_json(self.cursor)["since"])

    def test_failed_catch_up_preserves_cursor_and_releases_lock(self):
        with mock.patch.object(watch.machine_events, "catch_up_states", return_value=[]):
            self.refresh.return_value = 1
            with self.assertRaisesRegex(RuntimeError, "catch-up failed"):
                self.consume(catch_up=True)
            self.assertFalse(self.cursor.exists())
            self.refresh.return_value = 0
            self.consume(catch_up=True)
        self.assertEqual("all", watch.plugins.read_json(self.cursor)["since"])

    def test_contention_retries_and_factory_failure_releases_the_reconciliation_lock(self):
        lock = watch.plugins.acquire_lock(self.directory / "machine-events.lock")
        try:
            with self.assertRaisesRegex(RuntimeError, "already running"):
                self.consume([hint()])
        finally:
            lock.close()
        with self.assertRaisesRegex(RuntimeError, "broken factory"):
            watch.consume(self.directory, factory=mock.Mock(side_effect=RuntimeError("broken factory")))
        self.consume([hint(repository="attacker/project")])

    def test_idle_reconnection_resumes_acknowledged_cursor_without_refresh(self):
        self.write(self.cursor, {"since": "old", "checked_at": 100})
        events = mock.Mock(return_value=iter(()))
        with mock.patch.object(watch, "consume") as consume:
            with self.assertRaises(KeyboardInterrupt):
                watch.run(self.directory, events=events, now=lambda: 101,
                          sleep=mock.Mock(side_effect=KeyboardInterrupt))
        events.assert_called_once_with("t" * 40, "old")
        consume.assert_not_called()

    def test_expired_cursor_performs_one_catch_up_before_streaming(self):
        with mock.patch.object(watch.machine_events, "catch_up_states", return_value=[]), \
                mock.patch.object(watch.machine_events, "Reconciler"):
            def catch_up(directory, **kwargs):
                self.assertTrue(kwargs["catch_up"])
                self.write(self.cursor, {"since": "old", "checked_at": 100})

            events = mock.Mock(return_value=iter(()))
            with mock.patch.object(watch, "consume", side_effect=catch_up) as consume:
                with self.assertRaises(KeyboardInterrupt):
                    watch.run(self.directory, events=events, now=lambda: 100,
                              sleep=mock.Mock(side_effect=KeyboardInterrupt))
        consume.assert_called_once()
        events.assert_called_once_with("t" * 40, "old")

    def test_runtime_replacement_requests_a_supervisor_restart(self):
        self.write(self.cursor, {"since": "old", "checked_at": 100})
        runtime = self.directory / "bin/source.json"
        runtime.parent.mkdir()
        runtime.write_text("old")
        with mock.patch.object(watch, "consume", side_effect=lambda *_, **__: runtime.write_text("new")):
            self.assertEqual(1, watch.run(self.directory, events=lambda *_: iter([hint()]), now=lambda: 101))

    def test_stream_failure_reconnects_from_the_last_committed_cursor(self):
        self.write(self.cursor, {"since": "old", "checked_at": 100})
        events = mock.Mock(side_effect=[OSError("connection lost"), KeyboardInterrupt])
        sleep = mock.Mock()
        with self.assertLogs(watch.machine_events.LOG, level="WARNING"), self.assertRaises(KeyboardInterrupt):
            watch.run(self.directory, events=events, now=lambda: 101, sleep=sleep)
        self.assertEqual([mock.call("t" * 40, "old")] * 2, events.call_args_list)
        sleep.assert_called_once_with(30)

    def test_watch_cli_is_a_singleton_and_uses_the_installed_profiles(self):
        with (
            mock.patch.dict(os.environ),
            mock.patch.object(watch.machine_events, "require_tommy") as identity,
            mock.patch.object(watch, "run", return_value=0) as run,
        ):
            lock = watch.plugins.acquire_lock(self.directory / "machine-events-watch.lock")
            arguments = ["--watch", "--state-directory", str(self.directory), "--claude-home",
                         str(self.directory / "claude"), "--codex-home", str(self.directory / "codex")]
            try:
                self.assertEqual(0, watch.machine_events.main(arguments))
                run.assert_not_called()
            finally:
                lock.close()
            self.assertEqual(0, watch.machine_events.main(arguments))
            run.assert_called_once_with(self.directory.resolve())
            identity.assert_called_once()
            self.assertEqual((self.directory / "codex").resolve(), Path(os.environ["CODEX_HOME"]))


class SubscriberBootstrapTests(SubscriberFixture):
    def setUp(self):
        super().setUp()
        self.launch = watch.launch_check
        patch = mock.patch.dict(os.environ, {"AGENT_STATE_DIRECTORY": str(self.directory)})
        patch.start()
        self.addCleanup(patch.stop)
        self.managed = self.directory / "bin/.agents/agent/runtime/launch_check.py"
        patch = mock.patch.object(self.launch, "__file__", str(self.managed))
        patch.start()
        self.addCleanup(patch.stop)
        self.service = self.directory / "machine-events-service.json"

    def test_managed_launch_bootstraps_once_after_a_successful_install_receipt(self):
        def repair(*_):
            self.write(self.service, event_service.receipt(self.directory,
                       self.launch.Host.CLAUDE.configuration_root, self.launch.Host.CODEX.configuration_root))

        with mock.patch.object(self.launch, "recover_install", side_effect=repair) as recover:
            self.launch.ensure_subscriber(self.directory)
            self.launch.ensure_subscriber(self.directory)
        recover.assert_called_once_with(self.directory, "background machine events")

    def test_setup_failure_is_bounded_and_does_not_fail_foreground_updates(self):
        with mock.patch.object(self.launch, "recover_install", side_effect=RuntimeError("no supervisor")) as recover, \
                mock.patch.object(sys, "stderr"), mock.patch.object(self.launch, "settle", return_value="current"):
            self.launch.ensure_subscriber(self.directory)
            self.launch.ensure_subscriber(self.directory)
            self.assertIn(self.launch.check(self.directory, fetch=lambda _: b"", update=lambda *_: True, now=100),
                          ("current", "updated"))
        recover.assert_called_once()
        self.assertIn("failed_at", watch.plugins.read_json(self.service))

    def test_invalid_backoff_does_not_prevent_repair_and_source_runtime_never_installs_a_service(self):
        self.write(self.service, {"failed_at": "invalid"})
        with mock.patch.object(self.launch, "recover_install") as recover:
            self.launch.ensure_subscriber(self.directory)
            recover.assert_called_once()
            recover.reset_mock()
            with mock.patch.object(self.launch, "__file__", str(ROOT / ".agents/agent/runtime/launch_check.py")):
                self.launch.ensure_subscriber(self.directory)
            recover.assert_not_called()

    def test_installer_records_supervision_only_after_successful_registration(self):
        context = mock.Mock(state_directory=self.directory, claude_home=self.directory / "claude",
                            codex_home=self.directory / "codex")
        with mock.patch.object(install.event_service, "apply", side_effect=RuntimeError("denied")):
            with self.assertRaises(RuntimeError):
                install.apply_background(context)
        self.assertFalse(self.service.exists())
        with mock.patch.object(install.event_service, "apply", return_value=["registered"]):
            install.apply_background(context)
        self.assertEqual(install.background_receipt(context), watch.plugins.read_json(self.service))
