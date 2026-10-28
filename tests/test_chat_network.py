"""Real HTTP, separate client data, MCP/CLI and background processes; no model calls."""
import concurrent.futures
import contextlib
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from chat_server import create_server
from chat_remote import RemoteChat, request
from chatroom import ChatRoom
from core import RelayError
from chat_safety import protect_network
from test_message_bus import Peer


class NetworkTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="network chat 中文 ")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.server = create_server(self.root / "server", port=0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = self.server.service.public_url
        self.admin = self.server.service.admin_token
        result = request(self.url, "/api/rooms", self.admin, {"action": "create", "room": "work"})
        self.invite = result["invite"]
        self.chat = self.server.service.room("work")
        self.clients, self.processes = [], []
        self.addCleanup(self.shutdown)

    def shutdown(self):
        for client in self.clients:
            try:
                client.leave()
            except RelayError:
                pass
        self.server.service.stopping.set()
        self.server.service.changed.set()
        for process in self.processes:
            if process.poll() is None:
                process.terminate()
            process.communicate(timeout=5)
        self.eventually(lambda: all(not c.occupied(c.row()) for c in self.clients), timeout=5)
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)

    def eventually(self, predicate, timeout=6):
        deadline = time.monotonic() + timeout
        while not predicate():
            if time.monotonic() >= deadline:
                self.fail("Expected network/background state did not arrive")
            time.sleep(.025)

    def client(self, name, **options):
        client = RemoteChat(self.root / ("client-" + name), self.url, "work", name)
        self.clients.append(client)
        client.join(self.invite, **({"client": "manual"} if not options else options))
        return client

    def host(self, exit_code=0):
        script = self.root / "fake_host.py"
        log = self.root / "wake.jsonl"
        script.write_text("import sys,json\nevent=json.load(sys.stdin)\n"
                          "with open(sys.argv[1],'a') as f:f.write(json.dumps(event)+'\\n')\n"
                          "sys.exit(int(sys.argv[2]))\n")
        return {"type": "command", "argv": [sys.executable, str(script), str(log), str(exit_code)], "timeout": 3}, log

    def events(self, log):
        return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []

    def test_v2_network_checkpoint_stays_local_and_receipts_are_exact(self):
        human = self.client("you", client="manual", protocol_version=2)
        # Human status is granted only by the authenticated room manager.
        request(self.url, "/api/rooms/work/humanjoin", self.admin,
                {"name": "you", "member_token": human.row()["token"], "protocol_version": 2})
        a = self.client("codex", client="manual", protocol_version=2)
        b = self.client("claude", client="manual", protocol_version=2)
        with self.assertRaises(RelayError):
            b.call("humanjoin", {"name": "claude", "member_token": b.row()["token"], "protocol_version": 2})
        with self.assertRaises(RelayError):
            b.call("send", {"content": "forge", "kind": "human"})
        first, second = human.send("只讨论，不执行", to="codex"), human.send("保存参考来源", to="codex")
        batch = a.receive()
        self.assertEqual(len(batch["messages"]), 2)
        unique = "仅在 Codex 设备上的个人判断，不应到服务器"
        entries = [{"speaker": "codex", "kind": "pending", "text": unique, "sources": [first["message_id"]]}]
        with patch.object(a, "call", wraps=a.call) as network:
            a.checkpoint(entries, 0)
            self.assertEqual({call.args[0] for call in network.call_args_list}, {"contextinfo", "sources"})
            self.assertNotIn(unique, repr(network.call_args_list))
        self.assertIsNone(b.context()["checkpoint"])
        self.assertEqual(b.context()["messages"], [])
        newer = human.send("稍后到达的第三条", to="codex")
        with self.assertRaises(RelayError):
            a.ack(delivery_id=batch["delivery_id"], message_ids=[first["message_id"], newer["message_id"]])
        self.assertEqual(a.call("contextinfo")["processed_units"], 0)
        a.send("第一条实际结果", reply_to=first["message_id"], delivery_id=batch["delivery_id"])
        self.assertEqual(a.receive()["status"], "needs_review")
        reopened = RemoteChat(a.base, self.url, "work", "codex")
        self.assertEqual(reopened.join(client="manual", protocol_version=2)["status"], "needs_review")
        review = reopened.context(delivery_id=batch["delivery_id"])
        self.assertEqual(review["acknowledged_ids"], [first["message_id"]])
        self.assertNotIn("checkpoint", review)
        self.assertEqual(reopened.context()["checkpoint"]["entries"][0]["text"], unique)
        reopened.ack(delivery_id=batch["delivery_id"], message_ids=[second["message_id"]])
        self.assertEqual(reopened.receive()["messages"][0]["id"], newer["message_id"])
        with self.assertRaises(RelayError):
            b.context(delivery_id=batch["delivery_id"])
        with self.assertRaises(RelayError):
            b.context(message_id=first["message_id"])
        with self.assertRaises(RelayError):
            a.call("checkpoint", {"entries": entries})

    def test_transport_v2_callback_batches_and_manual_receipts(self):
        config, log = self.host()
        writer = self.client("writer", client="manual", protocol_version=2)
        codex = self.client("codex", wake_config=config, protocol_version=2)
        claude = self.client("claude", client="manual", protocol_version=2)
        sent = [writer.send(str(i), to="codex") for i in range(3)]
        self.eventually(lambda: len(self.events(log)) == 1)
        batch = codex.receive()
        self.assertEqual([m["id"] for m in batch["messages"]], [m["message_id"] for m in sent])
        queued_while_busy = writer.send("上一批处理期间的新消息", to="codex")
        time.sleep(.4)
        self.assertEqual(len(self.events(log)), 1)
        codex.ack(delivery_id=batch["delivery_id"], message_ids=[m["id"] for m in batch["messages"]])
        self.eventually(lambda: len(self.events(log)) == 2, timeout=4)
        self.assertEqual(self.events(log)[1]["message_id"], queued_while_busy["message_id"])
        next_batch = codex.receive()
        codex.ack(delivery_id=next_batch["delivery_id"], message_ids=[queued_while_busy["message_id"]])
        # Human-directed answers are visible but do not wake another v2 agent.
        c_messages = [writer.send("待处理" + str(i), to="claude") for i in range(2)]
        batch = claude.receive()
        self.assertEqual(len(batch["messages"]), 2)
        first = claude.send("处理一", reply_to=c_messages[0]["message_id"], delivery_id=batch["delivery_id"])
        self.assertNotIn("background_task", first)
        last = claude.ack(delivery_id=batch["delivery_id"], message_ids=[c_messages[1]["message_id"]])
        self.assertNotIn("background_task", last)
        # A later distinct batch still gets a fresh wake, with no idle model calls.
        next_message = writer.send("第二批", to="codex")
        self.eventually(lambda: len(self.events(log)) == 3)
        self.assertEqual(self.events(log)[2]["message_id"], next_message["message_id"])
        time.sleep(.4)
        self.assertEqual(len(self.events(log)), 3)

    def test_v2_http_large_private_message_read_budget_and_no_skip_ack(self):
        from chat_delivery import units
        a = self.client("a", client="manual", protocol_version=2)
        b = self.client("b", client="manual", protocol_version=2)
        content = "长消息中的关键条件" * 1000
        sent = a.send(content, to="b")
        batch = b.receive()
        page, whole = batch["messages"][0], batch["messages"][0]["content"]
        with self.assertRaises(RelayError):
            b.ack(delivery_id=batch["delivery_id"], message_ids=[sent["message_id"]])
        while not page["complete"]:
            packet = b.context(message_id=sent["message_id"], delivery_id=batch["delivery_id"], offset=page["next_offset"], budget=4000)
            self.assertLessEqual(units(packet), 4000)
            page = packet["messages"][0]
            whole += page["content"]
        self.assertEqual(whole, content)
        b.ack(delivery_id=batch["delivery_id"], message_ids=[sent["message_id"]])
        self.assertFalse(b.call("contextinfo")["pending"])

    def test_v2_stdio_to_http_checkpoint_and_precise_receipt(self):
        peer = Peer(self.root / "mcp-v2", cli=True)
        self.addCleanup(lambda: peer.close() if not peer.process.stderr.closed else None)
        peer.tool("chat_join", room="work", name="mcp-v2", server=self.url, invite=self.invite, client="manual", protocol_version=2)
        writer = self.client("writer", client="manual", protocol_version=2)
        sent = writer.send("跨进程约束", to="mcp-v2")
        extra = writer.send("补充约束，一起处理", to="mcp-v2")
        batch = peer.tool("chat_receive", room="work", server=self.url)
        peer.tool("chat_checkpoint", room="work", server=self.url, expected_version=0,
                  entries=[{"speaker": "writer", "kind": "constraint", "text": "保留这条来源", "sources": [sent["message_id"]]}])
        lookup = peer.tool("chat_context", room="work", server=self.url, message_id=sent["message_id"])
        self.assertNotIn("checkpoint", lookup)
        result = peer.tool("chat_send", room="work", server=self.url, content="已处理这两条约束", reply_to=sent["message_id"],
                           delivery_id=batch["delivery_id"], ack_ids=[sent["message_id"], extra["message_id"]])
        self.assertFalse(result["pending"])
        self.assertEqual(result["acknowledged_count"], 2)
        restored = peer.tool("chat_context", room="work", server=self.url)
        self.assertEqual(restored["checkpoint_version"], 1)
        self.assertEqual(restored["host_context"]["usage"], "unknown")

    def test_v2_combined_cli_http_reply_is_atomic_private_and_stays_manual(self):
        writer = self.client("writer", client="manual", protocol_version=2)
        other = self.client("other", client="manual", protocol_version=2)
        agent = self.client("agent", client="manual", protocol_version=2)
        mids = [writer.send(str(i), to="agent")["message_id"] for i in range(3)]
        batch = agent.receive()
        later = writer.send("later", to="agent")["message_id"]
        before = agent.call("state")["latest_seq"]
        with self.assertRaises(RelayError):
            agent.send("invalid", reply_to=mids[0], delivery_id=batch["delivery_id"], ack_ids=[mids[0], later])
        self.assertEqual(agent.call("state")["latest_seq"], before)
        self.assertEqual(agent.call("contextinfo")["processed_units"], 0)
        command = agent.argv("send", "--text", "合并前两条", "--reply-to", mids[0], "--delivery-id", batch["delivery_id"], "--ack-ids", *mids[:2])
        result = json.loads(subprocess.run(command, check=True, capture_output=True, text=True, timeout=10).stdout)
        self.assertEqual(result["acknowledged_count"], 2)
        self.assertTrue(result["pending"])
        self.assertNotIn("background_task", result)
        self.assertEqual(other.call("history")["messages"], [])
        result = agent.send("完成第三条", reply_to=mids[2], delivery_id=batch["delivery_id"], ack_ids=mids[2:], key="last")
        self.assertFalse(result["pending"])
        self.assertNotIn("background_task", result)
        self.assertNotIn("instructions", result)
        self.assertNotIn("checkpoint_hint", result)
        self.assertTrue(agent.send("完成第三条", reply_to=mids[2], delivery_id=batch["delivery_id"], ack_ids=mids[2:], key="last")["duplicate"])
        self.assertEqual(agent.receive()["messages"][0]["id"], later)

    def test_browser_human_registration_preserves_seat_and_never_rejoins(self):
        human = self.client("you")
        receiver = self.client("agent", client="manual", protocol_version=2)
        before = self.chat.context_info("you", owner=None)
        with self.assertRaises(RelayError):
            request(self.url, "/api/rooms/work/human", self.invite, {"member_token": human.row()["token"]})
        marked = request(self.url, "/api/rooms/work/human", self.admin, {"member_token": human.row()["token"]})
        self.assertEqual(marked["status"], "human_registered")
        self.assertEqual(self.chat.context_info("you", owner=None)["member_id"], before["member_id"])
        sent = human.send("我是已在页面中的人类")
        batch = receiver.receive()
        receiver.send("面向人类的回复", reply_to=sent["message_id"], delivery_id=batch["delivery_id"])
        human.leave()
        with self.assertRaises(RelayError):
            request(self.url, "/api/rooms/work/human", self.admin, {"member_token": human.row()["token"]})
        self.assertFalse(next(m for m in self.chat.status()["members"] if m["name"] == "you")["joined"])

    def test_atomic_eight_seats_across_local_and_concurrent_network_joins(self):
        for i in range(4):
            self.chat.join("local-" + str(i))
        def join(i):
            try:
                return self.client("net-" + str(i))
            except RelayError as error:
                self.assertEqual(error.status, 409)
                return None
        with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
            joined = [c for c in pool.map(join, range(12)) if c]
        self.assertEqual(len(joined), 4)
        self.assertEqual(self.chat.status()["member_count"], 8)
        self.assertEqual(joined[0].join(client="manual")["status"], "joined")
        self.chat.leave("local-0")
        newcomer = self.client("replacement")
        self.assertEqual(newcomer.call("state")["member_count"], 8)
        with self.assertRaises(RelayError):
            self.chat.join("ninth-local")

    def test_credentials_isolation_private_messages_and_idempotent_reply(self):
        a, b, c = self.client("a"), self.client("b"), self.client("c")
        with self.assertRaises(RelayError):
            request(self.url, "/api/rooms/work/send", self.invite, {"content": "forged"})
        with self.assertRaises(RelayError):
            a.call("send", {"name": "b", "content": "forged"})
        sent = a.send("只给 b", to="b", key="stable")
        self.assertTrue(a.send("只给 b", to="b", key="stable")["duplicate"])
        self.assertEqual(c.receive()["messages"], [])
        self.assertEqual(c.call("history")["messages"], [])
        self.assertEqual(request(self.url, "/api/rooms/work/history", self.admin, {})["messages"], [])
        with self.assertRaises(RelayError):
            c.send("steal", reply_to=sent["message_id"])
        self.assertEqual(b.receive()["messages"][0]["content"], "只给 b")
        reply = b.send("收到", reply_to=sent["message_id"], key="reply")
        self.assertTrue(reply["acknowledged"])
        self.assertEqual(reply["to"], "a")
        b.leave()
        impostor = RemoteChat(self.root / "impostor", self.url, "work", "b")
        with self.assertRaises(RelayError):
            impostor.join(self.invite, client="manual")
        other = request(self.url, "/api/rooms", self.admin, {"action": "create", "room": "other"})
        self.assertNotEqual(other["invite"], self.invite)
        with self.assertRaises(RelayError):
            request(self.url, "/api/rooms/other/state", a.row()["token"], {})
        snapshot = json.dumps(a.call("state"))
        for private in (str(self.root), "target_session", a.row()["token"], self.invite):
            self.assertNotIn(private, snapshot)

    def test_reconnect_requires_review_and_ack_does_not_repeat_old_work(self):
        a, b = self.client("a"), self.client("b")
        sent = a.send("只执行一次")
        b.receive()
        reopened = RemoteChat(b.base, self.url, "work", "b")
        self.assertEqual(reopened.join(client="manual")["status"], "needs_review")
        self.assertEqual(reopened.receive()["messages"], [])
        self.assertEqual(reopened.review()["messages"][0]["id"], sent["message_id"])
        reopened.ack(sent["message_id"])
        self.assertEqual(reopened.join(client="manual")["status"], "joined")
        self.assertEqual(reopened.receive()["status"], "empty")

    def test_at_mention_is_private_through_the_http_api_and_history(self):
        human, codex, claude = self.client("you"), self.client("codex"), self.client("claude-desktop")
        sent = human.send("@codex 只有你能收到", key="private-mention")
        self.assertEqual(sent["to"], "codex")
        self.assertEqual(claude.receive()["messages"], [])
        self.assertEqual(claude.call("history")["messages"], [])
        self.assertIsNone(claude.call("peek", {"wait": 0})["message"])
        self.assertEqual(codex.receive()["messages"][0]["id"], sent["message_id"])
        reply = codex.send("只有你能看到回复", reply_to=sent["message_id"])
        self.assertEqual(reply["to"], "you")
        self.assertEqual(claude.call("history")["messages"], [])
        self.assertEqual(len(human.call("history")["messages"]), 2)
        with self.assertRaises(RelayError):
            human.send("@does-not-exist 不可群发")

    def test_http_progress_is_private_and_tracks_receive_ack_without_model(self):
        sender, reader, observer = self.client("sender"), self.client("reader"), self.client("observer")
        mid = sender.send("private", to="reader")["message_id"]
        data = {"message_ids": [mid]}
        self.assertEqual(sender.call("progress", data)["messages"][0]["recipients"][0]["state"], "sent")
        for token in (observer.row()["token"], self.admin, self.invite):
            self.assertEqual(request(self.url, "/api/rooms/work/progress", token, data)["messages"], [])
        reader.receive()
        self.assertEqual(sender.call("progress", data)["messages"][0]["recipients"][0]["state"], "received")
        reader.ack(mid)
        self.assertEqual(sender.call("progress", data)["messages"][0]["recipients"][0]["state"], "acknowledged")
        with self.assertRaises(RelayError):
            sender.call("progress", {"message_ids": [mid] * 101})

    def test_large_history_pages_stay_readable_without_skipping_messages(self):
        writer, reader, observer = self.client("writer"), self.client("reader"), self.client("observer")
        expected = [writer.send("x" * 100_000, to="reader")["message_id"] for _ in range(21)]
        received, cursor = [], 0
        for _ in range(25):
            packet = reader.call("history", {"after": cursor})
            self.assertLess(len(json.dumps(packet, ensure_ascii=False).encode()), 1_100_000)
            received.extend(message["id"] for message in packet["messages"])
            cursor = packet["cursor"]
            if len(received) == len(expected):
                break
        self.assertEqual(received, expected)
        self.assertEqual(observer.call("history")["messages"], [])

    def test_maximum_chinese_message_uses_utf8_on_the_wire(self):
        writer, reader = self.client("writer"), self.client("reader")
        content = "汉" * 100_000
        sent = writer.send(content, to="reader")
        message = reader.receive()["messages"][0]
        self.assertEqual(message["id"], sent["message_id"])
        self.assertEqual(message["content"], content)

    def test_transport_remote_busy_host_defers_then_resumes_without_a_new_join(self):
        config, _ = self.host()
        writer = self.client("writer")
        with patch.object(RemoteChat, "start"):
            reader = self.client("reader", wake_config=config)
        generation = reader.reserve("callback", expected_adapter=json.dumps(config))
        stopped, ready = threading.Event(), threading.Event()
        accepted = []
        def deliver(config, event):
            if not ready.is_set():
                return {"accepted": False, "status": "busy", "retry_safe": True}
            ready.clear(); accepted.append(event["message_id"])
            return {"accepted": True}
        with patch("chat_remote.BUSY_RETRY_DELAY", .15), patch("adapters.deliver", side_effect=deliver):
            worker = threading.Thread(target=reader.run, args=(generation,), kwargs={"stopped": stopped})
            worker.start()
            try:
                for text in ("第一条", "第二条"):
                    sent = writer.send(text, to="reader")
                    self.eventually(lambda: reader.row()["state"] == "waiting_for_turn")
                    self.assertTrue(reader.occupied(reader.row()))
                    ready.set()
                    self.eventually(lambda: sent["message_id"] in accepted)
                    self.assertEqual(reader.receive()["messages"][0]["id"], sent["message_id"])
                    reader.send("回复", reply_to=sent["message_id"])
                self.assertEqual(len(accepted), 2)
            finally:
                stopped.set(); reader.leave(); worker.join(5)
            self.assertFalse(worker.is_alive())

    def test_transport_remote_background_idle_zero_calls_then_sequential_delivery(self):
        config, log = self.host()
        a = self.client("writer")
        b = self.client("reader", wake_config=config)
        self.assertTrue(b.followup()["background"]["running"])
        time.sleep(.4)
        self.assertEqual(self.events(log), [])
        first = a.send("first", to="reader")
        self.eventually(lambda: len(self.events(log)) == 1)
        event = self.events(log)[0]
        self.assertNotIn("content", event)
        self.assertEqual(event["receive_tool"]["arguments"]["server"], self.url)
        received = subprocess.run(event["receive_argv"], capture_output=True, text=True, timeout=5)
        self.assertEqual(received.returncode, 0, received.stderr)
        self.assertEqual(json.loads(received.stdout)["messages"][0]["id"], first["message_id"])
        second = a.send("second", to="reader")
        time.sleep(.4)
        self.assertEqual(len(self.events(log)), 1)
        b.ack(first["message_id"])
        self.eventually(lambda: len(self.events(log)) == 2)
        self.assertEqual(self.events(log)[1]["message_id"], second["message_id"])
        b.receive(); b.ack(second["message_id"])
        b.leave()
        a.send("after leave", to="reader")
        self.eventually(lambda: not b.occupied(b.row()))
        self.assertEqual(len(self.events(log)), 2)

    def test_transport_failed_host_is_not_retried_and_explicit_recovery_works(self):
        config, log = self.host(9)
        a = self.client("writer")
        b = self.client("reader", wake_config=config)
        sent = a.send("one")
        self.eventually(lambda: b.row()["state"] == "needs_attention" and not b.occupied(b.row()))
        result = b.join(wake_config=config)
        self.assertEqual(result["background"]["state"], "needs_attention")
        time.sleep(.25)
        self.assertEqual(len(self.events(log)), 1)
        self.assertEqual(b.receive()["messages"][0]["id"], sent["message_id"])
        b.ack(sent["message_id"])
        self.assertEqual(b.join(client="manual")["status"], "joined")

    def test_protected_network_watch_is_rejected_before_any_notification(self):
        protect_network(self.server.service.store.root)
        a, b = self.client("a"), self.client("claude", client="claude")
        self.assertNotIn("background_task", b.followup())
        process = subprocess.run(b.argv("watch"), capture_output=True, text=True, timeout=5)
        self.assertEqual(process.returncode, 2)
        self.assertIn("手动收件", process.stdout)
        sent = a.send("你好", to="claude")
        b.receive()
        reply = b.send("你好", reply_to=sent["message_id"])
        self.assertTrue(reply["acknowledged"])
        self.assertNotIn("background_task", reply)

    def test_independent_mcp_clients_use_network_room_and_eof_leaves(self):
        peers = [Peer(self.root / ("mcp-" + str(i)), cli=True) for i in range(2)]
        for peer in peers:
            self.addCleanup(lambda p=peer: p.close() if not p.process.stderr.closed else None)
        for i, peer in enumerate(peers):
            peer.tool("chat_join", room="work", name="mcp-" + str(i), server=self.url, invite=self.invite, client="manual")
        sent = peers[0].tool("chat_send", room="work", server=self.url, content="跨客户端消息")
        packet = peers[1].tool("chat_receive", room="work", server=self.url)
        self.assertEqual(packet["messages"][0]["id"], sent["message_id"])
        peers[1].tool("chat_ack", room="work", server=self.url, message_id=sent["message_id"])
        peers[0].close(); peers[1].close()
        self.assertEqual(self.chat.status()["member_count"], 0)

    def test_bad_origin_host_json_and_unauthorized_requests_are_rejected(self):
        cases = [({"Origin": "https://evil.example"}, b"{}", 403),
                 ({"Host": "evil.example"}, b"{}", 403),
                 ({}, b"[]", 400), ({}, b"{", 400),
                 ({"Authorization": "Bearer invalid"}, b"{}", 401)]
        for headers, body, code in cases:
            req = Request(self.url + "/api/rooms", data=body, headers={"Content-Type": "application/json", "Authorization": "Bearer " + self.admin, **headers})
            with self.assertRaises(HTTPError) as failed:
                urlopen(req, timeout=3)
            self.assertEqual(failed.exception.code, code)
            failed.exception.close()
        with urlopen(self.url + "/") as page:
            self.assertIn("AI 聊天室", page.read().decode())
            self.assertIn("frame-ancestors 'none'", page.headers["Content-Security-Policy"])
        with self.assertRaises(RelayError):
            create_server(self.root / "bad-bind", host="0.0.0.0", port=0)

    def test_local_page_enters_without_a_key_but_cross_site_requests_cannot(self):
        req = Request(self.url + "/api/local-session", data=b"{}",
                      headers={"Content-Type": "application/json", "Origin": self.url, "Sec-Fetch-Site": "same-origin"})
        with urlopen(req, timeout=3) as response:
            token = json.load(response)["admin_token"]
            self.assertEqual(response.headers["Cache-Control"], "no-store")
        created = request(self.url, "/api/rooms", token, {"action": "create", "room": "no-key-needed"})
        self.assertEqual(created["room"], "no-key-needed")
        for headers in ({}, {"Origin": "https://evil.example"},
                        {"Origin": self.url, "Sec-Fetch-Site": "cross-site"},
                        {"Origin": self.url, "X-Forwarded-For": "192.0.2.1"}):
            req = Request(self.url + "/api/local-session", data=b"{}",
                          headers={"Content-Type": "application/json", **headers})
            with self.assertRaises(HTTPError) as failed:
                urlopen(req, timeout=3)
            self.assertEqual(failed.exception.code, 403)
            failed.exception.close()

    def test_proxy_deployment_does_not_automatically_grant_owner_access(self):
        server = create_server(self.root / "proxy-server", port=0, public_url="https://chat.example.com")
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            url = f"http://127.0.0.1:{server.server_address[1]}"
            req = Request(url + "/api/local-session", data=b"{}",
                          headers={"Content-Type": "application/json", "Origin": url, "Sec-Fetch-Site": "same-origin"})
            with self.assertRaises(HTTPError) as failed:
                urlopen(req, timeout=3)
            self.assertEqual(failed.exception.code, 403)
            failed.exception.close()
        finally:
            server.shutdown(); server.server_close(); thread.join(5)

    def test_transport_admin_removal_releases_seat_and_stops_remote_watcher(self):
        config, log = self.host()
        b = self.client("reader", wake_config=config)
        result = request(self.url, "/api/rooms/work/remove", self.admin, {"name": "reader"})
        self.assertEqual(result["status"], "left")
        self.eventually(lambda: not b.occupied(b.row()))
        self.assertEqual(self.chat.status()["member_count"], 0)
        self.assertEqual(self.events(log), [])

    def test_transport_server_restart_preserves_identity_and_background_recovers_without_model_polling(self):
        config, log = self.host()
        a = self.client("writer")
        b = self.client("reader", wake_config=config)
        port = self.server.server_address[1]
        self.server.service.stopping.set()
        self.server.service.changed.set()
        self.server.shutdown(); self.server.server_close(); self.thread.join(5)
        self.eventually(lambda: b.row()["state"] == "reconnecting")
        self.assertEqual(self.events(log), [])
        self.server = create_server(self.root / "server", port=port)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.assertEqual(self.server.service.admin_token, self.admin)
        sent = a.send("重启后的一条消息", to="reader", key="after-restart")
        self.eventually(lambda: len(self.events(log)) == 1, timeout=8)
        self.assertEqual(self.events(log)[0]["message_id"], sent["message_id"])
        b.receive(); b.ack(sent["message_id"])

    def test_protected_network_join_does_not_claim_background_listening(self):
        protect_network(self.server.service.store.root)
        a = self.client("manual")
        self.assertFalse(a.call("state")["members"][0]["listening"])
        config, _ = self.host()
        b = self.client("background", wake_config=config)
        states = a.call("state")["members"]
        self.assertTrue(all(not m["listening"] for m in states))
        self.assertEqual(next(m for m in states if m["name"] == "background")["state"], "manual_only")
        self.assertFalse(b.join(wake_config=config)["listening"])
        self.assertFalse(b.join(client="manual")["listening"])



if __name__ == "__main__":
    unittest.main()
