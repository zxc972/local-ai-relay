"""Key-free local onboarding with actual HTTP/CLI and isolated member state."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
import zipfile
from urllib.error import HTTPError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from chat_remote import RemoteChat, request
from chat_server import create_server
from chat_safety import protect_network
from chatroom_mcp import ChatEndpoint
from core import RelayError


class LocalNoKeysTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="relay-no-keys-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.server = create_server(self.root / "server", port=0)
        self.server.service.room("work", create=True)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop)
        self.url = self.server.service.public_url

    def stop(self):
        self.server.service.stopping.set()
        self.server.service.changed.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)

    def client(self, name, session=None):
        return RemoteChat(self.root / "clients", self.url, "work", name, session_id=session or name + "-session")

    def test_local_members_join_without_invites_and_keep_private_context(self):
        guide, worker, observer = [self.client(name) for name in ("guide", "worker", "observer")]
        for client in (guide, worker, observer):
            joined = client.join(client="manual", protocol_version=2)
            self.assertEqual(joined["status"], "joined")
            self.assertNotIn("invite", joined)
            self.assertNotIn("token", joined)
            self.assertFalse(client.row()["invite"])
        sent = guide.send("private-task-marker", to="worker")
        batch = worker.receive()
        self.assertEqual(batch["messages"][0]["id"], sent["message_id"])
        worker.send("private-result-marker", reply_to=sent["message_id"], delivery_id=batch["delivery_id"])
        self.assertEqual(guide.receive()["messages"][0]["content"], "private-result-marker")
        self.assertEqual(observer.receive()["messages"], [])
        self.assertEqual(observer.call("history")["messages"], [])
        self.assertNotIn("private-task-marker", json.dumps(observer.context()))
        reopened = self.client("worker")
        self.assertEqual(reopened.join(client="manual", protocol_version=2)["status"], "joined")
        self.assertEqual(reopened.row()["token"], worker.row()["token"])
        self.assertEqual(reopened.receive()["messages"], [])
        with self.assertRaises(RelayError):
            self.client("worker", "another-session").join(client="manual", protocol_version=2)

    def test_cli_join_and_leave_without_a_key(self):
        prefix = [sys.executable, str(ROOT / "relay.py"), "--data-dir", str(self.root / "cli")]
        for action in ("join", "leave"):
            argv = prefix + ["room", action, "work", "--server", self.url, "--name", "cli-agent", "--session-id", "cli-session"]
            if action == "join":
                argv += ["--client", "manual", "--protocol", "2"]
            run = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8", timeout=10)
            self.assertEqual(run.returncode, 0, run.stderr + run.stdout)
            self.assertNotIn("error", json.loads(run.stdout))

    def test_portable_invitation_command_runs_from_a_different_source_folder(self):
        archive = self.root / "release.zip"
        subprocess.run([sys.executable, str(ROOT / "scripts/build_release.py"), "--output", str(archive)],
                       check=True, capture_output=True, timeout=15)
        target = self.root / "another installation 中文"
        with zipfile.ZipFile(archive) as bundle:
            bundle.extractall(target)
        project = target / "local-ai-relay"
        for action in ("join", "leave"):
            argv = [sys.executable, "local_dev.py", "room", action, "work", "--name", "portable",
                    "--session-id", "portable-session", "--server", self.url]
            if action == "join":
                argv += ["--client", "manual", "--protocol", "2"]
            run = subprocess.run(argv, cwd=project, capture_output=True, text=True, encoding="utf-8", timeout=10)
            self.assertEqual(run.returncode, 0, run.stderr + run.stdout)
        self.assertTrue((project / ".local-dev" / "network-clients").is_dir())

    def test_mcp_sessions_join_and_exchange_without_a_key(self):
        endpoint = ChatEndpoint(self.root / "mcp")
        self.addCleanup(endpoint.close)
        def call(action, session, **args):
            return endpoint.invoke("chat_" + action, {"room": "work", "server": self.url, "session_id": session, **args})
        for name in ("one", "two"):
            call("join", name, name=name, client="manual", protocol_version=2)
        sent = call("send", "one", content="mcp-private", to="two")
        self.assertEqual(call("receive", "two")["messages"][0]["id"], sent["message_id"])
        with self.assertRaises(RelayError):
            call("receive", "unknown-session")

    def test_member_and_admin_operations_do_not_accept_missing_or_bad_identity(self):
        client = self.client("agent")
        client.join(client="manual", protocol_version=2)
        for action in ("receive", "context", "history", "send", "ack", "humanjoin", "activity", "remove"):
            with self.subTest(action=action), self.assertRaises(RelayError) as denied:
                request(self.url, "/api/rooms/work/" + action, "", {})
            self.assertEqual(denied.exception.status, 401)
        for token in ("not-a-valid-token", "x" * 43):
            with self.subTest(token_length=len(token)), self.assertRaises(RelayError):
                request(self.url, "/api/rooms/work/state", token, {})
        with self.assertRaises(RelayError):
            request(self.url, "/api/rooms", "", {"action": "create", "room": "forged"})

    def test_cross_origin_proxy_and_network_rooms_cannot_use_local_join(self):
        for headers in ({"Origin": "https://unrelated.invalid"}, {"Sec-Fetch-Site": "cross-site"},
                        {"Host": "unrelated.invalid"}, {"X-Forwarded-For": "203.0.113.4"}):
            req = Request(self.url + "/api/rooms/work/state", b"{}", {"Content-Type": "application/json", **headers})
            with self.subTest(headers=headers), self.assertRaises(HTTPError):
                urlopen(req, timeout=5)
        # A previously published data directory stays protected on loopback.
        protect_network(self.server.service.store.root)
        with self.assertRaises(RelayError):
            self.client("protected").join(client="manual", protocol_version=2)
        server = create_server(self.root / "published", port=0, public_url="https://example.invalid")
        try:
            self.assertFalse(server.local_access)
        finally:
            server.server_close()

    def test_browser_bootstrap_advertises_key_free_mode_without_user_input(self):
        req = Request(self.url + "/api/local-session", b"{}", {"Content-Type": "application/json", "Origin": self.url})
        with urlopen(req, timeout=5) as response:
            session = json.load(response)
        self.assertTrue(session["local_join"])
        created = request(self.url, "/api/rooms", session["admin_token"], {"action": "create", "room": "from-browser"})
        self.assertEqual(created["room"], "from-browser")


if __name__ == "__main__":
    unittest.main()
