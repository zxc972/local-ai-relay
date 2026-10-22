import contextlib
import json
import os
import queue
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from room_cli import ENTRY, Room


class AutoReceiveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="relay auto ")
        self.data = Path(self.temp.name) / "消息 data"
        self.env = {k: v for k, v in os.environ.items() if k not in ("CODEX_THREAD_ID", "AIRELAY_SESSION_ID")}
        self.env["PYTHONUTF8"] = "1"

    def tearDown(self):
        self.temp.cleanup()

    def cli(self, *args, success=True):
        result = subprocess.run([sys.executable, str(ENTRY), "--data-dir", str(self.data), *args],
                                cwd=self.temp.name, env=self.env, text=True, encoding="utf-8", capture_output=True, timeout=5)
        self.assertEqual(result.returncode == 0, success, result.stdout + result.stderr)
        return json.loads(result.stdout)

    def test_prepare_does_not_start_a_model_or_read_messages(self):
        joined = self.cli("join", "demo", "--as", "receiver", "--auto", "--client", "codex")
        self.assertEqual(joined["auto_receive"]["status"], "requires_event_adapter")
        self.assertFalse(joined["auto_receive"]["enabled"])
        self.assertNotIn("heartbeat_prompt_file", joined)
        self.assertFalse(list(self.data.rglob("heartbeat-*.md")))
        self.cli("join", "demo", "--as", "sender")
        sent = self.cli("send", "demo", "--text", "仅入队")
        self.assertEqual(self.cli("poll", "demo", "--as", "receiver")["status"], "disabled")
        with contextlib.closing(Room("demo", self.data).store.connect()) as db:
            self.assertEqual(db.execute("SELECT state FROM channel_messages WHERE id=?", (sent["message_id"],)).fetchone()[0], "queued")
        plan = self.cli("auto", "demo", "--as", "sender", "--client", "claude")
        self.assertEqual(plan["auto_receive"]["status"], "requires_channel_restart")
        config = json.loads(Path(plan["mcp_config"]).read_text(encoding="utf-8"))
        server = next(iter(config["mcpServers"].values()))
        self.assertIn("channel_server.py", server["args"][0])
        self.assertEqual(server["env"]["CODEX_THREAD_ID"], "")
        self.assertEqual(Room("demo", self.data).store.list_tasks(), [])

    def test_retired_scheduled_poll_cannot_consume_even_with_stale_enabled_state(self):
        self.cli("join", "demo", "--as", "receiver", "--auto", "--client", "codex")
        self.cli("join", "demo", "--as", "sender")
        self.cli("auto", "demo", "--as", "receiver", "--client", "codex", "--scheduled", "test-only", success=False)
        from auto_receive import write_state
        room = Room("demo", self.data)
        with room.store.transaction() as db:
            write_state(db, "receiver", {"enabled": True, "client": "codex",
                                         "automation_id": "old-install", "status": "scheduled"})
        sent = self.cli("send", "demo", "--text", "保留给事件收件")
        self.assertEqual(self.cli("poll", "demo", "--as", "receiver")["status"], "disabled")
        with contextlib.closing(room.store.connect()) as db:
            self.assertEqual(db.execute("SELECT state FROM channel_messages WHERE id=?",
                                        (sent["message_id"],)).fetchone()[0], "queued")
        self.cli("auto-off", "demo", "--as", "receiver")
        self.assertEqual(self.cli("poll", "demo", "--as", "receiver")["status"], "disabled")
        self.assertEqual(self.cli("receive", "demo", "--as", "receiver", "--timeout", "0")["message"]["content"], "保留给事件收件")

    def test_native_channel_pushes_after_confirmation_and_roundtrips_without_polling(self):
        # The receiver is a protocol-test peer using a single explicit receive.
        # Only the native sender channel's actual push is under test here.
        self.cli("join", "demo", "--as", "receiver")
        plan = self.cli("auto", "demo", "--as", "sender", "--client", "claude")
        config = next(iter(json.loads(Path(plan["mcp_config"]).read_text(encoding="utf-8"))["mcpServers"].values()))
        process = subprocess.Popen([config["command"], *config["args"]], env=self.env,
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   text=True, encoding="utf-8", bufsize=1)
        incoming = queue.Queue()
        def read():
            for line in process.stdout:
                incoming.put(json.loads(line))
        reader = threading.Thread(target=read, daemon=True)
        reader.start()
        notices = []
        def write(value):
            process.stdin.write(json.dumps(value) + "\n")
            process.stdin.flush()
        def call(rid, method, params=None):
            write({"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}})
            while True:
                packet = incoming.get(timeout=4)
                if packet.get("id") == rid:
                    return packet["result"]
                notices.append(packet)
        def tool(rid, name, arguments=None):
            result = call(rid, "tools/call", {"name": name, "arguments": arguments or {}})
            self.assertFalse(result["isError"], result)
            return result["structuredContent"]
        try:
            initialized = call(1, "initialize", {"protocolVersion": "2025-06-18", "clientInfo": {"name": "protocol-test", "version": "1"}})
            self.assertIn("claude/channel", initialized["capabilities"]["experimental"])
            write({"jsonrpc": "2.0", "method": "notifications/initialized"})
            # Ping establishes that initialization was handled, without claiming ready.
            call(2, "ping")
            self.assertEqual(self.cli("status", "demo")["peers"]["sender"]["auto_receive"]["status"], "awaiting_client_confirmation")
            # A receiver reply is pushed into the sender even while no receive tool is running.
            command = self.cli("send", "demo", "--as", "sender", "--text", "明确的执行指令")
            self.cli("receive", "demo", "--as", "receiver", "--timeout", "0")
            sent = self.cli("send", "demo", "--as", "receiver", "--reply-to", command["message_id"], "--text", "给 sender 的结果")
            with self.assertRaises(queue.Empty):
                incoming.get(timeout=.3)
            self.assertEqual(tool(3, "relay_ready")["status"], "listening")
            notification = notices.pop() if notices else incoming.get(timeout=3)
            self.assertEqual(notification["method"], "notifications/claude/channel")
            self.assertEqual(notification["params"]["meta"]["message_id"], sent["message_id"])
            # Notification writes do not mark the message read; only the actual tool does.
            with contextlib.closing(Room("demo", self.data).store.connect()) as db:
                self.assertEqual(db.execute("SELECT state FROM channel_messages WHERE id=?", (sent["message_id"],)).fetchone()[0], "queued")
            received = tool(4, "relay_receive")
            self.assertEqual(received["message"]["content"], "给 sender 的结果")
            tool(5, "relay_send", {"content": "具体意见", "reply_to": sent["message_id"]})
            feedback = self.cli("receive", "demo", "--as", "receiver", "--timeout", "0")
            self.assertEqual(feedback["message"]["content"], "具体意见")
            self.cli("auto-off", "demo", "--as", "sender")
            self.cli("send", "demo", "--as", "receiver", "--reply-to", feedback["message"]["id"], "--text", "停止后不推送")
            with self.assertRaises(queue.Empty):
                incoming.get(timeout=.4)
            denied = call(6, "tools/call", {"name": "relay_receive", "arguments": {}})
            self.assertTrue(denied["isError"])
        finally:
            process.stdin.close()
            try:
                process.wait(timeout=4)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)
            reader.join(timeout=1)
            stderr = process.stderr.read()
            process.stdout.close()
            process.stderr.close()
            self.assertEqual(process.returncode, 0, stderr)

    def test_launcher_does_not_nest_claude_inside_a_tool_process(self):
        result = self.cli("claude", "demo", "--as", "sender", success=False)
        self.assertIn("外部交互终端", result["error"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
