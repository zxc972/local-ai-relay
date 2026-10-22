import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core import RelayError
from room_cli import ENTRY, Room, install_cli
from test_support import command_arguments

THREAD_A = "12345678-1234-1234-1234-123456789abc"
THREAD_B = "12345678-1234-1234-1234-123456789def"


class RoomCLITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="airelay space ")
        self.data = Path(self.temp.name) / "消息 data"
        self.env = {**os.environ, "CODEX_THREAD_ID": THREAD_A, "PYTHONUTF8": "1"}

    def tearDown(self):
        self.temp.cleanup()

    def run_cli(self, *args, success=True, env=None):
        result = subprocess.run([sys.executable, str(ENTRY), "--data-dir", str(self.data), *args],
                                cwd=self.temp.name, env=env or self.env, text=True, encoding="utf-8",
                                capture_output=True, timeout=8)
        if success:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        else:
            self.assertNotEqual(result.returncode, 0)
        return json.loads(result.stdout)

    def run_generated(self, command):
        result = subprocess.run(command_arguments(command), cwd=self.temp.name, env=self.env,
                                text=True, encoding="utf-8", capture_output=True, timeout=8)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return json.loads(result.stdout)

    def test_two_processes_exchange_task_results_feedback_and_finish(self):
        # The receiver starts before the sender, as it will in the two chat windows.
        receiver = subprocess.Popen([sys.executable, str(ENTRY), "--data-dir", str(self.data),
                                     "join", "demo", "--as", "codex", "--timeout", "5"],
                                    cwd=self.temp.name, env=self.env, text=True, encoding="utf-8",
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            sender = self.run_cli("join", "demo", "--as", "claude", "--task", "计算 17 × 23", "--max-rounds", "2", "--timeout", "0")
            self.assertEqual(sender["status"], "waiting")
            stdout, stderr = receiver.communicate(timeout=7)
            self.assertEqual(receiver.returncode, 0, stderr)
            work = json.loads(stdout)
            self.assertEqual(work["status"], "work")
            self.assertEqual(work["message"]["content"], "计算 17 × 23")
            tid = work["task"]["id"]
            Path(work["result_file"]).write_text("17 × 23 = 391", encoding="utf-8")
            self.run_generated(work["reply_command"])
            review = self.run_cli("receive", "demo", "--as", "claude", "--timeout", "0")
            self.assertEqual(review["result"]["content"], "17 × 23 = 391")
            self.assertTrue(review["can_request_changes"])
            Path(review["review_file"]).write_text("在 391 上加 9", encoding="utf-8")
            self.run_generated(review["review_command"])
            next_work = self.run_cli("receive", "demo", "--as", "codex", "--timeout", "0")
            self.assertEqual(next_work["task"]["id"], tid)
            self.assertEqual(next_work["message"]["round"], 2)
            self.assertEqual(len(next_work["history"]), 3)
            Path(next_work["result_file"]).write_text("391 + 9 = 400", encoding="utf-8")
            self.run_generated(next_work["reply_command"])
            last_review = self.run_cli("receive", "demo", "--as", "claude", "--after-seq", str(review["result"]["seq"]), "--timeout", "0")
            self.assertEqual(last_review["result"]["content"], "391 + 9 = 400")
            self.assertFalse(last_review["can_request_changes"])
            Path(last_review["review_file"]).write_text("不应再开一轮", encoding="utf-8")
            error = self.run_cli("review", "demo", "--reply-to", last_review["result"]["id"], "--file", last_review["review_file"], success=False)
            self.assertIn("最大往返轮数", error["error"])
            self.run_generated(last_review["finish_command"])
            finished = self.run_cli("receive", "demo", "--as", "codex", "--timeout", "0")
            self.assertEqual(finished["status"], "finished")
            self.assertIsNone(finished["next_command"])
            self.assertEqual(len(Room("demo", self.data).store.get_task(tid)["messages"]), 4)
        finally:
            if receiver.poll() is None:
                receiver.terminate()
                receiver.communicate(timeout=3)

    def test_join_is_idempotent_and_room_rejects_a_different_goal(self):
        self.run_cli("join", "demo", "--as", "claude", "--task", "同一个目标", "--timeout", "0")
        self.run_cli("join", "demo", "--as", "claude", "--task", "同一个目标", "--timeout", "0")
        room = Room("demo", self.data)
        self.assertEqual(len(room.store.list_tasks()), 1)
        self.run_cli("join", "demo", "--as", "claude", "--task", "不同目标", "--timeout", "0", success=False)
        self.assertEqual(len(room.store.list_tasks()), 1)

    def test_different_codex_chat_cannot_take_over_a_bound_room(self):
        self.run_cli("join", "bound", "--as", "codex", "--timeout", "0")
        error = self.run_cli("join", "bound", "--as", "codex", "--timeout", "0",
                             env={**self.env, "CODEX_THREAD_ID": THREAD_B}, success=False)
        self.assertIn("另一条 Codex", error["error"])

    def test_unfinished_execution_does_not_repeat_on_receive(self):
        self.run_cli("join", "busy", "--as", "claude", "--task", "只执行一次", "--timeout", "0")
        work = self.run_cli("join", "busy", "--as", "codex", "--timeout", "0")
        repeated = self.run_cli("receive", "busy", "--as", "codex", "--timeout", "0")
        self.assertEqual(repeated["status"], "needs_attention")
        self.assertIsNone(repeated["next_command"])
        self.assertTrue(Path(work["result_file"]).parent.is_dir())

    def test_idle_budget_stops_and_no_goal_is_invented(self):
        room = Room("idle", self.data)
        stopped = room.receive("codex", timeout=0, attempts_left=1)
        self.assertEqual(stopped["status"], "idle_timeout")
        self.assertIsNone(stopped["next_command"])
        peer = room.receive("claude", timeout=0)
        self.assertEqual(peer["status"], "needs_task")
        self.assertEqual(room.store.list_tasks(), [])

    def test_room_paths_cannot_escape_the_data_directory(self):
        for name in ["../../escape", "a/b", ".", "a b", "", "-bad", "a" * 49]:
            with self.assertRaises(RelayError):
                Room(name, self.data)

    def test_installer_is_idempotent_and_does_not_overwrite_another_program(self):
        destination = Path(self.temp.name) / "bin"
        if os.name == "nt":
            with self.assertRaisesRegex(RelayError, "pip"):
                install_cli(destination)
            self.assertFalse(destination.exists())
            return
        first = install_cli(destination)
        self.assertEqual(install_cli(destination)["installed"], first["installed"])
        self.assertEqual((destination / "airelay").resolve(), ENTRY.resolve())
        (destination / "airelay").unlink()
        (destination / "airelay").write_text("another program")
        with self.assertRaises(RelayError):
            install_cli(destination)
        self.assertEqual((destination / "airelay").read_text(encoding="utf-8"), "another program")

    def test_pair_prints_only_connection_instructions(self):
        result = subprocess.run([sys.executable, str(ENTRY), "--data-dir", str(self.data),
                                 "pair", "connected"],
                                cwd=self.temp.name, env=self.env, text=True, encoding="utf-8", capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 0)
        self.assertIn("--as sender", result.stdout)
        self.assertIn("--as receiver", result.stdout)
        self.assertNotIn("--task", result.stdout)
        self.assertNotIn("--as codex", result.stdout)
        self.assertEqual(Room("connected", self.data).store.list_tasks(), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
