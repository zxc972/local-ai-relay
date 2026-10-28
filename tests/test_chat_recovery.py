"""Recovery with real SQLite/HTTP/processes and simulated hosts, never real Agents."""
import collections
import contextlib
import itertools
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import chat_recovery
from chatroom import ChatRoom
from chat_remote import RemoteChat, request
from chat_server import create_server
from chat_session import session_owner


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="relay-recovery-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.parents, self.threads = [], []
        self.stopped = threading.Event()
        self.server = None
        self.addCleanup(patch.stopall)
        self.addCleanup(self.cleanup)

    def eventually(self, check, timeout=8):
        deadline = time.monotonic() + timeout
        while not check():
            if time.monotonic() > deadline:
                self.fail("Expected recovery state did not arrive")
            time.sleep(.025)

    def setup_room(self, network, mode="callback", real=False):
        self.network = network
        self.session = "simulated-worker-session"
        self.owner = session_owner(self.session)
        self.log = self.root / "notices.jsonl"
        handler = self.root / "fake_host.py"
        handler.write_text("import sys,json\nevent=json.load(sys.stdin)\n"
                           "with open(sys.argv[1], 'a') as f: f.write(json.dumps(event)+'\\n')\n")
        self.config = {"type": "command", "argv": [sys.executable, str(handler), str(self.log)], "timeout": 2}
        kwargs = {"client": "background-task"} if mode == "host-task" else {"wake_config": self.config}
        original = subprocess.Popen
        def capture(*args, **kw):
            process = original(*args, **kw)
            self.parents.append(process)
            return process
        if network:
            self.server = create_server(self.root / "server", port=0)
            self.server_thread = threading.Thread(target=self.server.serve_forever, daemon=True)
            self.server_thread.start()
            url = self.server.service.public_url
            invite = request(url, "/api/rooms", self.server.service.admin_token, {"action": "create", "room": "work"})["invite"]
            self.chat = self.server.service.room("work")
            self.writer = RemoteChat(self.root / "clients", url, "work", "writer", session_id="writer-session")
            self.writer.join(invite, client="manual", protocol_version=2)
            self.reader = RemoteChat(self.root / "clients", url, "work", "reader", session_id=self.session)
            with contextlib.ExitStack() as stack:
                stack.enter_context(patch("subprocess.Popen", side_effect=capture)) if real else stack.enter_context(patch.object(self.reader, "start"))
                self.reader.join(invite, protocol_version=2, **kwargs)
            self.store = self.reader.store
        else:
            self.chat = ChatRoom(self.root / "local", "work")
            self.chat.create()
            self.chat.join("writer", protocol_version=2)
            self.bg = self.chat.background
            with contextlib.ExitStack() as stack:
                stack.enter_context(patch("subprocess.Popen", side_effect=capture)) if real else stack.enter_context(patch.object(self.bg, "start"))
                self.bg.join("reader", session_id=self.session, protocol_version=2, env={}, **kwargs)
            self.store = self.chat.store

    def cleanup(self):
        self.stopped.set()
        if hasattr(self, "chat"):
            if self.network:
                self.reader.leave()
                self.writer.leave()
            else:
                self.chat.leave("reader", self.owner)
                self.chat.leave("writer")
        for thread in self.threads:
            thread.join(5)
            self.assertFalse(thread.is_alive())
        for parent in self.parents:
            try:
                parent.wait(timeout=8)
            except subprocess.TimeoutExpired:
                parent.terminate()
                parent.wait(timeout=5)
                self.fail("Test listener parent did not stop on leave")
        if self.server:
            self.server.service.stopping.set()
            self.server.service.changed.set()
            self.server.shutdown()
            self.server.server_close()
            self.server_thread.join(5)

    def row(self):
        if self.network:
            return self.reader.row()
        with contextlib.closing(self.store.connect()) as db:
            return dict(db.execute("SELECT * FROM chat_listeners WHERE name='reader'").fetchone())

    def send(self, text):
        return self.writer.send(text, to="reader") if self.network else self.chat.send("writer", text, to="reader")

    def receive(self):
        return self.reader.receive() if self.network else self.chat.receive("reader", self.owner)

    def ack(self, packet, message_id):
        args = {"delivery_id": packet["delivery_id"], "message_ids": [message_id]}
        return self.reader.ack(**args) if self.network else self.chat.ack("reader", owner=self.owner, **args)

    def start_thread(self, deliver):
        # Only shorten the transport poll in this test process, not the production timeout policy.
        patch("chat_remote.PEEK_WAIT", 0).start()
        patch("adapters.deliver", side_effect=deliver).start()
        if self.network:
            generation = self.reader.reserve("callback", expected_adapter=json.dumps(self.config))
            target, args = self.reader.run, (generation,)
        else:
            generation = self.bg.reserve("reader", self.owner, "callback", expected_adapter=json.dumps(self.config))
            target, args = self.bg.run, ("reader", generation)
        thread = threading.Thread(target=target, args=args, kwargs={"stopped": self.stopped})
        self.threads.append(thread)
        thread.start()

    def recovery_row(self):
        with contextlib.closing(self.store.connect()) as db:
            row = db.execute("SELECT * FROM chat_recovery WHERE scope='reader'").fetchone()
            return dict(row) if row else None

    def due(self):
        self.eventually(lambda: self.recovery_row() is not None)
        with self.store.transaction() as db:
            db.execute("UPDATE chat_recovery SET next_at=0 WHERE scope='reader'")

    def busy_then_missing_ack(self, network, uncertain=False):
        self.setup_room(network)
        effects = collections.Counter()
        busy, started, recovered, finished = threading.Event(), threading.Event(), threading.Event(), threading.Event()
        busy.set()
        notices, packets = [], {}
        errors = []
        def execute_once(packet):
            message = packet["messages"][0]
            effects[message["content"]] += 1
            # An actual external effect in the temp directory, distinct from the relay receipt.
            with (self.root / "effects.txt").open("a") as f:
                f.write(message["content"] + "\n")
            return message
        def deliver(config, event):
            try:
                notices.append(event)
                if event["event"] == "receipt_recovery":
                    self.assertNotIn("content", event)
                    self.assertEqual(event["receive_tool"]["arguments"]["session_id"], self.session)
                    if busy.is_set():
                        return {"accepted": False, "status": "busy", "retry_safe": True}
                    current = self.receive()
                    self.assertEqual(current["status"], "needs_review")
                    self.assertEqual(current["delivery_id"], packets["A"]["delivery_id"])
                    self.assertEqual((self.root / "effects.txt").read_text().splitlines(), ["A"])
                    self.ack(current, packets["A"]["messages"][0]["id"])
                    recovered.set()
                else:
                    packet = self.receive()
                    message = execute_once(packet)
                    packets[message["content"]] = packet
                    if message["content"] == "A":
                        started.set()
                        if uncertain:
                            return {"accepted": False, "status": "uncertain", "retry_safe": False}
                    else:
                        self.assertTrue(recovered.is_set())
                        self.ack(packet, message["id"])
                        finished.set()
                return {"accepted": True}
            except Exception as error:
                errors.append(error)
                return {"accepted": False, "status": "uncertain", "error": str(error)}
        self.start_thread(deliver)
        time.sleep(.15)
        self.assertEqual(notices, [], "Idle recovery must not call a model/host")
        self.send("A")
        self.assertTrue(started.wait(5))
        self.send("B")
        self.due()
        self.eventually(lambda: any(n["event"] == "receipt_recovery" for n in notices))
        self.eventually(lambda: self.recovery_row()["attempts"] == 0)
        self.assertEqual(effects, {"A": 1})
        self.assertFalse(finished.is_set())
        busy.clear()
        self.due()
        self.assertTrue(finished.wait(8), str(errors))
        self.assertEqual(errors, [])
        self.assertEqual(effects, {"A": 1, "B": 1})
        self.assertEqual((self.root / "effects.txt").read_text().splitlines(), ["A", "B"])
        self.assertEqual(sum(n["event"] == "message_available" for n in notices), 2)
        self.assertEqual(self.receive()["messages"], [])
        count = len(notices)
        time.sleep(.3)
        self.assertEqual(len(notices), count)

    def test_local_busy_missing_ack_then_next_task_exactly_once(self):
        self.busy_then_missing_ack(False)

    def test_http_busy_missing_ack_then_next_task_exactly_once(self):
        self.busy_then_missing_ack(True)

    def test_local_uncertain_notification_after_effect_does_not_reexecute(self):
        self.busy_then_missing_ack(False, uncertain=True)

    def test_http_uncertain_notification_after_effect_does_not_reexecute(self):
        self.busy_then_missing_ack(True, uncertain=True)

    def accepted_but_unreceived(self, network):
        self.setup_room(network)
        notices, effects = [], []
        done = threading.Event()
        def deliver(config, event):
            notices.append(event)
            if event["event"] == "receipt_recovery":
                packet = self.receive()
                effects.extend(m["id"] for m in packet["messages"])
                self.ack(packet, packet["messages"][0]["id"])
                done.set()
            return {"accepted": True}
        self.start_thread(deliver)
        sent = self.send("A")
        self.eventually(lambda: len(notices) == 1)
        self.due()
        self.assertTrue(done.wait(5))
        self.assertEqual(effects, [sent["message_id"]])
        self.assertEqual([n["event"] for n in notices], ["message_available", "receipt_recovery"])

    def test_local_accepted_notification_but_no_receive_is_recovered(self):
        self.accepted_but_unreceived(False)

    def test_http_accepted_notification_but_no_receive_is_recovered(self):
        self.accepted_but_unreceived(True)

    def bounded_unknown_result(self, network):
        self.setup_room(network)
        reminders = []
        started = threading.Event()
        def deliver(config, event):
            if event["event"] == "receipt_recovery":
                reminders.append(event)
                self.assertEqual(self.receive()["status"], "needs_review")
                # No proof of completion: do not execute again and do not acknowledge.
            else:
                self.receive()
                started.set()
            return {"accepted": True}
        self.start_thread(deliver)
        sent = self.send("A")
        self.assertTrue(started.wait(5))
        self.send("B")
        for n in range(3):
            self.due()
            self.eventually(lambda: len(reminders) == n + 1)
        self.due()
        self.eventually(lambda: self.row()["state"] == "needs_attention")
        time.sleep(.3)
        self.assertEqual(len(reminders), 3)
        self.assertIn(sent["message_id"], self.receive()["pending_message_ids"])
        self.assertIn("上限", self.row()["error"])
        self.assertEqual(self.recovery_row()["attempts"], 3)
        def visible_attention():
            state = (request(self.reader.server, "/api/rooms/work/state", self.reader.row()["token"], {})
                     if self.network else self.chat.status())
            return next(m for m in state["members"] if m["name"] == "reader")["state"] == "needs_attention"
        self.eventually(visible_attention)

    def test_local_reminder_budget_cannot_ack_or_reexecute_unknown_work(self):
        self.bounded_unknown_result(False)

    def test_http_reminder_budget_cannot_ack_or_reexecute_unknown_work(self):
        self.bounded_unknown_result(True)

    def prearmed_watch(self, network):
        self.setup_room(network, mode="host-task")
        first = self.send("A")
        packet = self.receive()
        self.assertTrue(packet["background_task"]["before_work"])
        argv = self.reader.argv("watch") if network else [sys.executable, str(ROOT / "relay.py"),
            "--data-dir", str(self.chat.room.base), "room", "watch", "work", "--name", "reader", "--session-id", self.session]
        process = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.parents.append(process)
        self.eventually(lambda: self.row()["pid"] is not None)
        second = self.send("B")
        time.sleep(.2)
        self.assertIsNone(process.poll(), "The prearmed watcher must not interrupt A with B")
        self.ack(packet, first["message_id"])
        output, error = process.communicate(timeout=8)
        self.assertEqual(process.returncode, 0, error)
        event = json.loads(output)
        self.assertEqual(event["message_id"], second["message_id"])
        self.assertEqual(event["receive_tool"]["arguments"]["session_id"], self.session)
        received = self.receive()
        self.assertEqual([m["id"] for m in received["messages"]], [second["message_id"]])
        self.ack(received, second["message_id"])

    def test_local_host_task_prearmed_before_work_receives_next_without_rearming_after_ack(self):
        self.prearmed_watch(False)

    def test_http_host_task_prearmed_before_work_receives_next_without_rearming_after_ack(self):
        self.prearmed_watch(True)

    def restart_callback(self, network):
        self.setup_room(network, real=True)
        first = self.send("A")
        self.eventually(lambda: self.log.exists() and len(self.log.read_text().splitlines()) == 1)
        old = self.row()
        self.assertNotEqual(self.parents[0].pid, old["pid"])
        os.kill(old["pid"], getattr(signal, "SIGKILL", signal.SIGTERM))  # Only this fixture's child.
        self.eventually(lambda: self.row()["pid"] not in (None, old["pid"]) and
                        ChatRoom.process_alive(self.row()["pid"]) is not False)
        self.assertEqual(self.row()["generation"], old["generation"])
        self.assertEqual(len(self.log.read_text().splitlines()), 1, "Restart must not resend the accepted task")
        packet = self.receive()
        self.assertEqual(packet["messages"][0]["id"], first["message_id"])
        self.ack(packet, first["message_id"])
        second = self.send("B")
        self.eventually(lambda: len(self.log.read_text().splitlines()) == 2)
        notices = [json.loads(line) for line in self.log.read_text().splitlines()]
        self.assertEqual([n["message_id"] for n in notices], [first["message_id"], second["message_id"]])

    def test_local_callback_child_crash_restarts_without_replaying_task(self):
        self.restart_callback(False)

    def test_http_callback_child_crash_restarts_without_replaying_task(self):
        self.restart_callback(True)

    def test_reminder_budget_is_durable_and_resets_only_when_progress_changes(self):
        self.setup_room(False, mode="host-task")
        event = {"message_id": "known-message", "receive_tool": {"name": "chat_receive", "arguments": {"session_id": self.session}}}
        with self.store.transaction() as db:
            chat_recovery.select(db, "reader", "batch:old:1", event, now=0)
        for attempt, moment in enumerate((100, 1000, 3000), start=1):
            with self.store.transaction() as db:
                state, reminder = chat_recovery.select(db, "reader", "batch:old:1", event, now=moment)
                self.assertEqual(state, "recovering")
                self.assertEqual(reminder["recovery"]["attempt"], attempt)
                self.assertNotIn("content", reminder)
            # Reopening/migrating must not reset a reserved (possibly uncertain) notice.
            self.chat.background
        with self.store.transaction() as db:
            self.assertEqual(chat_recovery.select(db, "reader", "batch:old:1", event, now=10000), ("needs_attention", None))
            self.assertEqual(chat_recovery.select(db, "reader", "batch:new:1", event, now=10001), ("recovery_wait", None))

    def test_supervisor_bounds_repeated_child_start_failures(self):
        ticks = itertools.count(0, 2)
        exhausted = []
        with patch("chat_recovery.subprocess.Popen", side_effect=OSError("test-only start failure")) as launch, \
                patch("chat_recovery.time.monotonic", side_effect=lambda: next(ticks)), \
                patch("chat_recovery.time.sleep"):
            chat_recovery.supervise(["fake-worker"], lambda: True, lambda: exhausted.append(True))
        self.assertEqual(launch.call_count, 4)  # Initial child plus three recovery attempts.
        self.assertEqual(exhausted, [True])
