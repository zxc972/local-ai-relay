"""v2 routing, precise receipts and private recovery, without AI calls."""
import contextlib
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from chatroom import ChatRoom
from chatroom_mcp import ChatEndpoint
from chat_context import LocalContext, CHECKPOINT_INTERVAL
from chat_delivery import BATCH_LIMIT, BATCH_BUDGET, units
from core import RelayError


class ContextTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="context 中文 ")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.chat = ChatRoom(self.root, "work")
        self.chat.create()
        self.chat.join("you", protocol_version=2, trusted_human=True)
        self.chat.join("codex", protocol_version=2)
        self.chat.join("claude", protocol_version=2)

    def say(self, text="请分析", **args):
        return self.chat.send("you", text, **args)

    def ack(self, batch, name="codex", message_ids=None):
        return self.chat.ack(name, delivery_id=batch["delivery_id"], message_ids=message_ids or [m["id"] for m in batch["messages"]])

    def entries(self, mid, text="只做设计，尚未执行", speaker="you", kind="constraint"):
        return [{"speaker": speaker, "kind": kind, "text": text, "sources": [mid]}]

    def queued(self, name):
        with self.chat.store.transaction() as db:
            return [dict(r) for r in self.chat.delivery.queued(db, self.chat.member(db, name, "cli"))]

    def test_seven_answers_do_not_trigger_42_peer_wakes_but_discussion_does(self):
        agents = ["codex", "claude"] + ["agent" + str(i) for i in range(5)]
        for name in agents[2:]:
            self.chat.join(name, protocol_version=2)
        sent = self.say("大家说一下看法")
        self.assertEqual(sum(len(self.queued(n)) for n in agents), 7)
        for name in agents:
            batch = self.chat.receive(name)
            self.assertEqual([m["id"] for m in batch["messages"]], [sent["message_id"]])
            result = self.chat.send(name, "我的独立观点", reply_to=sent["message_id"], delivery_id=batch["delivery_id"])
            self.assertTrue(result["acknowledged"])
        self.assertEqual(sum(len(self.queued(n)) for n in agents), 0)
        public = self.chat.context("codex")["messages"]
        self.assertEqual(len(public), 8)
        discussion = self.chat.send("codex", "请 claude 和 agent0 评审", notify=["claude", "agent0"])
        self.assertEqual(sum(len(self.queued(n)) for n in agents), 2)
        for name in ("claude", "agent0"):
            batch = self.chat.receive(name)
            self.assertEqual(batch["messages"][0]["id"], discussion["message_id"])
            self.chat.send(name, "我有不同看法", reply_to=discussion["message_id"], delivery_id=batch["delivery_id"])
        self.assertEqual(len(self.queued("codex")), 2)

    def test_unknown_legacy_sender_keeps_delivery_and_v1_ignores_suppression(self):
        self.chat.join("legacy")
        unknown = self.chat.send("legacy", "旧版客户端")
        self.assertEqual(self.queued("codex")[0]["id"], unknown["message_id"])
        statement = self.chat.send("codex", "公开进展，不要求别人处理")
        self.assertNotIn(statement["message_id"], [r["id"] for r in self.queued("claude")])
        self.assertEqual(self.chat.receive("legacy")["messages"][0]["id"], statement["message_id"])

    def test_batch_partial_receipts_new_arrivals_and_idempotent_reply(self):
        sent = [self.say(str(i), to="codex") for i in range(20)]
        batch = self.chat.receive("codex")
        self.assertEqual([m["id"] for m in batch["messages"]], [m["message_id"] for m in sent[:16]])
        later = self.say("收件之后的新请求", to="codex")
        with self.assertRaises(RelayError):
            self.ack(batch, message_ids=[sent[0]["message_id"], later["message_id"]])
        self.assertEqual(self.chat.context_info("codex")["processed_units"], 0)
        reply = self.chat.send("codex", "第一条结果", reply_to=sent[0]["message_id"], delivery_id=batch["delivery_id"], key="result")
        self.assertTrue(reply["acknowledged"])
        self.assertTrue(self.chat.send("codex", "第一条结果", reply_to=sent[0]["message_id"], delivery_id=batch["delivery_id"], key="result")["duplicate"])
        recovery = self.chat.receive("codex")
        self.assertEqual(recovery["status"], "needs_review")
        self.assertEqual(recovery["delivery_id"], batch["delivery_id"])
        self.assertEqual(len(recovery["pending_message_ids"]), 15)
        self.ack(batch)
        next_batch = self.chat.receive("codex")
        self.assertEqual([m["id"] for m in next_batch["messages"]], [m["message_id"] for m in sent[16:]] + [later["message_id"]])
        with self.assertRaises(RelayError):
            self.chat.wait("codex", after_seq=later["seq"], timeout=0)

    def test_invalid_reply_batch_rolls_back_send_and_receipt(self):
        sent = self.say(to="codex")
        batch = self.chat.receive("codex")
        before = self.chat.status()["latest_seq"]
        with self.assertRaises(RelayError):
            self.chat.send("codex", "不能提交", reply_to=sent["message_id"], delivery_id="wrong")
        self.assertEqual(self.chat.status()["latest_seq"], before)
        self.assertEqual(self.chat.receive("codex")["delivery_id"], batch["delivery_id"])

    def test_large_message_requires_contiguous_read_and_explicit_receipt(self):
        text = "约束🧠：先看完整需求。" * 1500
        sent = self.say(text, to="codex")
        batch = self.chat.receive("codex")
        page = batch["messages"][0]
        self.assertFalse(page["complete"])
        self.assertLessEqual(units({k: batch[k] for k in ("status", "delivery_id", "messages", "budget")}), BATCH_BUDGET)
        with self.assertRaises(RelayError):
            self.ack(batch)
        # Skipping to the final page cannot make the unread middle count as read.
        self.chat.context("codex", message_id=sent["message_id"], delivery_id=batch["delivery_id"], offset=len(text)-10)
        with self.assertRaises(RelayError):
            self.ack(batch)
        reconstructed = page["content"]
        while not page["complete"]:
            result = self.chat.context("codex", message_id=sent["message_id"], delivery_id=batch["delivery_id"], offset=page["next_offset"], budget=4000)
            self.assertLessEqual(units(result), 4000)
            page = result["messages"][0]
            reconstructed += page["content"]
        self.assertEqual(reconstructed, text)
        self.assertTrue(self.chat.context_info("codex")["pending"])
        self.ack(batch)
        self.assertEqual(self.chat.context_info("codex")["processed_units"], len(text.encode()))

    def test_checkpoint_is_private_atomic_versioned_and_does_not_ack(self):
        sent = self.say("只做设计，不执行", to="codex")
        batch = self.chat.receive("codex")
        result = self.chat.checkpoint("codex", self.entries(sent["message_id"]), 0)
        self.assertEqual(result["version"], 1)
        self.assertTrue(self.chat.context_info("codex")["pending"])
        with self.assertRaises(RelayError):
            self.chat.checkpoint("codex", self.entries(sent["message_id"]), 0)
        with self.assertRaises(RelayError):
            self.chat.checkpoint("claude", self.entries(sent["message_id"]), 0)
        self.assertIsNone(self.chat.context("claude")["checkpoint"])
        saved = self.chat.context("codex")["checkpoint"]
        context = LocalContext.for_room(self.chat, "codex", "cli")
        with context.store.transaction() as db:
            db.execute("CREATE TRIGGER fail_checkpoint BEFORE INSERT ON checkpoints BEGIN SELECT RAISE(ABORT,'disk unavailable'); END")
        with self.assertRaises(sqlite3.DatabaseError):
            context.save(self.entries(sent["message_id"], "更新失败"), 1)
        self.assertEqual(self.chat.context("codex")["checkpoint"], saved)
        self.assertEqual(self.chat.receive("codex")["delivery_id"], batch["delivery_id"])
        with context.store.transaction() as db:
            db.execute("DROP TRIGGER fail_checkpoint")
        for version in range(1, 4):
            context.save(self.entries(sent["message_id"], "保留用户明确约束，尚未执行"), version)
        with contextlib.closing(context.store.connect()) as db:
            self.assertEqual([r[0] for r in db.execute("SELECT version FROM checkpoints ORDER BY version")], [3, 4])
        self.assertIn("约束", self.chat.context("codex")["checkpoint"]["entries"][0]["text"])

    def test_checkpoint_hint_uses_acknowledged_content_and_ignores_history(self):
        text = "a" * 2100
        sent = [self.say(text, to="codex") for _ in range(3)]
        self.chat.context("codex")
        self.assertFalse(LocalContext.for_room(self.chat, "codex", "cli").hint()["checkpoint_due"])
        for _ in range(3):
            self.ack(self.chat.receive("codex"))
        self.assertGreaterEqual(self.chat.context_info("codex")["processed_units"], CHECKPOINT_INTERVAL)
        self.assertTrue(LocalContext.for_room(self.chat, "codex", "cli").hint()["checkpoint_due"])
        self.chat.checkpoint("codex", self.entries(sent[0]["message_id"]), 0)
        self.assertFalse(LocalContext.for_room(self.chat, "codex", "cli").hint()["checkpoint_due"])

    def test_independent_views_remain_distinct_and_raw_source_is_recoverable(self):
        question = self.say("方案未定")
        a = self.chat.send("codex", "我倾向方案 A")
        b = self.chat.send("claude", "我倾向方案 B")
        for name, preferred in (("codex", "A"), ("claude", "B")):
            entries = self.entries(question["message_id"], "尚无共同决定", kind="pending")
            entries += self.entries(a["message_id"], "支持 A", speaker="codex", kind="claim")
            entries += self.entries(b["message_id"], "支持 B", speaker="claude", kind="disagreement")
            entries += self.entries(question["message_id"], "本人偏好 " + preferred, speaker=name, kind="claim")
            self.chat.checkpoint(name, entries, 0)
        reopened = ChatRoom(self.root, "work")
        self.assertNotEqual(reopened.context("codex")["checkpoint"], reopened.context("claude")["checkpoint"])
        self.assertEqual(reopened.context("claude", message_id=a["message_id"])["messages"][0]["content"], "我倾向方案 A")

    def test_new_owner_same_nickname_cannot_inherit_private_context_or_receipts(self):
        sent = self.say("秘密约束", to="codex")
        batch = self.chat.receive("codex")
        self.chat.checkpoint("codex", self.entries(sent["message_id"]), 0)
        identity = self.chat.context_info("codex")
        self.chat.leave("codex")
        self.chat.join("codex", owner="different", protocol_version=2)
        self.assertNotEqual(self.chat.context_info("codex", "different")["member_id"], identity["member_id"])
        self.assertIsNone(self.chat.context("codex", "different")["checkpoint"])
        for args in ({"message_id": sent["message_id"]}, {"delivery_id": batch["delivery_id"]}):
            with self.assertRaises(RelayError):
                self.chat.context("codex", "different", **args)
        with self.assertRaises(RelayError):
            self.chat.ack("codex", owner="different", delivery_id=batch["delivery_id"], message_ids=[sent["message_id"]])
        # Downgrading cannot reveal the prior identity's private messages either.
        self.chat.join("codex", owner="different", protocol_version=1)
        self.assertEqual(self.chat.wait("codex", after_seq=0, owner="different", timeout=0, replay=True)["messages"], [])
        with self.assertRaises(RelayError):
            self.chat.send("you", "不能把旧私聊转给新同名成员", reply_to=sent["message_id"])

    def test_transport_coalescing_is_bounded_suppressed_messages_not_acknowledged(self):
        bg = self.chat.background
        bg.join("codex", client="claude", protocol_version=2, env={})
        generation = bg.reserve("codex", "cli", "host-task")
        self.assertEqual(bg.step("codex", generation)[0], "waiting")
        self.say("一", to="codex")
        self.say("二", to="codex")
        self.assertEqual(bg.step("codex", generation)[0], "coalescing")
        with patch("chat_background.time.time", return_value=time.time() + 1.1):
            self.assertEqual(bg.step("codex", generation)[0], "deliver")
        batch = self.chat.receive("codex")
        self.assertEqual(len(batch["messages"]), 2)
        self.ack(batch)
        self.chat.send("claude", "公开答案无需唤醒")
        before = self.chat.context_info("codex")["processed_units"]
        self.assertEqual(bg.step("codex", generation)[0], "waiting")
        self.assertEqual(self.chat.context_info("codex")["processed_units"], before)
        with self.chat.store.transaction() as db:
            member = self.chat.member(db, "codex", "cli")
            self.assertGreater(member["notify_seq"], member["confirmed_seq"])
        self.chat.leave("codex")

    def test_protocol_upgrade_refuses_unconfirmed_v1_delivery(self):
        self.chat.join("old")
        self.say(to="old")
        self.chat.receive("old")
        with self.assertRaises(RelayError):
            self.chat.join("old", protocol_version=2)

    def test_transport_protocol_upgrade_refuses_existing_worker_even_without_pending_messages(self):
        bg = self.chat.background
        bg.join("old", client="claude", env={})
        generation = bg.reserve("old", "cli", "host-task")
        bg.step("old", generation)
        with self.assertRaises(RelayError):
            self.chat.join("old", protocol_version=2)
        self.chat.leave("old")

    def test_idempotency_survives_protocol_upgrade_and_target_leaving(self):
        self.chat.join("old")
        sent = self.chat.send("old", "旧版定向消息", to="codex", key="old-key")
        self.chat.join("old", protocol_version=2)
        self.assertEqual(self.chat.send("old", "旧版定向消息", to="codex", key="old-key")["message_id"], sent["message_id"])
        discussion = self.chat.send("claude", "共同讨论", notify=["codex"], key="discuss")
        self.chat.leave("codex")
        self.assertTrue(self.chat.send("claude", "共同讨论", notify=["codex"], key="discuss")["duplicate"])
        with self.assertRaises(RelayError):
            self.chat.send("claude", "共同讨论", notify=["you"], key="discuss")

    def test_old_process_writes_get_identity_and_preserve_v2_visibility(self):
        # Simulate an already-running v1 process inserting its old column set.
        with self.chat.store.transaction() as db:
            db.execute("INSERT INTO chat_members(name,owner,active,joined_at,joined_seq) VALUES('old','one',1,0,0)")
            db.execute("INSERT INTO chat_messages(id,sender,recipient,content,created_at) VALUES('legacy-write','old','codex','旧进程发来',0)")
        self.assertEqual(self.chat.receive("codex")["messages"][0]["id"], "legacy-write")
        self.assertEqual(self.chat.receive("claude")["messages"], [])
        sent = self.chat.send("you", "给旧身份的私聊", to="old")
        with self.chat.store.transaction() as db:
            db.execute("UPDATE chat_members SET owner='two' WHERE name='old'")
        with self.assertRaises(RelayError):
            self.chat.delivery.context("old", "two", message_id=sent["message_id"])

    def test_ten_thousand_history_records_bounded_and_exact_lookup(self):
        # Insert durable public fixtures in one transaction, never invoke a host.
        with self.chat.store.transaction() as db:
            member = self.chat.member(db, "you", "cli")
            db.executemany("INSERT INTO chat_messages(id,sender,sender_member_id,content,created_at) VALUES(?,'you',?,?,?)",
                           [(f"fixture-{i}", member["member_id"], "中文约束与证据" * 40, i) for i in range(10000)])
        recovered = self.chat.context("codex")
        self.assertLessEqual(units(recovered), 8000)
        self.assertLess(len(recovered["messages"]), 32)
        self.assertTrue(recovered["has_more"])
        self.assertEqual(self.chat.context("codex", message_id="fixture-0")["messages"][0]["content"], "中文约束与证据" * 40)
        self.assertEqual(self.chat.context_info("codex")["processed_units"], 0)

    def test_mcp_v2_schema_private_checkpoint_and_concise_results(self):
        endpoint = ChatEndpoint(self.root)
        self.addCleanup(endpoint.close)
        endpoint.invoke("chat_join", {"room": "work", "name": "mcp", "protocol_version": 2, "client": "manual"})
        sent = self.say("mcp 的约束", to="mcp")
        batch = endpoint.invoke("chat_receive", {"room": "work"})
        self.assertNotIn("这是 AI 聊天室", batch["instructions"])
        endpoint.invoke("chat_checkpoint", {"room": "work", "entries": self.entries(sent["message_id"]), "expected_version": 0})
        endpoint.invoke("chat_ack", {"room": "work", "delivery_id": batch["delivery_id"], "message_ids": [sent["message_id"]]})
        self.assertEqual(endpoint.invoke("chat_context", {"room": "work"})["checkpoint_version"], 1)
        with self.assertRaises(RelayError):
            endpoint.invoke("chat_send", {"room": "work", "content": "bad", "notify": "codex"})

    def test_optional_checkpoint_failure_does_not_hide_successful_receipt(self):
        endpoint = ChatEndpoint(self.root)
        self.addCleanup(endpoint.close)
        endpoint.invoke("chat_join", {"room": "work", "name": "mcp", "protocol_version": 2, "client": "manual"})
        sent = self.say("确认后摘要提示故障", to="mcp")
        batch = endpoint.invoke("chat_receive", {"room": "work"})
        with patch.object(LocalContext, "for_room", side_effect=OSError("disk full")):
            result = endpoint.invoke("chat_ack", {"room": "work", "delivery_id": batch["delivery_id"], "message_ids": [sent["message_id"]]})
        self.assertEqual(result["status"], "acknowledged")
        self.assertIn("context_warning", result)
        self.assertFalse(result["pending"])

    def test_cli_v2_complete_cycle(self):
        def run(*args):
            command = [sys.executable, str(ROOT / "relay.py"), "--data-dir", str(self.root), "room", *args]
            return json.loads(subprocess.run(command, check=True, capture_output=True, text=True, timeout=10).stdout)
        run("join", "work", "--name", "terminal", "--client", "manual", "--protocol", "2")
        sent = self.say("CLI 测试", to="terminal")
        batch = run("receive", "work", "--name", "terminal")
        file = self.root / "checkpoint.json"
        file.write_text(json.dumps(self.entries(sent["message_id"])))
        run("checkpoint", "work", "--name", "terminal", "--file", str(file), "--expected-version", "0")
        run("ack", "work", "--name", "terminal", "--delivery-id", batch["delivery_id"], "--message-ids", sent["message_id"])
        self.assertEqual(run("context", "work", "--name", "terminal")["checkpoint_version"], 1)
