"""Lean v2 results and atomic combined replies; no real host or room."""
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from chatroom import ChatRoom
from chatroom_mcp import ChatEndpoint
from chat_context import LocalContext, with_local_hint
from chat_delivery import RULES_V2, units
from core import RelayError


class EfficiencyTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="chat-efficiency-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.chat = ChatRoom(self.root, "work")
        self.chat.create()
        for name in ("you", "other", "agent"):
            self.chat.join(name, protocol_version=2, trusted_human=name != "agent")

    def send(self, content="一条要求", **kwargs):
        return self.chat.send("you", content, to="agent", **kwargs)

    def reply(self, batch, mids, **kwargs):
        return self.chat.send("agent", "已处理这些要求", reply_to=mids[0], delivery_id=batch["delivery_id"], ack_ids=mids, **kwargs)

    def receipts(self, batch):
        with self.chat.store.connect() as db:
            return dict(db.execute("SELECT message_id,acked FROM chat_receipts WHERE delivery_id=?", (batch["delivery_id"],)))

    def test_combined_reply_partial_retry_and_next_batch_remain_exact(self):
        mids = [self.send(str(i))["message_id"] for i in range(3)]
        batch = self.chat.receive("agent")
        newer = self.send("稍后到达")["message_id"]
        first = self.reply(batch, mids[:2], key="combined")
        self.assertEqual(first["acknowledged_count"], 2)
        self.assertTrue(first["pending"])
        self.assertEqual(first["remaining"], 1)
        self.assertEqual(self.receipts(batch), {mids[0]: 1, mids[1]: 1, mids[2]: 0})
        retry = self.chat.send("agent", "已处理这些要求", reply_to=mids[0], delivery_id=batch["delivery_id"], ack_ids=list(reversed(mids[:2])), key="combined")
        self.assertTrue(retry["duplicate"])
        with self.assertRaises(RelayError):
            self.reply(batch, mids, key="combined")
        self.assertEqual(self.receipts(batch)[mids[2]], 0)
        self.reply(batch, mids[2:], key="last")
        next_batch = self.chat.receive("agent")
        self.assertEqual(next_batch["messages"][0]["id"], newer)
        # Retry the old completed batch while a new batch is pending.
        self.reply(batch, mids[:2], key="combined")
        self.assertEqual(self.chat.receive("agent")["delivery_id"], next_batch["delivery_id"])
        self.assertEqual(self.receipts(next_batch)[newer], 0)

    def test_later_message_and_unread_message_cannot_be_confirmed_by_reply(self):
        first = self.send()["message_id"]
        batch = self.chat.receive("agent")
        later = self.send("later")["message_id"]
        before = self.chat.status()["latest_seq"]
        with self.assertRaises(RelayError):
            self.reply(batch, [first, later], key="invalid-batch")
        self.assertEqual(self.chat.status()["latest_seq"], before)
        self.assertEqual(self.receipts(batch)[first], 0)
        self.reply(batch, [first])
        batch = self.chat.receive("agent")
        self.reply(batch, [later])
        long_id = self.send("完整约束必须读完。" * 2000)["message_id"]
        batch = self.chat.receive("agent")
        self.assertFalse(batch["messages"][0]["complete"])
        before = self.chat.status()["latest_seq"]
        with self.assertRaises(RelayError):
            self.reply(batch, [long_id])
        self.assertEqual(self.chat.status()["latest_seq"], before)
        self.assertEqual(self.receipts(batch)[long_id], 0)

    def test_transaction_failure_rolls_back_answer_and_all_receipts(self):
        mids = [self.send(str(i))["message_id"] for i in range(2)]
        batch = self.chat.receive("agent")
        before = self.chat.status()["latest_seq"]
        with self.chat.store.transaction() as db:
            db.execute("CREATE TRIGGER fail_ack BEFORE UPDATE OF acked ON chat_receipts WHEN OLD.message_id='" + mids[1] + "' BEGIN SELECT RAISE(ABORT,'disk failure'); END")
        with self.assertRaises(sqlite3.DatabaseError):
            self.reply(batch, mids, key="atomic")
        self.assertEqual(self.chat.status()["latest_seq"], before)
        self.assertEqual(set(self.receipts(batch).values()), {0})
        with self.chat.store.transaction() as db:
            db.execute("DROP TRIGGER fail_ack")
        self.assertFalse(self.reply(batch, mids, key="atomic")["duplicate"])

    def test_combined_answer_cannot_mix_private_audiences_or_public_sources(self):
        first = self.send("私聊 A")["message_id"]
        other = self.chat.send("other", "私聊 B", to="agent")["message_id"]
        public = self.chat.send("you", "公共消息")["message_id"]
        batch = self.chat.receive("agent")
        before = self.chat.status()["latest_seq"]
        for mids in ([first, other], [first, public], [public, first]):
            with self.subTest(mids=mids), self.assertRaises(RelayError):
                self.reply(batch, mids)
        self.assertEqual(self.chat.status()["latest_seq"], before)
        self.assertEqual(set(self.receipts(batch).values()), {0})

    def test_invalid_ack_lists_and_other_identity_are_rejected(self):
        mid = self.send()["message_id"]
        batch = self.chat.receive("agent")
        before = self.chat.status()["latest_seq"]
        for values in ([], [mid, mid], "not-an-array", ["absent"], [str(i) for i in range(17)]):
            with self.subTest(values=values), self.assertRaises(RelayError):
                self.chat.send("agent", "invalid", reply_to=mid, delivery_id=batch["delivery_id"], ack_ids=values)
        with self.assertRaises(RelayError):
            self.chat.send("agent", "invalid", reply_to=mid, ack_ids=[mid])
        with self.assertRaises(RelayError):
            self.chat.send("other", "invalid", reply_to=mid, delivery_id=batch["delivery_id"], ack_ids=[mid])
        self.chat.join("legacy")
        with self.assertRaises(RelayError):
            self.chat.send("legacy", "invalid", reply_to=mid, delivery_id=batch["delivery_id"], ack_ids=[mid])
        self.assertEqual(self.chat.status()["latest_seq"], before)

    def test_lookup_omits_checkpoint_and_recovery_preserves_it_and_rules(self):
        mid = self.send()["message_id"]
        batch = self.chat.receive("agent")
        self.chat.checkpoint("agent", [{"speaker": "you", "kind": "constraint", "text": "保留原始限制和证据。" * 30, "sources": [mid]}], 0)
        for options in ({"message_id": mid}, {"delivery_id": batch["delivery_id"]}, {"after_seq": 0}):
            with self.subTest(options=options), patch.object(LocalContext, "latest", side_effect=AssertionError("raw read must not load checkpoint")):
                result = self.chat.context("agent", budget=4000, **options)
                self.assertNotIn("checkpoint", result)
                self.assertNotIn("checkpoint_hint", result)
                self.assertNotIn("host_context", result)
                self.assertTrue(result["history_only"])
                self.assertLessEqual(units(result), 4000)
        restore = self.chat.context("agent", budget=4000)
        self.assertEqual(restore["checkpoint_version"], 1)
        self.assertIn("原始限制", restore["checkpoint"]["entries"][0]["text"])
        self.assertEqual(restore["instructions"], RULES_V2)
        self.assertLessEqual(units(restore), 4000)
        self.assertEqual(self.receipts(batch)[mid], 0)
        with self.assertRaises(RelayError):
            self.chat.context("other", message_id=mid)

    def test_due_hint_survives_compaction_and_rearms_local_host(self):
        self.chat.background.join("agent", client="claude", protocol_version=2, env={})
        mids = [self.send("x" * 2100)["message_id"] for _ in range(3)]
        for index, mid in enumerate(mids):
            batch = with_local_hint(self.chat, "agent", "cli", self.chat.receive("agent"))
            self.assertNotIn("checkpoint_hint", batch)
            result = with_local_hint(self.chat, "agent", "cli", self.reply(batch, [mid]))
            self.assertTrue(result["background_task"]["run_in_background"])
            self.assertNotIn("instructions", result)
            self.assertNotIn("background", result)
            if index < 2:
                self.assertNotIn("checkpoint_hint", result)
            else:
                self.assertEqual(result["checkpoint_hint"], {"checkpoint_version": 0, "checkpoint_due": True})
        self.chat.checkpoint("agent", [{"speaker": "you", "kind": "constraint", "text": "保留限制", "sources": mids}], 0)
        empty = with_local_hint(self.chat, "agent", "cli", self.chat.receive("agent"))
        self.assertNotIn("checkpoint_hint", empty)

    def test_attention_and_recovery_remain_visible(self):
        mid = self.send()["message_id"]
        batch = self.chat.receive("agent")
        with patch("chat_background.ChatBackground.followup", return_value={"background": {"state": "needs_attention", "error": "delivery uncertain"}}):
            result = self.chat.receive("agent")
        self.assertEqual(result["status"], "needs_review")
        self.assertEqual(result["background"]["error"], "delivery uncertain")
        self.assertIn("不自动重做", result["instructions"])
        self.assertEqual(self.receipts(batch)[mid], 0)

    def test_mcp_and_cli_combined_reply_entry_points(self):
        endpoint = ChatEndpoint(self.root)
        self.addCleanup(endpoint.close)
        endpoint.invoke("chat_join", {"room": "work", "name": "mcp", "protocol_version": 2, "client": "manual"})
        mids = [self.chat.send("you", str(i), to="mcp")["message_id"] for i in range(2)]
        batch = endpoint.invoke("chat_receive", {"room": "work"})
        result = endpoint.invoke("chat_send", {"room": "work", "content": "合并结果", "reply_to": mids[0], "delivery_id": batch["delivery_id"], "ack_ids": mids})
        self.assertFalse(result["pending"])
        self.assertEqual(result["acknowledged_count"], 2)
        mids = [self.send(str(i))["message_id"] for i in range(2)]
        batch = self.chat.receive("agent")
        command = [sys.executable, str(ROOT / "relay.py"), "--data-dir", str(self.root), "room", "send", "work", "--name", "agent", "--text", "合并结果", "--reply-to", mids[0], "--delivery-id", batch["delivery_id"], "--ack-ids", *mids]
        result = json.loads(subprocess.run(command, check=True, capture_output=True, text=True, timeout=10).stdout)
        self.assertFalse(result["pending"])
        self.assertEqual(result["acknowledged_count"], 2)
