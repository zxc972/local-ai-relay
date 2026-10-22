import contextlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from room_cli import ENTRY, Room
from test_support import command_arguments


class ConnectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="airelay connection ")
        self.data = Path(self.temp.name) / "中文 data"
        self.env = {k: v for k, v in os.environ.items() if k not in ("CODEX_THREAD_ID", "AIRELAY_SESSION_ID")}
        self.env["PYTHONUTF8"] = "1"

    def tearDown(self):
        self.temp.cleanup()

    def cli(self, *args, success=True, env=None):
        result = subprocess.run([sys.executable, str(ENTRY), "--data-dir", str(self.data), *args],
                                cwd=self.temp.name, env=env or self.env, capture_output=True, text=True, encoding="utf-8", timeout=5)
        self.assertEqual(result.returncode == 0, success, result.stdout + result.stderr)
        return json.loads(result.stdout)

    def test_either_role_can_join_first_without_creating_or_consuming_work(self):
        for first, second in [("sender", "receiver"), ("receiver", "sender")]:
            name = first + "_first"
            waiting = self.cli("join", name, "--as", first)
            self.assertEqual(waiting["status"], "waiting_for_peer")
            self.assertIsNone(waiting["next_command"])
            self.assertEqual(waiting["mode"], "standby")
            connected = self.cli("join", name, "--as", second)
            self.assertEqual(connected["status"], "connected")
            self.assertIsNone(connected["next_command"])
            self.assertEqual(self.cli("status", name)["status"], "connected")
            self.cli("join", name, "--as", first)
            room = Room(name, self.data)
            self.assertEqual(room.store.list_tasks(), [])
            self.assertFalse((room.store.root / "binding.json").exists())
            with contextlib.closing(room.store.connect()) as db:
                self.assertEqual(db.execute("SELECT COUNT(*) FROM channel_peers").fetchone()[0], 2)
                self.assertEqual(db.execute("SELECT COUNT(*) FROM channel_messages").fetchone()[0], 0)
            empty = self.cli("receive", name, "--as", second, "--timeout", "0")
            self.assertEqual(empty["status"], "empty")
            self.assertIsNone(empty["next_command"])

    def test_task_on_join_and_send_before_pairing_are_rejected(self):
        self.cli("join", "demo", "--as", "sender", "--task", "不能发送", success=False)
        self.cli("join", "demo", "--as", "sender")
        self.cli("send", "demo", "--text", "尚未连接", success=False)
        room = Room("demo", self.data)
        self.assertEqual(room.store.list_tasks(), [])
        with contextlib.closing(room.store.connect()) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM channel_messages").fetchone()[0], 0)

    def test_two_chats_can_register_the_same_new_room_concurrently(self):
        processes = [subprocess.Popen(
            [sys.executable, str(ENTRY), "--data-dir", str(self.data), "join", "same_time", "--as", role],
            cwd=self.temp.name, env=self.env, text=True, encoding="utf-8", stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            for role in ("sender", "receiver")]
        try:
            for process in processes:
                stdout, stderr = process.communicate(timeout=5)
                self.assertEqual(process.returncode, 0, stdout + stderr)
                self.assertIsNone(json.loads(stdout)["next_command"])
            self.assertEqual(self.cli("status", "same_time")["status"], "connected")
            self.assertEqual(Room("same_time", self.data).store.list_tasks(), [])
        finally:
            for process in processes:
                if process.poll() is None:
                    process.terminate()
                    process.communicate(timeout=3)

    def test_later_messages_and_reply_work_without_automatic_rounds(self):
        self.cli("join", "demo", "--as", "sender")
        self.cli("join", "demo", "--as", "receiver")
        text = "明确指定的内容 $(touch never-create) 与 '引号'"
        sent = self.cli("send", "demo", "--text", text, "--key", "one")
        repeated = self.cli("send", "demo", "--text", text, "--key", "one")
        self.assertTrue(repeated["duplicate"])
        self.assertEqual(sent["message_id"], repeated["message_id"])
        self.cli("send", "demo", "--text", "不同内容", "--key", "one", success=False)
        # Joining after delivery must still neither read nor run a queued message.
        joined = self.cli("join", "demo", "--as", "receiver")
        self.assertEqual(joined["status"], "connected")
        self.assertNotIn("message", joined)
        work = self.cli("receive", "demo", "--as", "receiver", "--timeout", "0")
        self.assertEqual(work["message"]["content"], text)
        self.assertEqual(work["message"]["state"], "received")
        self.assertIsNone(work["next_command"])
        busy = self.cli("receive", "demo", "--as", "receiver", "--timeout", "0")
        self.assertEqual(busy["status"], "needs_attention")
        self.assertNotIn("message", busy)
        resumed = self.cli("receive", "demo", "--as", "receiver", "--resume", "--timeout", "0")
        self.assertEqual(resumed["message"]["id"], sent["message_id"])
        Path(work["reply_file"]).write_text("实际结果", encoding="utf-8")
        command = command_arguments(work["reply_command"])
        for expected_duplicate in [False, True]:
            result = subprocess.run(command, env=self.env, cwd=self.temp.name,
                                    capture_output=True, text=True, encoding="utf-8", timeout=5)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(json.loads(result.stdout)["duplicate"], expected_duplicate)
        reply = self.cli("receive", "demo", "--as", "sender", "--timeout", "0")
        self.assertEqual(reply["message"]["content"], "实际结果")
        self.assertEqual(reply["message"]["reply_to"], sent["message_id"])
        self.cli("ack", "demo", reply["message"]["id"], "--as", "sender")
        next_message = self.cli("send", "demo", "--text", "另一个独立命令")
        self.assertNotEqual(next_message["message_id"], sent["message_id"])
        self.assertEqual(self.cli("receive", "demo", "--as", "receiver", "--timeout", "0")["message"]["content"], "另一个独立命令")
        self.assertFalse((Path(self.temp.name) / "never-create").exists())

    def test_role_is_independent_of_client_and_protects_identifiable_sessions(self):
        codex_sender = {**self.env, "CODEX_THREAD_ID": "codex-sender"}
        self.cli("join", "swapped", "--as", "sender", env=codex_sender)
        self.assertEqual(self.cli("join", "swapped", "--as", "receiver")["status"], "connected")
        self.cli("join", "swapped", "--as", "sender", env=codex_sender)
        self.cli("join", "swapped", "--as", "sender", env={**self.env, "CODEX_THREAD_ID": "other-chat"}, success=False)
        self.cli("leave", "swapped", "--as", "receiver")
        self.assertEqual(self.cli("status", "swapped")["status"], "waiting_for_peer")
        self.cli("send", "swapped", "--text", "对方已退出", env=codex_sender, success=False)


if __name__ == "__main__":
    unittest.main(verbosity=2)
