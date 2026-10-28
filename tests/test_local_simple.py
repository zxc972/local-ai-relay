"""Simple tool profile still supports private, exactly acknowledged collaboration."""
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from chatroom import ChatRoom
from room_cli import mcp_config
from test_message_bus import Peer


class SimpleProfileTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="local simple 中文 ")
        self.addCleanup(temporary.cleanup)
        self.data = Path(temporary.name)

    def peer(self, cli=False):
        peer = Peer(self.data, cli=cli, simple=True)
        self.addCleanup(peer.close)
        return peer

    def test_nine_tools_and_hidden_tools_are_rejected_before_dispatch(self):
        peer = self.peer()
        names = {tool["name"] for tool in peer.call("tools/list", {})["tools"]}
        self.assertEqual(names, {"chat_create", "chat_join", "chat_send", "chat_receive",
                                "chat_ack", "chat_context", "chat_checkpoint", "chat_status", "chat_leave"})
        peer.tool("chat_create", room="work")
        peer.tool("chat_join", room="work", name="member", protocol_version=2, client="manual")
        for name in ("chat_wait", "chat_submit_job", "chat_request_action", "chat_execute_action"):
            response = peer.call("tools/call", {"name": name, "arguments": {"room": "work"}})
            self.assertTrue(response["isError"])
            self.assertIn("白名单", response["content"][0]["text"])

    def test_guide_worker_private_three_rounds_with_an_unnotified_observer(self):
        guide, worker, observer = self.peer(cli=True), self.peer(), self.peer()
        guide.tool("chat_create", room="work")
        for peer, name in ((guide, "guide"), (worker, "worker"), (observer, "observer")):
            peer.tool("chat_join", room="work", name=name, protocol_version=2, client="manual")
        for number in range(3):
            sent = guide.tool("chat_send", room="work", to="worker", content=f"task {number}")
            packet = worker.tool("chat_receive", room="work")
            self.assertEqual([m["id"] for m in packet["messages"]], [sent["message_id"]])
            reply = worker.tool("chat_send", room="work", content=f"verified {number}",
                                reply_to=sent["message_id"], delivery_id=packet["delivery_id"])
            result = guide.tool("chat_receive", room="work")
            self.assertEqual([m["id"] for m in result["messages"]], [reply["message_id"]])
            guide.tool("chat_ack", room="work", message_ids=[reply["message_id"]], delivery_id=result["delivery_id"])
        self.assertEqual(observer.tool("chat_receive", room="work")["messages"], [])
        self.assertEqual(len(ChatRoom(self.data, "work").status()["members"]), 3)

    def test_generated_config_opts_in_without_replacing_full_mode(self):
        entry = mcp_config(self.data, simple=True)["mcpServers"]["local-ai-relay"]
        self.assertIn("--simple", entry["args"])
        self.assertEqual(json.loads((self.data / "mcp.json").read_text())["mcpServers"]["local-ai-relay"], entry)
        self.assertNotIn("--simple", mcp_config(self.data)["mcpServers"]["local-ai-relay"]["args"])
