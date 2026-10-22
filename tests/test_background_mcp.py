"""End-to-end CLI configuration, MCP clients, real watcher and host callbacks."""
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
from background import Background
from room_cli import Room
from test_message_bus import Peer


class BackgroundMCPTests(unittest.TestCase):
    def wait(self, predicate, timeout=5):
        deadline = time.monotonic() + timeout
        while not predicate():
            if time.monotonic() >= deadline:
                self.fail("Timed out waiting for host callback")
            time.sleep(.02)

    def test_cli_config_mcp_dispatch_real_callback_and_result_callback(self):
        with tempfile.TemporaryDirectory(prefix="relay integration 空间 ") as temp:
            root = Path(temp)
            data = root / "state"
            hook = root / "host callback.py"
            hook.write_text(
                "import json,pathlib,sys\n"
                "event=json.load(sys.stdin)\n"
                "with pathlib.Path(sys.argv[1]).open('a',encoding='utf-8') as stream:\n"
                "    stream.write(json.dumps(event)+'\\n')\n", encoding="utf-8")
            events = {role: root / (role + ".jsonl") for role in ("sender", "receiver")}
            background = Background(Room("shared", data))
            peers = []

            def cli(*args):
                env = {key: value for key, value in os.environ.items()
                       if key not in ("AIRELAY_SESSION_ID", "CODEX_THREAD_ID")}
                result = subprocess.run([sys.executable, str(ROOT / "relay.py"), "--data-dir", str(data), *args],
                                        check=True, capture_output=True, text=True, encoding="utf-8", env=env,
                                        timeout=8)
                parsed = json.loads(result.stdout)
                self.assertNotIn("error", parsed, parsed)
                return parsed

            def recorded(role):
                path = events[role]
                return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []

            try:
                for role in ("sender", "receiver"):
                    config = root / (role + " config.json")
                    config.write_text(json.dumps({"type": "command", "argv": [sys.executable, str(hook), str(events[role])]}),
                                      encoding="utf-8")
                    connected = cli("connect", "shared", "--as", role, "--wake-config", str(config))
                    self.assertEqual(connected["mode"], "background")
                    self.assertIsNone(connected["next_tool"])
                    self.assertTrue(connected["background"]["running"])
                peers = [Peer(data), Peer(data, cli=True)]
                sender, receiver = peers
                for peer, role in ((sender, "sender"), (receiver, "receiver")):
                    packet = peer.tool("relay_connect", room="shared", role=role)
                    self.assertIsNone(packet["next_tool"])
                time.sleep(.35)
                self.assertEqual(recorded("sender"), [])
                self.assertEqual(recorded("receiver"), [])
                sent = sender.tool("relay_send", room="shared", role="sender", content="授权测试：计算 2+2")
                self.wait(lambda: len(recorded("receiver")) == 1)
                event = recorded("receiver")[0]
                self.assertEqual(event["message_id"], sent["message_id"])
                received = receiver.tool(event["receive_tool"]["name"], **event["receive_tool"]["arguments"])
                self.assertEqual(received["message"]["content"], "授权测试：计算 2+2")
                self.assertIsNone(received["after_handling_tool"])
                result = receiver.tool("relay_reply", room="shared", role="receiver",
                                       message_id=received["message"]["id"], content="4；协议联调，无模型调用")
                self.wait(lambda: len(recorded("sender")) == 1)
                event = recorded("sender")[0]
                self.assertEqual(event["message_id"], result["message_id"])
                reply = sender.tool(event["receive_tool"]["name"], **event["receive_tool"]["arguments"])
                self.assertEqual(reply["message"]["kind"], "result")
                self.assertEqual(reply["message"]["content"], "4；协议联调，无模型调用")
                ack = sender.tool("relay_ack", room="shared", role="sender", message_id=reply["message"]["id"])
                self.assertIsNone(ack["next_tool"])
                empty = receiver.tool("relay_receive", room="shared", role="receiver")
                self.assertEqual(empty["status"], "empty")
                self.assertIsNone(empty["next_tool"])
                time.sleep(.35)
                self.assertEqual(len(recorded("sender")), 1)
                self.assertEqual(len(recorded("receiver")), 1)
                for peer, role in ((sender, "sender"), (receiver, "receiver")):
                    packet = peer.tool("relay_disconnect", room="shared", role=role)
                    self.assertEqual(packet["status"], "disconnected")
                self.wait(lambda: not any(background.status(role)["worker_active"] for role in ("sender", "receiver")))
            finally:
                for peer in peers:
                    peer.close()
                for role in ("sender", "receiver"):
                    background.stop(role)
                self.wait(lambda: not any(background.status(role)["worker_active"] for role in ("sender", "receiver")))


if __name__ == "__main__":
    unittest.main()
