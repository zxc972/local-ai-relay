"""Two anonymous MCP peers exercise the universal middleware, with no model calls."""
import contextlib
import json
import os
import queue
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from room_cli import ENTRY, Room


class Peer:
    def __init__(self, data, cli=False):
        command = [sys.executable, str(ENTRY), "--data-dir", str(data), "mcp"] if cli else [
            sys.executable, str(ROOT / "mcp_server.py"), "--data-dir", str(data)]
        env = {k: v for k, v in os.environ.items() if k not in ("CODEX_THREAD_ID", "AIRELAY_SESSION_ID")}
        self.process = subprocess.Popen(command, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=subprocess.PIPE, text=True, bufsize=1)
        self.inbox, self.saved, self.notices = queue.Queue(), {}, []
        self.counter = 0
        self.reader = threading.Thread(target=self.read, daemon=True)
        self.reader.start()
        self.initialized = self.call("initialize", {"protocolVersion": "2025-06-18",
                                                   "clientInfo": {"name": "generic-peer", "version": "1"}})

    def read(self):
        for line in self.process.stdout:
            self.inbox.put(json.loads(line))

    def notify(self, method, params):
        self.write({"jsonrpc": "2.0", "method": method, "params": params})

    def write(self, payload):
        self.process.stdin.write(json.dumps(payload) + "\n")
        self.process.stdin.flush()

    def begin(self, method, params):
        self.counter += 1
        self.write({"jsonrpc": "2.0", "id": self.counter, "method": method, "params": params})
        return self.counter

    def receive(self, rid):
        if rid in self.saved:
            return self.saved.pop(rid)["result"]
        deadline = time.monotonic() + 5
        while True:
            packet = self.inbox.get(timeout=max(.01, deadline - time.monotonic()))
            if packet.get("id") == rid:
                return packet["result"]
            if "id" in packet:
                self.saved[packet["id"]] = packet
            else:
                self.notices.append(packet)

    def call(self, method, params):
        return self.receive(self.begin(method, params))

    def tool(self, name, **arguments):
        response = self.call("tools/call", {"name": name, "arguments": arguments})
        if response.get("isError"):
            raise AssertionError(response)
        return response["structuredContent"]

    def listen(self, role):
        return self.begin("tools/call", {"name": "relay_listen", "arguments": {
            "room": "shared", "role": role, "timeout": 4}, "_meta": {"progressToken": role + "-wait"}})

    def wait_for_progress(self):
        deadline = time.monotonic() + 3
        while True:
            if any(p.get("method") == "notifications/progress" for p in self.notices):
                return
            packet = self.inbox.get(timeout=max(.01, deadline - time.monotonic()))
            if "id" in packet:
                self.saved[packet["id"]] = packet
            else:
                self.notices.append(packet)

    def close(self):
        self.process.stdin.close()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=2)
        self.reader.join(timeout=1)
        error = self.process.stderr.read()
        self.process.stdout.close()
        self.process.stderr.close()
        if self.process.returncode:
            raise AssertionError(error)


class MessageBusTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="generic MCP ")
        self.data = Path(self.temp.name) / "数据 space"
        self.sender, self.receiver = Peer(self.data, cli=True), Peer(self.data)

    def tearDown(self):
        self.sender.close()
        self.receiver.close()
        self.temp.cleanup()

    def connect(self):
        first = self.sender.tool("relay_connect", room="shared", role="sender")
        second = self.receiver.tool("relay_connect", room="shared", role="receiver")
        self.assertEqual(first["status"], "waiting_for_peer")
        self.assertEqual(second["status"], "connected")
        self.assertIsNone(first["next_tool"])
        self.assertEqual(first["mode"], "needs_adapter")
        self.assertFalse(first["background"]["running"])
        self.assertTrue(first["role_info"]["can_issue_commands"])
        self.assertFalse(second["role_info"]["can_issue_commands"])
        self.assertTrue(second["role_info"]["can_report_results"])

    def test_two_independent_peers_listen_send_and_reply_while_listening(self):
        names = {t["name"] for t in self.sender.call("tools/list", {})["tools"]}
        self.assertEqual(names, {"relay_connect", "relay_send", "relay_listen", "relay_receive", "relay_reply", "relay_ack", "relay_status", "relay_disconnect"})
        self.assertNotIn("Codex", self.sender.initialized["instructions"])
        self.assertNotIn("Claude", self.sender.initialized["instructions"])
        self.connect()
        room = Room("shared", self.data)
        with contextlib.closing(room.store.connect()) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM tasks").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM channel_messages").fetchone()[0], 0)
        waiting_r = self.receiver.listen("receiver")
        waiting_s = self.sender.listen("sender")
        self.receiver.wait_for_progress()
        self.sender.wait_for_progress()
        status = self.sender.tool("relay_status", room="shared")
        self.assertTrue(all(p["mcp"]["listening"] for p in status["peers"].values()))
        sent = self.sender.tool("relay_send", room="shared", role="sender", content="17 × 23", key="sample")
        duplicate = self.sender.tool("relay_send", room="shared", role="sender", content="17 × 23", key="sample")
        self.assertTrue(duplicate["duplicate"])
        response = self.receiver.receive(waiting_r)
        self.assertFalse(response["isError"])
        work = response["structuredContent"]
        self.assertEqual(work["message"]["id"], sent["message_id"])
        self.assertEqual(work["message"]["content"], "17 × 23")
        self.assertEqual(work["message"]["kind"], "command")
        self.assertIn("reply_tool", work)
        self.assertIsNone(work["after_handling_tool"])
        answer = self.receiver.tool("relay_reply", room="shared", role="receiver", message_id=sent["message_id"], content="391")
        repeated = self.receiver.tool("relay_reply", room="shared", role="receiver", message_id=sent["message_id"], content="391")
        self.assertTrue(repeated["duplicate"])
        back = self.sender.receive(waiting_s)["structuredContent"]
        self.assertEqual(back["message"]["content"], "391")
        self.assertEqual(back["message"]["kind"], "result")
        self.assertNotIn("reply_tool", back)
        self.assertEqual(back["followup_tool"]["name"], "relay_send")
        self.assertEqual(back["message"]["reply_to"], sent["message_id"])
        self.assertEqual(back["message"]["id"], answer["message_id"])
        self.sender.tool("relay_ack", room="shared", role="sender", message_id=answer["message_id"])
        with contextlib.closing(room.store.connect()) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM channel_messages WHERE state='done'").fetchone()[0], 2)

    def test_receiver_cannot_dispatch_commands_or_impersonate_sender(self):
        self.connect()
        attempts = [
            (self.receiver, "relay_send", {"room": "shared", "role": "receiver", "content": "反向下指令"}),
            (self.receiver, "relay_send", {"room": "shared", "role": "sender", "content": "伪装 sender 下指令"}),
            (self.receiver, "relay_connect", {"room": "shared", "role": "sender"}),
            (self.receiver, "relay_disconnect", {"room": "shared", "role": "sender"}),
            (self.sender, "relay_reply", {"room": "shared", "role": "receiver", "message_id": "fake", "content": "伪造结果"}),
        ]
        for peer, name, arguments in attempts:
            response = peer.call("tools/call", {"name": name, "arguments": arguments})
            self.assertTrue(response["isError"], (name, arguments))
        # The CLI uses the same command/result direction checks.
        env = {k: v for k, v in os.environ.items() if k not in ("CODEX_THREAD_ID", "AIRELAY_SESSION_ID")}
        rejected = subprocess.run([sys.executable, str(ENTRY), "--data-dir", str(self.data),
                                   "send", "shared", "--as", "receiver", "--text", "独立下发指令"],
                                  env=env, capture_output=True, text=True, timeout=5)
        self.assertNotEqual(rejected.returncode, 0)
        self.assertIn("sender", json.loads(rejected.stdout)["error"])
        with contextlib.closing(Room("shared", self.data).store.connect()) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM channel_messages").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM channel_peers").fetchone()[0], 2)

    def test_sender_reviews_result_and_dispatches_a_correlated_followup(self):
        self.connect()
        first = self.sender.tool("relay_send", room="shared", role="sender", content="计算 17 × 23")
        self.receiver.tool("relay_listen", room="shared", role="receiver", timeout=0)
        answer = self.receiver.tool("relay_reply", room="shared", role="receiver", message_id=first["message_id"], content="391")
        review = self.sender.tool("relay_listen", room="shared", role="sender", timeout=0)
        self.assertEqual(review["message"]["kind"], "result")
        feedback = self.sender.tool("relay_send", room="shared", role="sender", content="在 391 上加 9",
                                    reply_to=answer["message_id"], key="review-one")
        repeated = self.sender.tool("relay_send", room="shared", role="sender", content="在 391 上加 9",
                                    reply_to=answer["message_id"], key="review-one")
        self.assertTrue(repeated["duplicate"])
        self.assertEqual(feedback["kind"], "command")
        command = self.receiver.tool("relay_listen", room="shared", role="receiver", timeout=0)
        self.assertEqual(command["message"]["kind"], "command")
        self.assertEqual(command["message"]["reply_to"], answer["message_id"])
        self.assertEqual(command["message"]["content"], "在 391 上加 9")
        wrong_parent = self.receiver.call("tools/call", {"name": "relay_reply", "arguments": {
            "room": "shared", "role": "receiver", "message_id": answer["message_id"], "content": "不是原指令"}})
        self.assertTrue(wrong_parent["isError"])
        result = self.receiver.tool("relay_reply", room="shared", role="receiver", message_id=feedback["message_id"], content="400")
        final = self.sender.tool("relay_listen", room="shared", role="sender", timeout=0)
        self.assertEqual(final["message"]["kind"], "result")
        self.assertEqual(final["message"]["content"], "400")
        self.sender.tool("relay_ack", room="shared", role="sender", message_id=result["message_id"])
        with contextlib.closing(Room("shared", self.data).store.connect()) as db:
            self.assertEqual(db.execute("SELECT state FROM channel_messages WHERE id=?", (answer["message_id"],)).fetchone()[0], "done")
            self.assertEqual(db.execute("SELECT COUNT(*) FROM channel_messages").fetchone()[0], 4)

    def test_idle_never_renews_and_disconnect_terminates_pending_listen(self):
        self.connect()
        idle = self.receiver.tool("relay_listen", room="shared", role="receiver", timeout=0)
        self.assertEqual(idle["status"], "empty")
        self.assertIsNone(idle["next_tool"])
        once = self.receiver.tool("relay_receive", room="shared", role="receiver")
        self.assertEqual(once["status"], "empty")
        self.assertIsNone(once["next_tool"])
        pending = self.receiver.listen("receiver")
        self.receiver.wait_for_progress()
        duplicate = self.receiver.tool("relay_listen", room="shared", role="receiver", timeout=0)
        self.assertEqual(duplicate["status"], "already_listening")
        self.assertIsNone(duplicate["next_tool"])
        self.receiver.tool("relay_disconnect", room="shared", role="receiver")
        stopped = self.receiver.receive(pending)["structuredContent"]
        self.assertEqual(stopped["status"], "disconnected")
        self.assertIsNone(stopped["next_tool"])
        blocked = self.sender.call("tools/call", {"name": "relay_send", "arguments": {
            "room": "shared", "role": "sender", "content": "不能发给已断开的端点"}})
        self.assertTrue(blocked["isError"])

    def test_cancelled_listen_does_not_consume_future_messages(self):
        self.connect()
        pending = self.receiver.listen("receiver")
        self.receiver.wait_for_progress()
        self.receiver.notify("notifications/cancelled", {"requestId": pending})
        deadline = time.monotonic() + 3
        while self.receiver.tool("relay_status", room="shared")["peers"]["receiver"]["mcp"]["listening"]:
            self.assertLess(time.monotonic(), deadline)
            time.sleep(.02)
        self.sender.tool("relay_send", room="shared", role="sender", content="取消监听后仍应保留")
        with contextlib.closing(Room("shared", self.data).store.connect()) as db:
            self.assertEqual(db.execute("SELECT state FROM channel_messages").fetchone()[0], "queued")
        received = self.receiver.tool("relay_listen", room="shared", role="receiver", timeout=0)
        self.assertEqual(received["message"]["content"], "取消监听后仍应保留")
        bad = self.receiver.call("tools/call", {"name": "relay_reply", "arguments": {
            "room": "shared", "role": "sender", "message_id": received["message"]["id"], "content": "不能回复别人领取的消息"}})
        self.assertTrue(bad["isError"])

    def test_manual_connect_and_invalid_arguments_never_invent_work(self):
        joined = self.sender.tool("relay_connect", room="shared", role="sender", listen=False)
        self.assertIsNone(joined["next_tool"])
        stopped = self.sender.tool("relay_listen", room="shared", role="sender", timeout=0)
        self.assertEqual(stopped["status"], "disconnected")
        for arguments in [{"room": "../escape", "role": "sender"}, {"room": "shared", "role": "brand"},
                          {"room": "shared", "role": "sender", "listen": "yes"}]:
            result = self.sender.call("tools/call", {"name": "relay_connect", "arguments": arguments})
            self.assertTrue(result["isError"])
        with contextlib.closing(Room("shared", self.data).store.connect()) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM channel_messages").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
