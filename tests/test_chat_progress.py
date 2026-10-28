"""Per-message receipts use actual delivery, never status polling or inference."""
import contextlib
import tempfile
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from chat_server import ChatService
from chatroom import ChatRoom
from core import RelayError


class ProgressTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="chat progress ")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.service = ChatService(self.root)
        self.chat = self.service.room("work", create=True)
        self.chat.join("you", protocol_version=2, trusted_human=True)
        self.chat.join("codex", protocol_version=2)
        self.chat.join("claude", protocol_version=2)

    def progress(self, mid, name="you"):
        return self.service.progress(self.chat, name, [mid])["messages"]

    def states(self, mid):
        return {row["name"]: row["state"] for row in self.progress(mid)[0]["recipients"]}

    def test_broadcast_shows_each_actual_receipt_and_reply_ack(self):
        sent = self.chat.send("you", "大家回复")
        mid = sent["message_id"]
        self.assertEqual(self.states(mid), {"claude": "sent", "codex": "sent"})
        packet = self.chat.receive("codex")
        self.assertEqual(self.states(mid), {"claude": "sent", "codex": "received"})
        reply = self.chat.send("codex", "已处理", reply_to=mid, delivery_id=packet["delivery_id"], key="answer")
        self.assertEqual(self.states(mid), {"claude": "sent", "codex": "acknowledged"})
        self.assertEqual(self.progress(reply["message_id"])[0]["recipients"], [])
        duplicate = self.chat.send("codex", "已处理", reply_to=mid, delivery_id=packet["delivery_id"], key="answer")
        self.assertTrue(duplicate["duplicate"])
        self.assertEqual(self.states(mid)["codex"], "acknowledged")

    def test_history_peek_and_restore_do_not_mark_a_message_received(self):
        sent = self.chat.send("you", "只给 codex", to="codex")
        mid = sent["message_id"]
        self.service.history(self.chat, "codex", 0)
        self.service.peek(self.chat, "codex", "cli", None, 0)
        self.chat.context("codex", message_id=mid)
        self.assertEqual(self.states(mid), {"codex": "sent"})
        for viewer in (None, "claude"):
            self.assertEqual(self.progress(mid, viewer), [])
        self.assertTrue(self.progress(mid, "codex"))

    def test_legacy_single_receive_and_batch_confirmation_are_recorded(self):
        self.chat.join("legacy")
        first = self.chat.send("you", "one", to="legacy")["message_id"]
        self.chat.receive("legacy")
        self.assertEqual(self.states(first), {"legacy": "received"})
        self.chat.ack("legacy", first)
        self.assertEqual(self.states(first), {"legacy": "acknowledged"})
        mids = [self.chat.send("you", text, to="legacy")["message_id"] for text in ("two", "three")]
        packet = self.chat.wait("legacy", timeout=0)
        self.assertEqual([self.states(mid)["legacy"] for mid in mids], ["received"] * 2)
        self.chat.wait("legacy", after_seq=packet["next_after_seq"], timeout=0)
        self.assertEqual([self.states(mid)["legacy"] for mid in mids], ["acknowledged"] * 2)

    def test_v2_batch_ack_updates_only_selected_messages_and_survives_restart(self):
        mids = [self.chat.send("you", text, to="codex")["message_id"] for text in ("one", "two")]
        packet = self.chat.receive("codex")
        self.chat.ack("codex", delivery_id=packet["delivery_id"], message_ids=[mids[0]])
        self.chat = ChatRoom(self.root, "work")
        self.assertEqual([self.states(mid)["codex"] for mid in mids], ["acknowledged", "received"])
        with self.assertRaises(RelayError):
            self.chat.send("codex", "bad", reply_to=mids[1], delivery_id=packet["delivery_id"], ack_ids=[mids[1], "missing"])
        self.assertEqual(self.states(mids[1])["codex"], "received")

    def test_later_join_or_reused_nickname_does_not_rewrite_original_recipients(self):
        mid = self.chat.send("you", "hello", to="codex")["message_id"]
        self.chat.leave("codex")
        self.chat.join("codex", owner="another-owner", protocol_version=2)
        self.chat.join("late", protocol_version=2)
        self.assertEqual(self.progress(mid, "codex"), [])
        self.assertEqual(self.states(mid), {"codex": "sent"})
        self.chat.receive("codex", owner="another-owner")
        self.assertEqual(self.states(mid), {"codex": "sent"})

    def test_notifications_only_target_requested_agents(self):
        mid = self.chat.send("codex", "请 claude 评审", notify=["claude"])["message_id"]
        self.assertEqual(self.progress(mid)[0]["recipients"], [{"name": "claude", "state": "sent"}])
        self.chat.join("late", protocol_version=2)
        self.assertEqual(self.progress(mid)[0]["recipients"], [{"name": "claude", "state": "sent"}])

    def test_preexisting_messages_are_explicitly_untracked(self):
        mid = self.chat.send("you", "old")["message_id"]
        with self.chat.store.transaction() as db:
            db.execute("UPDATE chat_messages SET progress_tracked=0 WHERE id=?", (mid,))
            db.execute("DELETE FROM chat_progress WHERE message_id=?", (mid,))
        self.chat = ChatRoom(self.root, "work")
        self.assertEqual(self.progress(mid)[0], {"message_id": mid, "tracked": False, "recipients": []})

    def test_snapshot_exposes_safe_recovery_metadata(self):
        self.chat.background.join("codex", client="manual", protocol_version=2)
        member = next(m for m in self.service.snapshot(self.chat)["members"] if m["name"] == "codex")
        self.assertEqual(member["connection"], "local")
        self.assertEqual(member["protocol_version"], 2)
        self.assertFalse(member["listening"])
        self.assertNotIn("owner", member)
        self.assertNotIn("adapter", member)


if __name__ == "__main__":
    unittest.main()
