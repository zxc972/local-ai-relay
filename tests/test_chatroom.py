"""Real independent stdio/CLI processes; no desktop access or model calls."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from chatroom import ChatRoom
from core import RelayError
from test_message_bus import Peer
from room_cli import Room


class ChatRoomTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="chatroom 测试 space ")
        self.addCleanup(self.temp.cleanup)
        self.data = Path(self.temp.name)
        self.room = ChatRoom(self.data, "work")
        self.room.create()

    def peer(self):
        peer = Peer(self.data, cli=True)
        self.addCleanup(peer.close)
        return peer

    def eventually(self, predicate):
        deadline = time.monotonic() + 4
        while not predicate():
            if time.monotonic() > deadline:
                self.fail("Expected listener state did not arrive")
            time.sleep(.02)

    def waiting(self, name):
        return next((item["listening"] for item in self.room.status()["members"] if item["name"] == name), False)

    @staticmethod
    def begin_wait(peer, after=0):
        return peer.begin("tools/call", {"name": "chat_wait", "arguments": {"room": "work", "after_seq": after}})

    def test_three_independent_clients_broadcast_then_direct_reply(self):
        first, second, third = self.peer(), self.peer(), self.peer()
        for peer, name in ((first, "agent-a"), (second, "desktop-b"), (third, "cli-c")):
            joined = peer.tool("chat_join", room="work", name=name)
            self.assertFalse(joined["listening"])
            self.assertEqual(joined["next_after_seq"], 0)
        b_wait, c_wait = self.begin_wait(second), self.begin_wait(third)
        self.eventually(lambda: self.waiting("desktop-b") and self.waiting("cli-c"))
        # The tool stays pending; the host need not ask the model to poll.
        time.sleep(.25)
        self.assertTrue(second.inbox.empty())
        self.assertTrue(third.inbox.empty())
        sent = first.tool("chat_send", room="work", content="你好，所有成员", key="hello")
        for peer, request in ((second, b_wait), (third, c_wait)):
            packet = peer.receive(request)["structuredContent"]
            self.assertEqual([m["id"] for m in packet["messages"]], [sent["message_id"]])
            self.assertEqual(packet["messages"][0]["content"], "你好，所有成员")
        a_wait = self.begin_wait(first)
        reply = second.tool("chat_send", room="work", to="agent-a", content="已收到", reply_to=sent["message_id"])
        result = first.receive(a_wait)["structuredContent"]
        self.assertEqual(result["messages"][0]["id"], reply["message_id"])
        self.assertEqual(result["messages"][0]["reply_to"], sent["message_id"])
        other = third.tool("chat_wait", room="work", after_seq=sent["seq"], timeout=0)
        self.assertEqual(other["messages"], [])
        self.assertEqual(other["next_after_seq"], reply["seq"])

    def test_leave_interrupts_wait_and_releases_nickname(self):
        peer = self.peer()
        peer.tool("chat_join", room="work", name="member")
        waiting = self.begin_wait(peer)
        self.eventually(lambda: self.waiting("member"))
        peer.tool("chat_leave", room="work")
        self.assertEqual(peer.receive(waiting)["structuredContent"]["status"], "left")
        another = self.peer()
        self.assertEqual(another.tool("chat_join", room="work", name="member")["status"], "joined")

    def test_cancel_does_not_consume_messages_and_allows_wait_again(self):
        peer = self.peer()
        peer.tool("chat_join", room="work", name="reader")
        waiting = self.begin_wait(peer)
        self.eventually(lambda: self.waiting("reader"))
        peer.notify("notifications/cancelled", {"requestId": waiting})
        self.eventually(lambda: not self.waiting("reader"))
        self.room.join("writer")
        sent = self.room.send("writer", "取消之后仍保留")
        result = peer.tool("chat_wait", room="work", after_seq=0, timeout=0)
        self.assertEqual(result["messages"][0]["id"], sent["message_id"])
        self.assertNotIn(waiting, peer.saved)

    def test_duplicate_wait_is_rejected_without_interrupting_original(self):
        peer = self.peer()
        peer.tool("chat_join", room="work", name="reader")
        waiting = self.begin_wait(peer)
        self.eventually(lambda: self.waiting("reader"))
        self.assertTrue(peer.tool("chat_join", room="work", name="reader")["listening"])
        failed = peer.call("tools/call", {"name": "chat_wait", "arguments": {"room": "work", "after_seq": 0}})
        self.assertTrue(failed["isError"])
        self.assertTrue(self.waiting("reader"))
        peer.tool("chat_leave", room="work")
        peer.receive(waiting)

    def test_connection_cannot_impersonate_or_claim_an_active_nickname(self):
        first, second = self.peer(), self.peer()
        first.tool("chat_join", room="work", name="owner")
        for name, arguments in (
            ("chat_join", {"room": "work", "name": "owner"}),
            ("chat_send", {"room": "work", "content": "not joined"}),
            ("chat_wait", {"room": "work", "after_seq": 0, "timeout": 0}),
        ):
            self.assertTrue(second.call("tools/call", {"name": name, "arguments": arguments})["isError"])
        first.tool("chat_send", room="work", content="safe")
        invalid = first.call("tools/call", {"name": "chat_send", "arguments": {
            "room": "work", "name": "someone-else", "content": "forged"}})
        self.assertTrue(invalid["isError"])

    def test_cursor_replays_after_restart_and_join_does_not_skip_new_messages(self):
        self.room.join("reader")
        self.room.join("writer")
        sent = self.room.send("writer", "持久化的消息")
        self.assertEqual(self.room.join("reader")["next_after_seq"], 0)
        reopened = ChatRoom(self.data, "work")
        packet = reopened.wait("reader", 0, timeout=0)
        self.assertEqual(packet["messages"][0]["id"], sent["message_id"])
        self.assertEqual(reopened.wait("reader", packet["next_after_seq"], timeout=0)["messages"], [])

    def test_idempotency_private_reply_and_room_isolation(self):
        for name in ("a", "b", "c"):
            self.room.join(name)
        sent = self.room.send("a", "只给 b", to="b", key="one")
        self.assertTrue(self.room.send("a", "只给 b", to="b", key="one")["duplicate"])
        with self.assertRaises(RelayError):
            self.room.send("a", "不同内容", to="b", key="one")
        with self.assertRaises(RelayError):
            self.room.send("c", "看不见的原消息", reply_to=sent["message_id"])
        response = self.room.send("b", "私聊回复", reply_to=sent["message_id"])
        self.assertEqual(response["to"], "a")
        self.assertEqual(self.room.wait("c", 0, timeout=0)["messages"], [])
        other = ChatRoom(self.data, "other")
        other.create()
        other.join("a")
        self.assertEqual(other.wait("a", 0, timeout=0)["messages"], [])
        with self.assertRaises(RelayError):
            other.send("a", "跨房间关联", reply_to=sent["message_id"])

    def test_leading_mention_is_private_and_invalid_targets_never_broadcast(self):
        for name in ("you", "codex", "claude-desktop"):
            self.room.join(name)
        sent = self.room.send("you", "@codex 你好", key="mention")
        self.assertEqual(sent["to"], "codex")
        self.assertTrue(self.room.send("you", "@codex 你好", key="mention")["duplicate"])
        self.assertEqual(self.room.receive("claude-desktop")["messages"], [])
        self.assertEqual(self.room.receive("codex")["messages"][0]["id"], sent["message_id"])
        reply = self.room.send("codex", "私聊回复", reply_to=sent["message_id"])
        self.assertEqual(reply["to"], "you")
        self.assertEqual(self.room.receive("claude-desktop")["messages"], [])
        for text, to in (("@unknown 保密", None), ("@ 你好", None), ("@you 自发", None),
                         ("@codex @claude-desktop 两人", None), ("@codex 保密", "claude-desktop")):
            with self.assertRaises(RelayError):
                self.room.send("you", text, to=to)
        broadcast = self.room.send("you", "邮件 me@example.com；这里提到 @codex")
        self.assertIsNone(broadcast["to"])
        self.assertEqual(self.room.receive("claude-desktop")["messages"][0]["id"], broadcast["message_id"])

    def test_late_join_starts_at_current_cursor_and_batches_keep_order(self):
        self.room.join("writer")
        self.room.send("writer", "加入前的消息")
        start = self.room.join("reader")["next_after_seq"]
        self.assertEqual(start, 1)
        for i in range(53):
            self.room.send("writer", str(i))
        first = self.room.wait("reader", start, timeout=0)
        second = self.room.wait("reader", first["next_after_seq"], timeout=0)
        self.assertEqual([m["content"] for m in first["messages"] + second["messages"]], [str(i) for i in range(53)])

    def test_invalid_requests_and_missing_rooms_do_not_create_members(self):
        peer = self.peer()
        for tool, args in (
            ("chat_join", {"room": "missing", "name": "reader"}),
            ("chat_join", {"room": "../escape", "name": "reader"}),
            ("chat_join", {"room": "work", "name": "bad name"}),
            ("chat_wait", {"room": "work", "after_seq": True}),
        ):
            self.assertTrue(peer.call("tools/call", {"name": tool, "arguments": args})["isError"])
        self.assertEqual(self.room.status()["members"], [])
        self.assertEqual(self.room.status()["latest_seq"], 0)

    def test_mcp_eof_cancels_wait_and_unregisters_member(self):
        peer = Peer(self.data)
        try:
            peer.tool("chat_join", room="work", name="short-lived")
            self.begin_wait(peer)
            self.eventually(lambda: self.waiting("short-lived"))
        finally:
            peer.close()
        member = self.room.status()["members"][0]
        self.assertFalse(member["joined"])
        self.assertFalse(member["listening"])

    def test_shutdown_after_explicit_nickname_recovery_preserves_new_owner(self):
        original = Peer(self.data)
        replacement = self.peer()
        try:
            original.tool("chat_join", room="work", name="recovered")
            self.room.leave("recovered", owner=None)
            replacement.tool("chat_join", room="work", name="recovered")
        finally:
            original.close()
        self.assertTrue(self.room.status()["members"][0]["joined"])
        self.assertEqual(replacement.tool("chat_send", room="work", content="still joined")["status"], "sent")

    def test_cli_join_send_wait_and_leave_without_mcp_or_host_adapter(self):
        def cli(*args):
            result = subprocess.run([sys.executable, str(ROOT / "bin" / "airelay"),
                                     "--data-dir", str(self.data), "room", *args],
                                    check=True, capture_output=True, text=True, encoding="utf-8", timeout=5)
            return json.loads(result.stdout)
        self.assertEqual(cli("create", "cli-room")["status"], "created")
        self.assertEqual(cli("join", "cli-room", "--name", "desktop")["next_after_seq"], 0)
        cli("join", "cli-room", "--name", "terminal")
        sent = cli("send", "cli-room", "--name", "terminal", "--text", "你好")
        packet = cli("wait", "cli-room", "--name", "desktop", "--after-seq", "0", "--timeout", "0")
        self.assertEqual(packet["messages"][0]["id"], sent["message_id"])
        self.assertEqual(cli("leave", "cli-room", "--name", "terminal")["status"], "left")

    def test_reconnect_restores_confirmed_progress_and_receives_offline_messages(self):
        first = Peer(self.data)
        self.room.join("writer")
        try:
            first.tool("chat_join", room="work", name="reader")
            old = self.room.send("writer", "第一条")
            packet = first.tool("chat_wait", room="work", timeout=0)
            self.assertEqual(packet["messages"][0]["id"], old["message_id"])
            first.tool("chat_wait", room="work", after_seq=packet["next_after_seq"], timeout=0)
        finally:
            first.close()
        new = self.room.send("writer", "离线期间的新消息")
        second = self.peer()
        joined = second.tool("chat_join", room="work", name="reader")
        self.assertEqual(joined["next_after_seq"], old["seq"])
        packet = second.tool("chat_wait", room="work", timeout=0)
        self.assertEqual([m["id"] for m in packet["messages"]], [new["message_id"]])

    def test_unconfirmed_delivery_blocks_automatic_reexecution_after_reconnect(self):
        first = Peer(self.data)
        self.room.join("writer")
        try:
            first.tool("chat_join", room="work", name="reader")
            sent = self.room.send("writer", "可能已执行的操作")
            first.tool("chat_wait", room="work", timeout=0)
        finally:
            first.close()
        later = self.room.send("writer", "下一条")
        second = self.peer()
        joined = second.tool("chat_join", room="work", name="reader")
        self.assertEqual(joined["status"], "needs_review")
        self.assertEqual(joined["pending_through_seq"], sent["seq"])
        self.assertIsNone(joined["next_tool"])
        self.assertEqual(joined["messages"], [])
        blocked = second.tool("chat_wait", room="work", timeout=0)
        self.assertEqual(blocked["status"], "needs_review")
        history = second.tool("chat_wait", room="work", after_seq=0, replay=True)
        self.assertTrue(history["history_only"])
        self.assertEqual([m["id"] for m in history["messages"]], [sent["message_id"]])
        self.assertIsNone(history["next_tool"])
        self.assertEqual(second.tool("chat_wait", room="work", timeout=0)["status"], "needs_review")
        recovered = second.tool("chat_wait", room="work", after_seq=sent["seq"], timeout=0)
        self.assertEqual([m["id"] for m in recovered["messages"]], [later["message_id"]])

    def test_cli_leave_and_rejoin_keeps_progress(self):
        self.room.join("reader")
        self.room.join("writer")
        first = self.room.send("writer", "已处理")
        self.room.wait("reader", timeout=0)
        self.room.wait("reader", first["seq"], timeout=0)
        self.room.leave("reader")
        second = self.room.send("writer", "离线消息")
        joined = ChatRoom(self.data, "work").join("reader")
        self.assertEqual(joined["next_after_seq"], first["seq"])
        result = self.room.wait("reader", timeout=0)
        self.assertEqual(result["messages"][0]["id"], second["message_id"])

    def test_stale_cursor_resumes_forward_and_explicit_replay_is_read_only(self):
        self.room.join("writer")
        self.room.join("reader")
        sent = self.room.send("writer", "旧消息")
        self.room.wait("reader", timeout=0)
        self.room.wait("reader", sent["seq"], timeout=0)
        self.assertEqual(self.room.wait("reader", 0, timeout=0)["messages"], [])
        history = self.room.wait("reader", 0, replay=True)
        self.assertEqual(history["status"], "history")
        self.assertEqual(history["messages"][0]["id"], sent["message_id"])
        self.assertEqual(self.room.join("reader")["next_after_seq"], sent["seq"])

    def test_unread_messages_cannot_be_skipped_by_supplying_a_later_cursor(self):
        self.room.join("writer")
        self.room.join("reader")
        sent = self.room.send("writer", "不能跳过")
        with self.assertRaises(RelayError):
            self.room.wait("reader", sent["seq"], timeout=0)
        packet = self.room.wait("reader", timeout=0)
        self.assertEqual(packet["messages"][0]["id"], sent["message_id"])

    def test_status_distinguishes_idle_waiting_unconfirmed_and_left(self):
        peer = self.peer()
        peer.tool("chat_join", room="work", name="reader", client="manual")
        def member():
            return next(m for m in self.room.status()["members"] if m["name"] == "reader")
        self.assertEqual(member()["state"], "idle")
        self.assertFalse(member()["listening"])
        waiting = self.begin_wait(peer)
        self.eventually(lambda: member()["state"] == "waiting")
        self.room.join("writer")
        self.room.send("writer", "新消息")
        peer.receive(waiting)
        self.assertEqual(member()["state"], "unconfirmed")
        self.assertFalse(member()["listening"])
        peer.tool("chat_leave", room="work")
        self.assertEqual(member()["state"], "left")

    @unittest.skipUnless(os.name == "posix", "POSIX read-only PID liveness check")
    def test_killed_waiter_reports_interrupted_and_same_name_can_reconnect(self):
        peer = Peer(self.data)
        try:
            peer.tool("chat_join", room="work", name="reader")
            self.begin_wait(peer)
            self.eventually(lambda: self.waiting("reader"))
        finally:
            peer.process.kill()
            peer.process.wait(timeout=5)
            peer.process.stdin.close()
            peer.reader.join(timeout=2)
            peer.process.stdout.close()
            peer.process.stderr.close()
        member = self.room.status()["members"][0]
        self.assertEqual(member["state"], "interrupted")
        self.assertFalse(member["listening"])
        self.room.join("writer")
        sent = self.room.send("writer", "中断期间的新消息")
        replacement = self.peer()
        joined = replacement.tool("chat_join", room="work", name="reader")
        self.assertEqual(joined["next_after_seq"], 0)
        self.assertEqual(replacement.tool("chat_wait", room="work", timeout=0)["messages"][0]["id"], sent["message_id"])

    def test_legacy_cursor_migration_requires_review_and_runs_only_once(self):
        old = Room("legacy", self.data)
        with old.store.transaction() as db:
            db.execute("INSERT INTO meta VALUES('chat:created_at','1')")
            db.execute("CREATE TABLE chat_members(name TEXT PRIMARY KEY,owner TEXT,active INTEGER,"
                       "joined_at REAL,joined_seq INTEGER,wait_id TEXT,waiting_at REAL)")
            db.execute("CREATE TABLE chat_messages(seq INTEGER PRIMARY KEY AUTOINCREMENT,id TEXT UNIQUE,"
                       "sender TEXT,recipient TEXT,content TEXT,reply_to TEXT,created_at REAL,key TEXT,UNIQUE(sender,key))")
            db.execute("INSERT INTO chat_members VALUES('reader','cli',0,1,0,NULL,NULL)")
            db.execute("INSERT INTO chat_messages VALUES(1,'old-id','writer','reader','旧版未记录处理进度',NULL,1,NULL)")
        new = ChatRoom(self.data, "legacy")
        self.assertEqual(new.join("reader")["status"], "needs_review")
        self.assertEqual(new.wait("reader", timeout=0)["status"], "needs_review")
        self.assertEqual(new.wait("reader", after_seq=0, replay=True)["messages"][0]["id"], "old-id")
        new.wait("reader", 1, timeout=0)
        self.assertEqual(ChatRoom(self.data, "legacy").join("reader")["next_after_seq"], 1)


if __name__ == "__main__":
    unittest.main()
