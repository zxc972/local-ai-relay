"""Session routing and three simultaneous pairs; temporary DBs, no model calls."""
import concurrent.futures
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from chatroom import ChatRoom
from chatroom_mcp import ChatEndpoint
from chat_remote import RemoteChat, request
from chat_server import create_server
from chat_session import session_owner
from core import RelayError


class SessionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="relay-sessions-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.endpoint = ChatEndpoint(self.root / "clients")
        self.addCleanup(self.endpoint.close)
        self.endpoint.invoke("chat_create", {"room": "work"})

    def call(self, action, session=None, room="work", **args):
        return self.endpoint.invoke("chat_" + action, {"room": room,
            **({"session_id": session} if session else {}), **args})

    def join(self, name, session, **args):
        return self.call("join", session, name=name, client="manual", protocol_version=2, **args)

    def server(self):
        server = create_server(self.root / "server", port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        def shutdown():
            server.service.stopping.set()
            server.service.changed.set()
            server.shutdown()
            server.server_close()
            thread.join(5)
        self.addCleanup(shutdown)
        return server

    def cli(self, action, name="a", session=None, *extra):
        argv = [sys.executable, str(ROOT / "relay.py"), "--data-dir", str(self.root / "clients"),
                "room", action, "work", "--name", name, *extra]
        if session:
            argv += ["--session-id", session]
        env = {k: v for k, v in os.environ.items() if k not in ("CODEX_THREAD_ID", "AIRELAY_SESSION_ID")}
        return subprocess.run(argv, capture_output=True, text=True, timeout=10, env=env)

    def test_shared_mcp_requires_explicit_route_and_refuses_takeover_before_mutation(self):
        self.join("a", "thread-a")
        self.join("b", "thread-b")
        self.call("send", "thread-b", to="a", content="private-b-to-a")
        for action, extra in (("receive", {}), ("context", {}), ("send", {"content": "wrong"}),
                              ("leave", {}), ("checkpoint", {"entries": [], "expected_version": 0})):
            for session in (None, "unknown-thread"):
                with self.subTest(action=action, session=session), self.assertRaises(RelayError):
                    self.call(action, session, **extra)
        with self.assertRaises(RelayError):
            self.join("a", "thread-b")
        other = ChatEndpoint(self.endpoint.data_dir)
        self.addCleanup(other.close)
        with self.assertRaises(RelayError):
            other.invoke("chat_join", {"room": "work", "name": "a", "client": "manual"})
        self.assertEqual(self.call("receive", "thread-a")["messages"][0]["content"], "private-b-to-a")
        self.assertEqual(self.call("status")["latest_seq"], 1)

    def test_cli_cannot_read_or_leave_bound_member_without_original_session(self):
        self.join("a", "thread-a")
        for session in (None, "thread-b"):
            for action in ("receive", "context", "leave"):
                with self.subTest(session=session, action=action):
                    self.assertNotEqual(self.cli(action, session=session).returncode, 0)
        result = self.cli("context", session="thread-a")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["messages"], [])
        self.assertEqual(self.cli("leave", session="thread-a").returncode, 0)

    def test_target_mismatch_blocked_even_when_listener_idle_and_binding_survives_reconnect(self):
        self.join("a", "thread-a")
        with patch("chat_background.adapters.doctor") as doctor:
            with self.assertRaises(RelayError):
                self.call("join", "thread-a", name="a", client="codex", thread_id="thread-b", protocol_version=2)
            doctor.assert_not_called()
        self.endpoint.close()
        replacement = ChatEndpoint(self.endpoint.data_dir)
        self.addCleanup(replacement.close)
        resumed = replacement.invoke("chat_join", {"room": "work", "name": "a", "client": "manual",
            "session_id": "thread-a", "protocol_version": 2})
        self.assertEqual(resumed["session_binding"]["status"], "bound")
        self.assertEqual(len(replacement.invoke("chat_status", {"room": "work"})["members"]), 1)

    def test_rebinding_after_leave_does_not_inherit_history_or_checkpoint(self):
        first = self.join("a", "thread-a")
        self.join("b", "thread-b")
        sent = self.call("send", "thread-b", to="a", content="old private")
        packet = self.call("receive", "thread-a")
        self.call("checkpoint", "thread-a", expected_version=0, entries=[{
            "speaker": "b", "kind": "pending", "text": "old private summary", "sources": [sent["message_id"]]}])
        self.call("leave", "thread-a")
        second = self.join("a", "replacement-a")
        self.assertNotEqual(first["member_id"], second["member_id"])
        self.assertIsNone(self.call("context", "replacement-a")["checkpoint"])
        self.assertEqual(self.call("receive", "replacement-a")["messages"], [])
        for extra in ({"message_id": sent["message_id"]}, {"delivery_id": packet["delivery_id"]}):
            with self.assertRaises(RelayError):
                self.call("context", "replacement-a", **extra)

    def test_bound_local_background_wake_and_rearm_carry_original_session(self):
        self.call("join", "thread-a", name="a", client="background-task", protocol_version=2)
        self.join("b", "thread-b")
        chat = ChatRoom(self.endpoint.data_dir, "work")
        generation = chat.background.reserve("a", session_owner("thread-a"), "host-task")
        self.assertEqual(chat.background.step("a", generation)[0], "waiting")
        sent = self.call("send", "thread-b", to="a", content="wake exactly a")
        result = chat.background.run("a", generation, once=True)
        event = result
        self.assertEqual(event["receive_tool"]["arguments"]["session_id"], "thread-a")
        self.assertIn("--session-id thread-a", event["receive_command"])
        packet = self.call("receive", "thread-a")
        done = self.call("ack", "thread-a", delivery_id=packet["delivery_id"], message_ids=[sent["message_id"]])
        self.assertIn("--session-id thread-a", done["background_task"]["command"])

    def test_remote_same_data_name_has_separate_tokens_and_wrong_session_cannot_reuse_credentials(self):
        server = self.server()
        url = server.service.public_url
        invite = request(url, "/api/rooms", server.service.admin_token, {"action": "create", "room": "work"})["invite"]
        a = RemoteChat(self.root / "shared", url, "work", "a", session_id="thread-a")
        original = a.join(invite, client="background-task", protocol_version=2)
        for session in (None, "thread-b"):
            wrong = RemoteChat(a.base, url, "work", "a", session_id=session)
            self.assertNotEqual(a.store.path, wrong.store.path)
            self.assertNotEqual(a.row()["token"], wrong.row()["token"])
            for action in (wrong.receive, wrong.context, wrong.leave):
                with self.assertRaises(RelayError):
                    action()
            with self.assertRaises(RelayError):
                wrong.join(invite, client="manual", protocol_version=2)
        with self.assertRaises(RelayError):
            a.join(client="manual", thread_id="thread-b", protocol_version=2)
        reopened = RemoteChat(a.base, url, "work", "a", session_id="thread-a")
        self.assertEqual(a.row()["token"], reopened.row()["token"])
        self.assertEqual(reopened.context_info()["member_id"], original["member_id"])
        b = RemoteChat(a.base, url, "work", "b", session_id="thread-b")
        b.join(invite, client="manual", protocol_version=2)
        b.send("private wake", to="a")
        # Run the returned real CLI host-task command; it must use the same token namespace.
        argv = a.argv("watch")
        result = subprocess.run(argv, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        event = json.loads(result.stdout)
        self.assertEqual(event["receive_tool"]["arguments"]["session_id"], "thread-a")
        self.assertIn("--session-id thread-a", event["receive_command"])
        a.leave()
        b.leave()

    def test_legacy_session_keeps_working_and_cannot_be_silently_converted(self):
        joined = self.call("join", name="legacy", client="manual", protocol_version=2)
        self.assertEqual(joined["session_binding"]["status"], "legacy_unbound")
        self.assertEqual(self.call("send", content="legacy message")["status"], "sent")
        with self.assertRaises(RelayError):
            self.join("legacy", "new-thread")
        with self.assertRaises(RelayError):
            self.join("different-name", "new-thread")
        self.assertEqual(self.call("receive")["messages"], [])
        self.assertEqual(self.call("send", content="still works")["status"], "sent")

    def simulate(self, network, separate_rooms):
        server = self.server() if network else None
        endpoint = self.endpoint  # Deliberately share ONE MCP connection and data directory for all six agents.
        url = server.service.public_url if network else None
        agents, invites = [], {}
        for group in range(3):
            room = f"pair-{group}" if separate_rooms else "work"
            if room not in invites:
                if network:
                    invites[room] = request(url, "/api/rooms", server.service.admin_token, {"action": "create", "room": room})["invite"]
                else:
                    endpoint.invoke("chat_create", {"room": room})
                    invites[room] = None
            pair = []
            for role in ("guide", "worker"):
                # Same nicknames in separate rooms exercise room identity; distinct names in one room.
                name = role if separate_rooms else f"{role}-{group}"
                session = f"session-{group}-{role}"
                route = {"room": room, "session_id": session, **({"server": url} if network else {})}
                joined = endpoint.invoke("chat_join", {**route, "name": name, "client": "manual", "protocol_version": 2,
                    **({"invite": invites[room]} if network else {})})
                self.assertEqual(joined["session_binding"]["status"], "bound")
                pair.append((name, session, route))
            agents.append(pair)
        barrier = threading.Barrier(3, timeout=20)
        def exchange(group):
            guide, worker = agents[group]
            def invoke(agent, action, **args):
                return endpoint.invoke("chat_" + action, {**agent[2], **args})
            ids = []
            barrier.wait()
            for round_no in range(10):
                # Repeated key names in every pair must not deduplicate another sender's message.
                sent = invoke(guide, "send", content=f"pair-{group} task-{round_no}", to=worker[0], key=f"task-{round_no}")
                ids.append(sent["message_id"])
                incoming = invoke(worker, "receive")
                self.assertEqual([m["id"] for m in incoming["messages"]], [sent["message_id"]])
                reply = invoke(worker, "send", content=f"pair-{group} result-{round_no}", reply_to=sent["message_id"],
                    delivery_id=incoming["delivery_id"], key=f"reply-{round_no}")
                ids.append(reply["message_id"])
                received = invoke(guide, "receive")
                self.assertEqual([m["id"] for m in received["messages"]], [reply["message_id"]])
                invoke(guide, "ack", delivery_id=received["delivery_id"], message_ids=[reply["message_id"]])
                for agent, speaker, source in ((worker, guide[0], sent), (guide, worker[0], reply)):
                    invoke(agent, "checkpoint", expected_version=round_no, entries=[{
                        "speaker": speaker, "kind": "completed", "text": f"private-summary-{agent[1]}",
                        "sources": [source["message_id"]]}])
                barrier.wait()
            for agent in (guide, worker):
                context = invoke(agent, "context")
                self.assertEqual(context["checkpoint"]["entries"][0]["text"], f"private-summary-{agent[1]}")
                self.assertEqual(context["checkpoint_version"], 10)
                self.assertTrue(all(m["content"].startswith(f"pair-{group} ") for m in context["messages"]))
                history_ids, after = [], 0
                while True:
                    page = invoke(agent, "context", after_seq=after)
                    history_ids.extend(m["id"] for m in page["messages"])
                    if not page["has_more"]:
                        break
                    self.assertGreater(page["next_after_seq"], after)
                    after = page["next_after_seq"]
                self.assertEqual(set(history_ids), set(ids))
                self.assertEqual(invoke(agent, "receive")["messages"], [])
                if network:
                    remote = endpoint.remote_members[(agent[1], url, agent[2]["room"])]
                    self.assertEqual({m["id"] for m in remote.call("history")["messages"]}, set(ids))
            return ids
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
            results = list(pool.map(exchange, range(3)))
        self.assertEqual(len({mid for pair in results for mid in pair}), 60)
        for group, pair in enumerate(agents):
            # Knowing another pair's message ID grants no raw context access or checkpoint source access.
            foreign = results[(group + 1) % 3][0]
            for agent in pair:
                for action, args in (("context", {"message_id": foreign}), ("checkpoint", {
                    "expected_version": 10, "entries": [{"speaker": "guide", "kind": "pending", "text": "foreign", "sources": [foreign]}]})):
                    with self.assertRaises(RelayError):
                        endpoint.invoke("chat_" + action, {**agent[2], **args})
        # Cross-pair ACK must fail before changing the legitimate recipient's pending batch.
        packets = []
        for group, (guide, worker) in enumerate(agents):
            sent = endpoint.invoke("chat_send", {**guide[2], "to": worker[0], "content": f"pair-{group} pending"})
            packet = endpoint.invoke("chat_receive", worker[2])
            packets.append((sent, packet))
        for group, (_, worker) in enumerate(agents):
            foreign_sent, foreign_packet = packets[(group + 1) % 3]
            with self.assertRaises(RelayError):
                endpoint.invoke("chat_ack", {**worker[2], "message_ids": [foreign_sent["message_id"]], "delivery_id": foreign_packet["delivery_id"]})
            own_sent, own_packet = packets[group]
            recovery = endpoint.invoke("chat_receive", worker[2])
            self.assertEqual(recovery["delivery_id"], own_packet["delivery_id"])
            self.assertIn(own_sent["message_id"], recovery["pending_message_ids"])
            endpoint.invoke("chat_ack", {**worker[2], "message_ids": [own_sent["message_id"]], "delivery_id": own_packet["delivery_id"]})
        for pair in agents:
            for agent in pair:
                endpoint.invoke("chat_leave", agent[2])

    def test_three_pairs_local_same_room(self):
        self.simulate(network=False, separate_rooms=False)

    def test_three_pairs_local_separate_rooms(self):
        self.simulate(network=False, separate_rooms=True)

    def test_three_pairs_http_same_room(self):
        self.simulate(network=True, separate_rooms=False)

    def test_three_pairs_http_separate_rooms(self):
        self.simulate(network=True, separate_rooms=True)
