"""Room lifecycle and guide/worker collaboration on temporary local services."""
import contextlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from chat_server import create_server
from chat_remote import RemoteChat, request
from chatroom import ChatRoom
from core import RelayError


class RoomLifecycleTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="airrelay-lifecycle-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.server = create_server(self.root / "server", port=0)
        self.chat = self.server.service.room("work", create=True)
        self.server.service.room("other", create=True)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = self.server.service.public_url
        self.clients, self.watchers = [], []
        self.addCleanup(self.stop)

    def stop(self):
        for client in self.clients:
            with contextlib.suppress(RelayError):
                client.leave()
        self.server.service.stopping.set()
        self.server.service.changed.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)
        for thread in self.watchers:
            thread.join(5)
            self.assertFalse(thread.is_alive())
        for client in self.clients:
            self.eventually(lambda: not client.occupied(client.row()))

    def eventually(self, condition, timeout=10):
        deadline = time.monotonic() + timeout
        while not condition():
            if time.monotonic() > deadline:
                self.fail("Expected local event did not arrive")
            time.sleep(.03)

    def owner(self, action, data=None, room="work"):
        path = "/api/rooms" if action == "list" else f"/api/rooms/{room}/{action}"
        return request(self.url, path, self.server.service.admin_token, data or {})

    def client(self, name, room="work", **options):
        client = RemoteChat(self.root / "clients", self.url, room, name, session_id=name + "-session")
        self.clients.append(client)
        client.join(protocol_version=2, **(options or {"client": "manual"}))
        return client

    def test_chinese_room_names_work_over_http_and_remote_clients(self):
        from urllib.parse import quote
        from room_cli import room_name
        created = self.owner("list", {"action": "create", "room": "新项目"})
        self.assertEqual(created["room"], "新项目")
        self.assertIn("新项目", [room["room"] for room in self.owner("list")["rooms"]])
        guide = self.client("guide", room="新项目")
        guide.send("你好")
        history = request(self.url, f"/api/rooms/{quote('新项目')}/history", self.server.service.admin_token, {})
        self.assertEqual([m["content"] for m in history["messages"]], ["你好"])
        self.assertEqual(room_name("cafe\u0301"), "caf\u00e9")
        for bad in ("", "a/b", "a.b", "a b", "_a", "..", "x" * 49, "新\\项"):
            with self.subTest(bad=bad), self.assertRaises(RelayError):
                room_name(bad)
        with self.assertRaises(RelayError):
            request(self.url, f"/api/rooms/{quote('a/b', safe='')}/state", self.server.service.admin_token, {})

    def test_close_preserves_pending_work_and_context_without_replaying_it(self):
        for name in ("guide", "worker"):
            self.chat.join(name, protocol_version=2)
        first = self.chat.send("guide", "task-A", to="worker")
        batch = self.chat.receive("worker")
        second = self.chat.send("guide", "task-B", to="worker")
        self.owner("close")
        self.assertEqual(ChatRoom(self.root / "server", "work").status()["room_state"], "closed")
        for operation in (lambda: self.chat.send("guide", "bad"), lambda: self.chat.receive("worker"),
                          lambda: self.chat.join("new"), lambda: self.chat.ack("worker", delivery_id=batch["delivery_id"], message_ids=[first["message_id"]])):
            with self.assertRaises(RelayError) as denied:
                operation()
            self.assertEqual(denied.exception.status, 409)
        self.assertEqual(len(self.owner("activity")["messages"]), 2)
        self.owner("reopen")
        self.assertEqual(self.chat.receive("worker")["status"], "needs_review")
        self.chat.ack("worker", delivery_id=batch["delivery_id"], message_ids=[first["message_id"]])
        self.assertEqual([m["id"] for m in self.chat.receive("worker")["messages"]], [second["message_id"]])

    def test_delete_hides_room_restore_keeps_it_closed_and_never_changes_other_rooms(self):
        agent = self.client("agent")
        agent.send("saved-message")
        original = agent.context_info()
        self.owner("delete")
        self.assertEqual([r["room"] for r in self.owner("list")["rooms"]], ["other"])
        self.assertEqual([r["room"] for r in self.owner("list", {"include_deleted": True})["rooms"]], ["work"])
        self.assertEqual(self.owner("state", room="other")["room_state"], "open")
        with self.assertRaises(RelayError) as denied:
            agent.call("state")
        self.assertEqual(denied.exception.status, 410)
        with self.assertRaises(RelayError):
            self.chat.create()
        self.owner("restore")
        self.assertEqual(self.owner("state")["room_state"], "closed")
        with self.assertRaises(RelayError):
            agent.send("must not restart")
        self.owner("reopen")
        self.assertEqual(agent.context_info()["member_id"], original["member_id"])
        self.assertEqual(agent.call("history")["messages"][0]["content"], "saved-message")

    def test_only_user_management_session_can_close_delete_or_restore(self):
        agent = self.client("agent")
        for action in ("close", "delete", "reopen", "restore"):
            for token in (agent.row()["token"], self.server.service.invitation(self.chat)["invite"]):
                with self.subTest(action=action), self.assertRaises(RelayError) as denied:
                    request(self.url, f"/api/rooms/work/{action}", token, {})
                self.assertEqual(denied.exception.status, 403)
        self.assertEqual(self.owner("state")["room_state"], "open")

    def test_closing_and_deleting_stop_remote_watchers_without_a_model_call(self):
        for action, room in (("close", "work"), ("delete", "other")):
            client = self.client("watcher-" + action, room=room, client="background-task")
            result = []
            thread = threading.Thread(target=lambda: result.append(client.watch()))
            self.watchers.append(thread)
            thread.start()
            self.eventually(lambda: client.occupied(client.row()))
            self.owner(action, room=room)
            thread.join(6)
            self.assertFalse(thread.is_alive())
            self.assertEqual(result[0]["status"], "stopped")
            self.assertFalse(client.row()["enabled"])
            self.assertNotIn("message_id", result[0])

    def test_local_listener_is_disabled_and_old_generation_cannot_resume(self):
        bg = self.chat.background
        bg.join("worker", client="background-task", protocol_version=2, session_id="worker-session", env={})
        from chat_session import session_owner
        generation = bg.reserve("worker", session_owner("worker-session"), "host-task")
        self.owner("close")
        self.assertEqual(bg.step("worker", generation), ("stop", None))
        self.owner("reopen")
        self.assertEqual(bg.step("worker", generation), ("stop", None))

    def test_user_guide_worker_result_chain_three_rounds_without_model_polling(self):
        # Real watcher subprocesses and real CLI; simulated host decisions only.
        script = self.root / "simulated_host.py"
        script.write_text('''import json,subprocess,sys
from pathlib import Path
event=json.load(sys.stdin)
def cli(action, *extra):
    argv=event["receive_argv"].copy(); argv[argv.index("room")+1]=action
    return json.loads(subprocess.run(argv+list(extra),check=True,capture_output=True,text=True).stdout)
batch=cli("receive")
for message in batch["messages"]:
    if event["name"] == "guide":
        target = "worker" if message["sender"] == "you" else "you"
        cli("send","--to",target,"--text",message["content"])
        cli("ack","--delivery-id",batch["delivery_id"],"--message-ids",message["id"])
    else:
        with Path(sys.argv[1]).open("a") as f: f.write(message["content"]+"\\n")
        cli("send","--reply-to",message["id"],"--delivery-id",batch["delivery_id"],"--text","done:"+message["content"])
with Path(sys.argv[2]).open("a") as f: f.write(event["name"]+"\\n")
''', encoding="utf-8")
        artifact, log = self.root / "work.txt", self.root / "events.txt"
        config = {"type": "command", "argv": [sys.executable, str(script), str(artifact), str(log)], "timeout": 8}
        human = self.client("you")
        self.owner("human", {"member_token": human.row()["token"]})
        self.client("guide", wake_config=config)
        self.client("worker", wake_config=config)
        time.sleep(.25)
        self.assertFalse(log.exists())
        for n in range(3):
            human.send("task-" + str(n), to="guide")
            self.eventually(lambda: log.exists() and len(log.read_text().splitlines()) >= (n + 1) * 3)
            packet = human.receive()
            self.assertEqual([m["content"] for m in packet["messages"]], ["done:task-" + str(n)])
            human.ack(delivery_id=packet["delivery_id"], message_ids=[packet["messages"][0]["id"]])
        self.assertEqual(artifact.read_text().splitlines(), ["task-0", "task-1", "task-2"])
        time.sleep(.3)
        self.assertEqual(len(log.read_text().splitlines()), 9)


if __name__ == "__main__":
    unittest.main()
