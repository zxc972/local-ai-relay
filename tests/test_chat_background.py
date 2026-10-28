"""Real detached workers and host-owned task exits; no AI/provider calls."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from chatroom import ChatRoom
from chatroom_mcp import ChatEndpoint
from core import RelayError
from chat_safety import protect_network
from test_message_bus import Peer


class ChatBackgroundTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="chat background 测试 ")
        self.addCleanup(self.temp.cleanup)
        self.data = Path(self.temp.name)
        self.chat = ChatRoom(self.data, "work")
        self.chat.create()
        self.chat.join("writer")
        self.log = self.data / "events.jsonl"
        self.handler = self.data / "host.py"
        self.handler.write_text(
            "import json, sys\n"
            "event = json.load(sys.stdin)\n"
            "with open(sys.argv[1], 'a', encoding='utf-8') as output:\n"
            "    output.write(json.dumps(event) + '\\n')\n"
            "sys.exit(int(sys.argv[2]))\n", encoding="utf-8")
        self.workers, self.processes = [], []
        self.addCleanup(self.stop_workers)

    def stop_workers(self):
        for member in self.chat.status()["members"]:
            self.chat.leave(member["name"], owner=None)
        for process in self.processes:
            process.communicate(timeout=5)
        self.eventually(lambda: all(not m["background"]["worker_active"] for m in self.chat.status()["members"]))
        for pid in self.workers:
            self.eventually(lambda: self.chat.process_alive(pid) is not True)

    def eventually(self, condition, timeout=5):
        deadline = time.monotonic() + timeout
        while not condition():
            if time.monotonic() >= deadline:
                self.fail("Expected background event/state did not arrive")
            time.sleep(.025)

    def config(self, exit_code=0):
        return {"type": "command", "argv": [sys.executable, str(self.handler), str(self.log), str(exit_code)],
                "timeout": 3}

    def start(self, name="reader", exit_code=0):
        result = self.chat.background.join(name, wake_config=self.config(exit_code), env={})
        self.assertTrue(result["background"]["running"], result)
        self.workers.append(result["background"]["pid"])
        return result

    def events(self):
        return [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()] if self.log.exists() else []

    def native_watch(self, name="claude"):
        process = subprocess.Popen([sys.executable, str(ROOT / "bin" / "airelay"), "--data-dir", str(self.data),
                                    "room", "watch", "work", "--name", name],
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8")
        self.processes.append(process)
        self.eventually(lambda: self.chat.background.status(name)["running"])
        return process

    def test_transport_idle_is_silent_then_one_wake_per_processed_message(self):
        joined = self.start()
        self.assertIsNone(joined["next_tool"])
        self.assertIsNone(joined["next_command"])
        time.sleep(.6)
        self.assertEqual(self.events(), [], "An idle watcher must never call a host/model")
        first = self.chat.send("writer", "第一条", to="reader")
        second = self.chat.send("writer", "第二条", to="reader")
        self.eventually(lambda: len(self.events()) == 1)
        self.assertEqual(self.events()[0]["message_id"], first["message_id"])
        self.assertNotIn("content", self.events()[0])
        time.sleep(.4)
        self.assertEqual(len(self.events()), 1, "Accepted but unreceived events must not wake repeatedly")
        received = self.chat.receive("reader")
        self.assertEqual([m["id"] for m in received["messages"]], [first["message_id"]])
        self.assertEqual(self.chat.receive("reader")["status"], "needs_review")
        reply = self.chat.send("reader", "实际结果", reply_to=first["message_id"], key="reply-one")
        self.assertTrue(reply["acknowledged"])
        self.eventually(lambda: len(self.events()) == 2)
        self.assertEqual(self.events()[1]["message_id"], second["message_id"])
        self.chat.receive("reader")
        self.chat.ack("reader", second["message_id"])
        self.chat.ack("reader", second["message_id"])  # Idempotent receipt.
        time.sleep(.4)
        self.assertEqual(len(self.events()), 2, "Own replies/receipts must not create an echo loop")

    def test_transport_directed_messages_only_wake_the_recipient(self):
        self.start("first")
        self.start("second")
        sent = self.chat.send("writer", "@second 私聊")
        self.eventually(lambda: len(self.events()) == 1)
        time.sleep(.3)
        self.assertEqual([(e["name"], e["message_id"]) for e in self.events()], [("second", sent["message_id"])])

    def test_transport_busy_turn_defers_two_consecutive_messages_without_stopping(self):
        background = self.chat.background
        with patch.object(background, "start"):
            background.join("reader", wake_config=self.config(), env={})
        generation = background.reserve("reader", "cli", "callback")
        stopped, ready = threading.Event(), threading.Event()
        attempts, accepted = [], []
        def deliver(config, event):
            attempts.append(event["message_id"])
            if not ready.is_set():
                return {"accepted": False, "status": "busy", "retry_safe": True}
            ready.clear()
            accepted.append(event["message_id"])
            return {"accepted": True}
        with patch("chat_background.BUSY_RETRY_DELAY", .15), patch("adapters.deliver", side_effect=deliver):
            worker = threading.Thread(target=background.run, args=("reader", generation), kwargs={"stopped": stopped})
            worker.start()
            try:
                time.sleep(.1)
                self.assertEqual(attempts, [])
                first = self.chat.send("writer", "第一条", to="reader")
                self.eventually(lambda: background.status("reader")["state"] == "waiting_for_turn")
                self.assertTrue(background.status("reader")["running"])
                ready.set()
                self.eventually(lambda: accepted == [first["message_id"]])
                self.chat.receive("reader")
                self.chat.send("reader", "已处理第一条", reply_to=first["message_id"])
                second = self.chat.send("writer", "回复尚未结束时到达", to="reader")
                self.eventually(lambda: background.status("reader")["state"] == "waiting_for_turn")
                self.assertEqual(accepted, [first["message_id"]])
                ready.set()
                self.eventually(lambda: accepted == [first["message_id"], second["message_id"]])
                self.chat.receive("reader"); self.chat.ack("reader", second["message_id"])
                self.eventually(lambda: background.status("reader")["state"] == "waiting")
                count = len(attempts)
                time.sleep(.35)
                self.assertEqual(len(attempts), count)
            finally:
                stopped.set(); worker.join(5)
            self.assertFalse(worker.is_alive())

    def test_transport_rejoin_keeps_one_worker_and_leave_stops_future_wakes(self):
        first = self.start()
        again = self.chat.background.join("reader", wake_config=self.config(), env={})
        self.assertEqual(first["background"]["pid"], again["background"]["pid"])
        self.chat.leave("reader")
        self.chat.send("writer", "退出后的消息", to="reader")
        self.eventually(lambda: self.chat.process_alive(self.workers[0]) is not True)
        self.assertFalse(self.chat.background.status("reader")["running"])
        self.assertEqual(self.events(), [])

    def test_transport_ambiguous_host_failure_is_not_retried_by_reconnect(self):
        self.start(exit_code=7)
        sent = self.chat.send("writer", "不能重复唤醒", to="reader")
        self.eventually(lambda: self.chat.background.status("reader")["state"] == "needs_attention")
        self.eventually(lambda: self.chat.process_alive(self.workers[0]) is not True)
        again = self.chat.background.join("reader", wake_config=self.config(7), env={})
        if again["background"].get("pid"):
            self.workers.append(again["background"]["pid"])
        time.sleep(.4)
        self.assertEqual(len(self.events()), 1)
        self.assertEqual(self.chat.receive("reader")["messages"][0]["id"], sent["message_id"])

    def test_network_room_watch_is_blocked_and_manual_mail_remains_available(self):
        protect_network(self.data)
        joined = self.chat.background.join("claude", client="claude", env={})
        self.assertFalse(joined["background"]["running"])
        self.assertNotIn("background_task", joined)
        first = self.chat.send("writer", "你好", to="claude")
        process = subprocess.run([sys.executable, str(ROOT / "bin/airelay"), "--data-dir", str(self.data),
                                  "room", "watch", "work", "--name", "claude"], capture_output=True, text=True, timeout=5)
        self.assertEqual(process.returncode, 2)
        self.assertIn("手动收件", process.stdout)
        self.assertEqual(self.chat.receive("claude")["messages"][0]["id"], first["message_id"])
        reply = self.chat.send("claude", "回复", reply_to=first["message_id"])
        self.assertTrue(reply["acknowledged"])
        self.assertNotIn("background_task", reply)
        self.assertEqual(self.events(), [])

    def test_transport_rejoin_after_accepted_but_unreceived_wake_reports_recovery(self):
        self.start()
        sent = self.chat.send("writer", "通知后尚未领取", to="reader")
        self.eventually(lambda: len(self.events()) == 1)
        self.eventually(lambda: self.chat.background.status("reader")["state"] == "awaiting_receipt")
        self.chat.leave("reader")
        self.eventually(lambda: not self.chat.background.status("reader")["worker_active"])
        joined = self.chat.background.join("reader", wake_config=self.config(), env={})
        self.assertFalse(joined["listening"])
        self.assertEqual(joined["background"]["state"], "needs_attention")
        self.assertEqual(joined["recovery"]["message_id"], sent["message_id"])
        self.assertEqual(len(self.events()), 1)
        self.assertEqual(self.chat.receive("reader")["messages"][0]["id"], sent["message_id"])
        self.chat.ack("reader", sent["message_id"])
        restarted = self.start()
        self.assertTrue(restarted["background"]["running"])

    def test_unverified_native_watch_cannot_restart_after_leave(self):
        self.chat.background.join("claude", client="claude", env={})
        self.chat.leave("claude")
        with self.assertRaises(RelayError):
            self.chat.background.watch("claude")

    def test_shared_mcp_never_infers_an_unrelated_codex_thread(self):
        with patch("chat_background.adapters.doctor") as doctor:
            joined = self.chat.background.join("mcp-reader", owner="mcp:test", env={"CODEX_THREAD_ID": "wrong-chat"})
        doctor.assert_not_called()
        self.assertEqual(joined["background"]["state"], "needs_adapter")
        self.assertFalse(joined["listening"])
        self.assertIsNone(joined["next_tool"])

    def test_claude_mcp_join_returns_native_task_without_model_polling(self):
        endpoint = ChatEndpoint(self.data)
        self.addCleanup(endpoint.close)
        endpoint.client_name = "claude-code"
        joined = endpoint.invoke("chat_join", {"room": "work", "name": "claude-mcp"})
        self.assertEqual(joined["background"]["state"], "needs_background_task")
        self.assertTrue(joined["background_task"]["run_in_background"])
        self.assertEqual(self.events(), [])

    def test_generic_host_capability_does_not_require_a_brand(self):
        endpoint = ChatEndpoint(self.data)
        self.addCleanup(endpoint.close)
        endpoint.client_name = "unbranded-agent"
        manual = endpoint.invoke("chat_join", {"room": "work", "name": "unadapted", "protocol_version": 2})
        self.assertEqual(manual["background"]["state"], "needs_adapter")
        self.assertFalse(manual["listening"])
        endpoint.invoke("chat_leave", {"room": "work"})
        joined = endpoint.invoke("chat_join", {"room": "work", "name": "generic", "client": "background-task", "protocol_version": 2})
        self.assertEqual(joined["background"]["mode"], "host-task")
        self.assertFalse(joined["background"]["running"])
        self.assertEqual(joined["background"]["state"], "needs_background_task")
        self.assertNotIn("Claude", joined["background_task"]["instructions"])
        self.assertIsNone(joined["next_tool"])
        self.assertEqual(self.events(), [])

    def test_mcp_receive_reply_ack_use_the_same_durable_progress(self):
        peer = Peer(self.data)
        self.addCleanup(peer.close)
        peer.tool("chat_join", room="work", name="receiver", client="manual")
        first = self.chat.send("writer", "回复这条", to="receiver")
        second = self.chat.send("writer", "只读即可", to="receiver")
        self.assertEqual(peer.tool("chat_receive", room="work")["messages"][0]["id"], first["message_id"])
        result = peer.tool("chat_send", room="work", content="完成", reply_to=first["message_id"], key="answer")
        self.assertTrue(result["acknowledged"])
        self.assertEqual(peer.tool("chat_receive", room="work")["messages"][0]["id"], second["message_id"])
        peer.tool("chat_ack", room="work", message_id=second["message_id"])
        self.assertEqual(peer.tool("chat_receive", room="work")["status"], "empty")

    def test_reply_failure_cannot_ack_a_received_message(self):
        self.chat.join("reader")
        original = self.chat.send("writer", "执行并报告", to="reader")
        self.chat.receive("reader")
        self.chat.send("reader", "先前消息", key="conflict")
        with self.assertRaises(RelayError):
            self.chat.send("reader", "结果", reply_to=original["message_id"], key="conflict")
        self.assertEqual(self.chat.receive("reader")["status"], "needs_review")

    def test_batch_reply_cannot_ack_other_unprocessed_messages(self):
        self.chat.join("reader")
        self.chat.send("writer", "第一条", to="reader")
        second = self.chat.send("writer", "第二条", to="reader")
        batch = self.chat.wait("reader", timeout=0)
        self.assertEqual(len(batch["messages"]), 2)
        reply = self.chat.send("reader", "只处理了第二条", reply_to=second["message_id"])
        self.assertFalse(reply["acknowledged"])
        with self.assertRaises(RelayError):
            self.chat.ack("reader", second["message_id"])
        self.assertEqual(self.chat.receive("reader")["status"], "needs_review")


if __name__ == "__main__":
    unittest.main()
