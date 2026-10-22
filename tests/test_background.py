"""Real temporary inboxes and subprocess hooks, with no model/client calls."""
import contextlib
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from background import Background
from connection import Connection
from core import RelayError
from room_cli import Room


class BackgroundTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="relay background 中文 ")
        self.root = Path(self.temp.name)
        self.room = Room("test", self.root / "data")
        self.connection = Connection(self.room)
        self.connection.join("sender")
        self.connection.join("receiver")
        self.background = Background(self.room)
        self.threads = []
        self.hook = self.root / "record event.py"
        self.hook.write_text(
            "import json, pathlib, sys, time\n"
            "event = json.load(sys.stdin)\n"
            "with pathlib.Path(sys.argv[1]).open('a', encoding='utf-8') as stream:\n"
            "    stream.write(json.dumps(event) + '\\n')\n"
            "if len(sys.argv)>3: time.sleep(float(sys.argv[3]))\n"
            "sys.exit(int(sys.argv[2]) if len(sys.argv)>2 else 0)\n", encoding="utf-8")

    def tearDown(self):
        for role in ("sender", "receiver"):
            self.background.stop(role)
        for thread, stopped, errors in self.threads:
            stopped.set()
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive())
            self.assertFalse(errors, errors)
        self.wait(lambda: not any(self.background.status(r)["worker_active"] for r in ("sender", "receiver")))
        self.temp.cleanup()

    def wait(self, condition, timeout=5):
        deadline = time.monotonic() + timeout
        while not condition():
            if time.monotonic() >= deadline:
                self.fail("Timed out waiting for background state")
            time.sleep(.02)

    def stream(self, role):
        events, errors, stopped = [], [], threading.Event()

        def run():
            try:
                self.background.events(role, events.append, stopped)
            except BaseException as error:
                errors.append(error)

        thread = threading.Thread(target=run, daemon=True)
        self.threads.append((thread, stopped, errors))
        thread.start()
        self.wait(lambda: self.background.status(role)["running"] or bool(errors))
        self.assertFalse(errors, errors)
        return events, stopped, thread

    def state(self, message_id):
        with contextlib.closing(self.room.store.connect()) as db:
            return db.execute("SELECT state FROM channel_messages WHERE id=?", (message_id,)).fetchone()[0]

    def notices(self, role):
        return self.background.status(role)["notifications"]

    def recorded(self, path):
        if not path.exists():
            return []
        # The small append is one write, but a reader can observe the empty
        # newly-created file before the subprocess flushes its first line.
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]

    def test_stream_idle_no_events_receives_nothing_and_restart_does_not_repeat(self):
        events, stopped, thread = self.stream("receiver")
        time.sleep(.35)
        self.assertEqual(events, [])
        with self.assertRaises(RelayError):
            self.background.events("receiver", events.append, threading.Event())
        message = self.connection.send("sender", "external text must not be in wake metadata")
        self.wait(lambda: len(events) == 1)
        self.assertEqual(self.state(message["message_id"]), "queued")
        self.assertNotIn("content", events[0])
        self.assertEqual(events[0]["kind"], "command")
        self.assertEqual(events[0]["receive_tool"]["name"], "relay_receive")
        time.sleep(.35)
        self.assertEqual(len(events), 1)
        stopped.set()
        thread.join(timeout=3)
        resumed, _, _ = self.stream("receiver")
        time.sleep(.35)
        self.assertEqual(resumed, [])
        self.assertEqual(self.notices("receiver")[0]["attempt"], 1)

    def test_stream_both_roles_and_busy_inbox_blocks_later_notifications(self):
        receiver, _, _ = self.stream("receiver")
        sender, _, _ = self.stream("sender")
        first = self.connection.send("sender", "calculate")
        self.wait(lambda: len(receiver) == 1)
        received = self.connection.receive("receiver", timeout=0)
        second = self.connection.send("sender", "next command")
        time.sleep(.4)
        self.assertEqual(len(receiver), 1)
        self.assertEqual(self.background.status("receiver")["blocked_by"], first["message_id"])
        result = self.connection.send("receiver", "42", reply_to=received["message"]["id"])
        self.wait(lambda: len(sender) == 1 and len(receiver) == 2)
        self.assertEqual(sender[0]["message_id"], result["message_id"])
        self.assertEqual(sender[0]["kind"], "result")
        self.assertEqual(receiver[1]["message_id"], second["message_id"])
        self.assertEqual(self.state(result["message_id"]), "queued")

    def test_detached_hooks_for_both_roles_idle_and_stopped_are_silent(self):
        paths = {role: self.root / (role + ".jsonl") for role in ("sender", "receiver")}
        for role, path in paths.items():
            self.background.configure(role, [sys.executable, str(self.hook), str(path)])
            self.assertTrue(self.background.doctor(role)["ready"])
            started = self.background.start(role)
            self.assertTrue(started["running"], started)
            self.assertNotEqual(started["pid"], os.getpid())
            self.assertEqual(self.background.start(role)["pid"], started["pid"])
        time.sleep(.35)
        self.assertFalse(any(p.exists() for p in paths.values()))
        message = self.connection.send("sender", "task")
        self.wait(lambda: len(self.recorded(paths["receiver"])) == 1)
        self.assertEqual(self.state(message["message_id"]), "queued")
        command = self.connection.receive("receiver", timeout=0)["message"]
        result = self.connection.send("receiver", "done", reply_to=command["id"])
        self.wait(lambda: len(self.recorded(paths["sender"])) == 1)
        self.assertEqual(self.state(result["message_id"]), "queued")
        self.connection.receive("sender", timeout=0)
        self.connection.ack("sender", result["message_id"])
        self.background.stop("receiver")
        self.wait(lambda: not self.background.status("receiver")["worker_active"])
        queued = self.connection.send("sender", "remain queued")
        time.sleep(.4)
        self.assertEqual(len(self.recorded(paths["receiver"])), 1)
        self.assertEqual(self.state(queued["message_id"]), "queued")

    def test_failed_callback_requires_explicit_retry_even_after_restart(self):
        path = self.root / "failed.jsonl"
        self.background.configure("receiver", [sys.executable, str(self.hook), str(path), "4"])
        self.background.start("receiver")
        message = self.connection.send("sender", "task")["message_id"]
        self.wait(lambda: bool(self.background.status("receiver")["needs_attention"]))
        self.assertEqual(len(self.recorded(path)), 1)
        time.sleep(.35)
        self.assertEqual(len(self.recorded(path)), 1)
        self.background.stop("receiver")
        self.wait(lambda: not self.background.status("receiver")["worker_active"])
        self.background.configure("receiver", [sys.executable, str(self.hook), str(path)])
        self.background.start("receiver")
        time.sleep(.35)
        self.assertEqual(len(self.recorded(path)), 1)
        self.background.retry("receiver", message)
        self.wait(lambda: self.notices("receiver")[0]["state"] == "accepted")
        self.assertEqual(len(self.recorded(path)), 2)
        self.assertEqual(self.notices("receiver")[0]["attempt"], 2)
        self.assertEqual(self.state(message), "queued")

    def test_timeout_is_uncertain_and_never_repeated_automatically(self):
        path = self.root / "timed-out.jsonl"
        self.background.configure("receiver", [sys.executable, str(self.hook), str(path), "0", "2"], timeout=.15)
        self.background.start("receiver")
        message = self.connection.send("sender", "task")["message_id"]
        self.wait(lambda: bool(self.background.status("receiver")["needs_attention"]))
        self.assertEqual(len(self.recorded(path)), 1)
        time.sleep(.4)
        self.assertEqual(len(self.recorded(path)), 1)
        self.assertEqual(self.state(message), "queued")

    def test_crashed_attempt_never_redelivers_and_stop_prevents_new_worker_race(self):
        # Persist the state a killed worker would leave immediately before or
        # after host acceptance; takeover must regard the outcome as unknown.
        generation = self.background._reserve("receiver", "events")
        instance = self.background._enter("receiver", generation)
        message = self.connection.send("sender", "task")["message_id"]
        self.background._next("receiver", generation, instance)
        self.background.stop("receiver")
        with self.assertRaises(RelayError):
            self.background.events("receiver", lambda _: None)
        with self.room.store.transaction() as db:
            db.execute("UPDATE background_listeners SET lease_until=0 WHERE role='receiver'")
        events, _, _ = self.stream("receiver")
        time.sleep(.4)
        self.assertEqual(events, [])
        self.assertEqual(self.background.status("receiver")["needs_attention"][0]["message_id"], message)
        self.background.retry("receiver", message)
        self.wait(lambda: len(events) == 1)
        self.assertEqual(self.state(message), "queued")

    def test_unconfigured_start_fails_without_claim_or_new_task(self):
        with self.assertRaises(RelayError):
            self.background.start("receiver")
        self.assertFalse(self.background.doctor("receiver")["ready"])
        self.assertFalse(self.background.stop("receiver")["enabled"])
        with contextlib.closing(self.room.store.connect()) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM channel_messages").fetchone()[0], 0)

    def test_missing_hook_fails_preflight_without_daemon_or_consumption(self):
        self.background.configure("receiver", [str(self.root / "does-not-exist")])
        message = self.connection.send("sender", "preserve until host is configured")["message_id"]
        with patch("background.subprocess.Popen") as spawn:
            result = self.background.start("receiver")
        spawn.assert_not_called()
        self.assertEqual(result["status"], "needs_attention")
        self.assertFalse(result["running"])
        self.assertFalse(result["enabled"])
        self.assertIsNone(result["pid"])
        self.assertIn("does-not-exist", result["error"])
        self.assertEqual(self.state(message), "queued")
        self.assertEqual(self.notices("receiver"), [])
        self.assertEqual(self.background.status("receiver")["error"], result["error"])

    def test_failed_host_preflight_does_not_run_callback(self):
        path = self.root / "never-called.jsonl"
        self.background.configure("receiver", [sys.executable, str(self.hook), str(path)])
        with patch("adapters.doctor", return_value={"ready": False, "endpoint": {
                "ok": False, "detail": "Existing host endpoint is unavailable"}}), \
                patch("background.subprocess.Popen") as spawn:
            result = self.background.start("receiver")
        spawn.assert_not_called()
        self.assertFalse(path.exists())
        self.assertFalse(result["running"])
        self.assertEqual(result["error"], "Existing host endpoint is unavailable")

    def test_known_other_session_cannot_stop_reconfigure_or_retry(self):
        with self.room.store.transaction() as db:
            db.execute("UPDATE channel_peers SET session_id='owner' WHERE role='receiver'")
        with patch.dict(os.environ, {"AIRELAY_SESSION_ID": "owner"}):
            events, _, _ = self.stream("receiver")
        with patch.dict(os.environ, {"AIRELAY_SESSION_ID": "someone-else"}):
            for action in (
                lambda: self.background.stop("receiver"),
                lambda: self.background.configure("receiver", [sys.executable, str(self.hook)]),
                lambda: self.background.retry("receiver", "unknown"),
            ):
                with self.assertRaises(RelayError):
                    action()
        self.assertTrue(self.background.status("receiver")["running"])
        self.assertEqual(events, [])
        # Clear the fixture-only ownership before ordinary teardown.
        with self.room.store.transaction() as db:
            db.execute("UPDATE channel_peers SET session_id=NULL WHERE role='receiver'")

    def test_legacy_listener_must_be_explicitly_disabled_before_takeover(self):
        with self.room.store.transaction() as db:
            db.execute("INSERT INTO meta(key,value) VALUES(?,?)", ("listener:receiver", json.dumps({
                "enabled": True, "client": "claude", "status": "listening", "last_checked_at": time.time()})))
        with self.assertRaisesRegex(RelayError, "auto-off"):
            self.background.events("receiver", lambda _: None)
        self.assertFalse(self.background.status("receiver")["running"])
        with self.room.store.transaction() as db:
            db.execute("UPDATE meta SET value=? WHERE key='listener:receiver'", (json.dumps({"enabled": False}),))
        events, _, _ = self.stream("receiver")
        self.assertEqual(events, [])

    def test_closed_event_pipe_stops_and_preserves_uncertain_wake(self):
        message = self.connection.send("sender", "queued")["message_id"]

        def closed_pipe(event):
            raise BrokenPipeError("host pipe closed")

        status = self.background.events("receiver", closed_pipe)
        self.assertFalse(status["running"])
        self.assertFalse(status["worker_active"])
        self.assertEqual(status["needs_attention"][0]["message_id"], message)
        self.assertEqual(self.state(message), "queued")


if __name__ == "__main__":
    unittest.main()
