"""Public local auto-reception and protected network boundaries; no model calls."""
import contextlib
import json
import shlex
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from chat_server import create_server
from chat_remote import RemoteChat, request
from chat_safety import local_trusted, protect_network, NETWORK_MARKER, ActionGate
from chatroom import ChatRoom
from chatroom_mcp import ChatEndpoint
from core import RelayError


class LocalChatModeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="local-chat-mode-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.server = create_server(self.root / "server", port=0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.shutdown)
        self.url = self.server.service.public_url
        self.invite = request(self.url, "/api/rooms", self.server.service.admin_token,
                              {"action": "create", "room": "work"})["invite"]
        self.clients = []

    def shutdown(self):
        for client in self.clients:
            with contextlib.suppress(RelayError):
                client.leave()
        self.server.service.stopping.set()
        self.server.service.changed.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)
        for client in self.clients:
            self.eventually(lambda: not client.occupied(client.row()))

    def eventually(self, condition, timeout=8):
        deadline = time.monotonic() + timeout
        while not condition():
            if time.monotonic() > deadline:
                self.fail("Expected local transport completion did not arrive")
            time.sleep(.025)

    def client(self, name, **options):
        client = RemoteChat(self.root / name, self.url, "work", name)
        self.clients.append(client)
        client.join(self.invite, protocol_version=2, **({"client": "manual"} if not options else options))
        return client

    def test_local_session_cli_joins_using_this_source_and_data(self):
        request_body = Request(self.url + "/api/local-session", b"{}",
                               {"Content-Type": "application/json", "Origin": self.url})
        with urlopen(request_body, timeout=5) as response:
            session = json.load(response)
        prefix = shlex.split(session["cli_command"])
        self.assertEqual(Path(prefix[1]), ROOT / "relay.py")
        self.assertEqual(Path(prefix[3]), self.server.service.store.root)
        with patch("chat_server.secrets.token_urlsafe", return_value="-" + "a" * 42):
            invitation = request(self.url, "/api/rooms", self.server.service.admin_token,
                                 {"action": "create", "room": "dash-test"})["invite"]
        self.assertTrue(invitation.startswith("-"))
        for action in ("join", "leave"):
            argv = prefix + ["room", action, "dash-test", "--name", "test-cli", "--server", self.url]
            if action == "join":
                argv += ["--client", "manual", "--protocol", "2", "--invite=" + invitation]
            run = subprocess.run(argv, capture_output=True, text=True, timeout=8)
            self.assertEqual(run.returncode, 0, run.stderr + run.stdout)
            self.assertNotIn("error", json.loads(run.stdout))

    def test_three_automatic_receive_reply_cycles_without_relay_approval(self):
        log, result, handler = [self.root / name for name in ("events.jsonl", "host-result.txt", "host.py")]
        handler.write_text('''import json,subprocess,sys
event=json.load(sys.stdin)
def run(argv):
    return json.loads(subprocess.run(argv,check=True,capture_output=True,text=True).stdout)
packet=run(event["receive_argv"])
message=packet["messages"][0]
with open(sys.argv[2],"w") as output: output.write("host handled: "+message["content"])
argv=event["receive_argv"].copy()
argv[argv.index("room")+1]="send"
argv += ["--reply-to",message["id"],"--delivery-id",packet["delivery_id"],"--text","reply: "+message["content"]]
run(argv)
with open(sys.argv[1],"a") as output: output.write(json.dumps({"id":message["id"]})+"\\n")
''')
        writer = self.client("writer")
        reader = self.client("reader", wake_config={"type": "command", "argv": [sys.executable, str(handler), str(log), str(result)], "timeout": 5})
        self.assertTrue(reader.followup()["background"]["running"])
        self.assertEqual(reader.status()["security"]["execution"], "host-permissions")
        time.sleep(.4)
        self.assertFalse(log.exists(), "Idle transport must not invoke a host/model")
        for number in range(1, 4):
            sent = writer.send(str(number), to="reader")
            self.eventually(lambda: log.exists() and len(log.read_text().splitlines()) == number)
            reply = writer.receive()
            self.assertEqual(reply["messages"][0]["content"], "reply: " + str(number))
            writer.ack(delivery_id=reply["delivery_id"], message_ids=[reply["messages"][0]["id"]])
            self.assertEqual(result.read_text(), "host handled: " + str(number))
            self.assertFalse((reader.base / "local-safety").exists())
        time.sleep(.4)
        self.assertEqual(len(log.read_text().splitlines()), 3, "No echo or idle wake")

    def test_three_native_task_cycles_with_generic_capability_and_legacy_alias(self):
        writer = self.client("writer")
        for capability in ("background-task", "claude"):
            with self.subTest(capability=capability):
                reader = self.client("reader-" + capability, client=capability)
                for number in range(3):
                    self.assertTrue(reader.followup()["background_task"]["run_in_background"])
                    process = subprocess.Popen(reader.argv("watch"), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                    try:
                        self.eventually(lambda: reader.occupied(reader.row()))
                        time.sleep(.25)
                        self.assertIsNone(process.poll(), "Idle background task should wait without a model call")
                        sent = writer.send("round " + str(number), to=reader.member)
                        output, error = process.communicate(timeout=6)
                        self.assertEqual(process.returncode, 0, error)
                        self.assertEqual(json.loads(output)["message_id"], sent["message_id"])
                        batch = reader.receive()
                        response = reader.send("handled", reply_to=sent["message_id"], delivery_id=batch["delivery_id"])
                        self.assertTrue(response["background_task"]["run_in_background"])
                    finally:
                        if process.poll() is None:
                            process.terminate()
                            process.communicate(timeout=3)
                self.assertFalse((reader.base / "local-safety").exists())

    def test_local_file_and_http_actions_keep_the_native_permission_boundary(self):
        target = self.root / "not-written-by-middleware.txt"
        action = {"type": "write_text", "path": str(target), "text": "host decides"}
        endpoint = ChatEndpoint(self.root / "file-data")
        self.addCleanup(endpoint.close)
        endpoint.invoke("chat_create", {"room": "local"})
        joined = endpoint.invoke("chat_join", {"room": "local", "name": "agent", "client": "manual", "protocol_version": 2})
        self.assertEqual(joined["security"]["policy"], "trusted-local")
        self.assertFalse(joined["listening"])
        result = endpoint.invoke("chat_request_action", {"room": "local", "action": action})
        self.assertEqual(result["status"], "use_host_tools")
        self.assertFalse(result["executed"])
        self.assertNotIn("request_id", result)
        remote = self.client("remote-local")
        self.assertEqual(remote.request_action(action)["status"], "use_host_tools")
        self.assertEqual(remote.context()["security"], {"policy": "trusted-local", "execution": "host-permissions"})
        self.assertFalse(target.exists(), "Middleware must not bypass native permissions")

    def test_public_exposure_cannot_be_bypassed_by_loopback_or_direct_cli(self):
        protect_network(self.server.service.store.root)
        reader = self.client("reader", client="background-task")
        self.assertFalse(reader.followup()["security"]["automatic"])
        self.assertNotIn("background_task", reader.followup())
        with self.assertRaises(RelayError):
            reader.watch()
        chat = ChatRoom(self.server.service.store.root, "work")
        self.assertFalse(chat.background.join("local-cli", client="background-task")["security"]["automatic"])
        gate = ActionGate(self.server.service.store.root)
        with self.assertRaises(RelayError):
            gate.request("scope", {"type": "delete_file", "path": str(self.server.service.store.root / NETWORK_MARKER)})

    def test_remote_server_or_missing_policy_cannot_claim_trusted_local(self):
        client = RemoteChat(self.root / "external", "https://example.invalid", "work", "agent")
        self.assertFalse(client.local_mode({"policy": "trusted-local", "automatic": True}))
        local = RemoteChat(self.root / "old-server", self.url, "work", "agent")
        self.assertFalse(local.local_mode({}))

    def test_public_deployment_remains_protected_after_loopback_restart(self):
        data = self.root / "published"
        server = create_server(data, port=0, public_url="https://example.invalid")
        try:
            self.assertFalse(server.local_access)
            self.assertFalse(local_trusted(data))
        finally:
            server.server_close()
        reopened = create_server(data, port=0)
        try:
            self.assertTrue(reopened.local_access)
            self.assertFalse(local_trusted(data))
        finally:
            reopened.server_close()

    def test_forwarded_request_cannot_treat_a_proxy_as_trusted_local(self):
        req = Request(self.url + "/api/rooms/work/state", b"{}", {
            "Content-Type": "application/json", "Authorization": "Bearer " + self.server.service.admin_token,
            "X-Forwarded-For": "203.0.113.1"})
        with urlopen(req, timeout=4) as response:
            state = json.load(response)
        self.assertEqual(state["security"]["policy"], "chat-only")
        self.assertFalse(local_trusted(self.server.service.store.root))

    def test_exposing_room_stops_a_running_local_listener_without_a_wake(self):
        log, handler = self.root / "unexpected.txt", self.root / "callback.py"
        handler.write_text("from pathlib import Path; import sys; Path(sys.argv[1]).write_text('unexpected')")
        reader = self.client("reader", wake_config={"type": "command", "argv": [sys.executable, str(handler), str(log)]})
        protect_network(self.server.service.store.root)
        self.server.service.changed.set()
        self.eventually(lambda: not reader.row()["enabled"])
        self.assertEqual(reader.row()["state"], "manual_only")
        self.assertFalse(log.exists())
        self.assertFalse(local_trusted(self.server.service.store.root))


if __name__ == "__main__":
    unittest.main()
