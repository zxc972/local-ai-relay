"""Local owner observation never widens member history or consumes delivery."""
import contextlib
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from chat_server import ChatService
from chat_safety import protect_network
from core import RelayError


class ChatViewTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="chat-views-")
        self.addCleanup(temporary.cleanup)
        self.service = ChatService(Path(temporary.name))
        self.chat = self.service.room("work", create=True)
        for name in ("guide", "worker", "observer", "you", "other-human"):
            self.chat.join(name, protocol_version=2, trusted_human=name in ("you", "other-human"))

    def call(self, action, data=None, token=None):
        return self.service.dispatch("/api/rooms/work/" + action,
                                     token or self.service.admin_token, data or {})

    def test_local_owner_sees_agent_collaboration_but_no_human_private_messages(self):
        task = self.chat.send("guide", "task", to="worker")
        secret = self.chat.send("you", "private", to="worker")
        self.chat.send("worker", "private reply", to="you")
        self.chat.send("other-human", "another private message", to="guide")
        public = self.chat.send("you", "room message")
        packet = self.call("activity")
        self.assertEqual([m["id"] for m in packet["messages"]], [task["message_id"], public["message_id"]])
        self.assertTrue(packet["read_only"])
        states = self.call("activityprogress", {"message_ids": [task["message_id"], secret["message_id"]]})
        self.assertEqual(len(states["messages"]), 1)
        self.assertEqual(states["messages"][0]["recipients"], [{"name": "worker", "state": "sent"}])
        self.assertEqual(self.service.history(self.chat, "observer", 0)["messages"][0]["id"], public["message_id"])
        self.assertEqual(len(self.call("history")["messages"]), 1)
        received = self.chat.receive("worker")
        self.assertIn(task["message_id"], [m["id"] for m in received["messages"]])

    def test_only_local_owner_can_use_observation_endpoints(self):
        task = self.chat.send("guide", "task", to="worker")
        invite = self.service.invitation(self.chat)["invite"]
        for action, data in (("activity", {}), ("activityprogress", {"message_ids": [task["message_id"]]})):
            for token in (invite, "not-owner"):
                with self.assertRaises(RelayError) as raised:
                    self.call(action, data, token)
                self.assertEqual(raised.exception.status, 403)
        protect_network(self.service.store.root)
        with self.assertRaises(RelayError):
            self.call("activity")
        with self.assertRaises(RelayError):
            self.call("activityprogress", {"message_ids": [task["message_id"]]})

    def test_peer_history_only_contains_that_pair_and_does_not_ack(self):
        one = self.chat.send("you", "one", to="worker")
        two = self.chat.send("worker", "two", to="you")
        self.chat.send("you", "other", to="guide")
        self.chat.send("guide", "agents", to="worker")
        self.chat.send("guide", "public")
        rows = self.service.history(self.chat, "you", 0, peer="worker")["messages"]
        self.assertEqual([m["id"] for m in rows], [one["message_id"], two["message_id"]])
        state = self.service.progress(self.chat, "worker", [one["message_id"]])["messages"][0]
        self.assertEqual(state["recipients"][0]["state"], "sent")
        with self.assertRaises(RelayError):
            self.call("history", {"peer": "worker"})

    def test_observation_receipts_follow_real_receive_and_ack(self):
        sent = self.chat.send("guide", "task", to="worker")
        data = {"message_ids": [sent["message_id"]]}
        packet = self.chat.receive("worker")
        self.assertEqual(self.call("activityprogress", data)["messages"][0]["recipients"][0]["state"], "received")
        self.chat.ack("worker", message_ids=[sent["message_id"]], delivery_id=packet["delivery_id"])
        self.assertEqual(self.call("activityprogress", data)["messages"][0]["recipients"][0]["state"], "acknowledged")

    def test_historical_kind_does_not_change_when_a_nickname_is_reused(self):
        secret = self.chat.send("guide", "for human", to="you")
        self.chat.leave("you")
        self.chat.join("you", owner="new-agent", protocol_version=2)
        # Reopening/migrating the room must retain the original human recipient kind.
        self.service.rooms.clear()
        self.assertNotIn(secret["message_id"], [m["id"] for m in self.call("activity")["messages"]])
