"""Unpack the distributable and exercise it from an unrelated directory."""
import json
import os
from pathlib import Path
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen
import zipfile

ROOT = Path(__file__).resolve().parents[1]


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="airelay release 中文 ")
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.archive = self.folder / "release.zip"
        subprocess.run([sys.executable, str(ROOT / "scripts/build_release.py"), "--output", str(self.archive)],
                       check=True, capture_output=True, text=True, timeout=15)
        with zipfile.ZipFile(self.archive) as bundle:
            bundle.extractall(self.folder / "unpacked")
        self.source = self.folder / "unpacked/local-ai-relay"
        self.entry = self.source / "relay.py"
        self.cwd = self.folder / "unrelated"
        self.cwd.mkdir()
        self.env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "AIRELAY_DATA_DIR", "CODEX_THREAD_ID", "AIRELAY_SESSION_ID")}
        self.env["PYTHONUNBUFFERED"] = "1"
        self.servers = []
        self.addCleanup(self.stop_servers)

    def stop_servers(self):
        for process in self.servers:
            if process.poll() is None:
                process.terminate()
            process.communicate(timeout=8)

    def start_server(self, port=0, public_url=None):
        command = [sys.executable, str(self.entry), "--data-dir", str(self.folder / "server-data"), "serve", "--port", str(port)]
        if public_url:
            command += ["--public-url", public_url]
        process = subprocess.Popen(command, cwd=self.cwd, env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.servers.append(process)
        lines = queue.Queue()
        def read_startup():
            text = ""
            for line in process.stdout:
                text += line
                try:
                    value = json.loads(text)
                except json.JSONDecodeError:
                    continue
                lines.put(value)
                return
            lines.put(None)
        threading.Thread(target=read_startup, daemon=True).start()
        started = lines.get(timeout=8)
        self.assertIsNotNone(started, "Server exited without startup information")
        self.assertEqual(started["status"], "listening")
        return process, started["url"].split("#")[0].rstrip("/")

    def post(self, base, path, data, token=None, **headers):
        headers["Content-Type"] = "application/json"
        if token:
            headers["Authorization"] = "Bearer " + token
        with urlopen(Request(base + path, json.dumps(data).encode(), headers), timeout=5) as response:
            return json.load(response)

    def cli(self, name, *args):
        command = [sys.executable, str(self.entry), "--data-dir", str(self.folder / ("client-" + name)), "room", *args]
        result = subprocess.run(command, cwd=self.cwd, env=self.env, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        return json.loads(result.stdout)

    def test_local_entry_generates_simple_config_only_in_its_own_data(self):
        inherited = self.folder / "existing-data"
        inherited.mkdir()
        (inherited / "sentinel").write_text("untouched")
        env = {**self.env, "AIRELAY_DATA_DIR": str(inherited)}
        run = subprocess.run([sys.executable, str(self.source / "local_dev.py"), "mcp-config"],
                             cwd=self.cwd, env=env, capture_output=True, text=True, timeout=10)
        self.assertEqual(run.returncode, 0, run.stderr + run.stdout)
        entry = json.loads(run.stdout)["mcpServers"]["local-ai-relay"]
        self.assertIn("--simple", entry["args"])
        self.assertEqual(Path(entry["args"][2]).resolve(), (self.source / ".local-dev").resolve())
        self.assertTrue((self.source / ".local-dev/mcp.json").is_file())
        self.assertEqual([p.name for p in inherited.iterdir()], ["sentinel"])
        self.assertFalse((self.source / ".relay").exists())

    def test_release_contains_runtime_and_deployment_without_local_state(self):
        with zipfile.ZipFile(self.archive) as bundle:
            names = bundle.namelist()
            self.assertEqual(len(names), len(set(names)))
            self.assertIn("local-ai-relay/deploy/.env.example", names)
            self.assertIn("local-ai-relay/Dockerfile", names)
            for name in names:
                parts = Path(name).parts
                self.assertFalse(Path(name).is_absolute())
                self.assertNotIn("..", parts)
                self.assertFalse({".relay", ".git", ".env", "__pycache__", "verification"}.intersection(parts), name)
                self.assertFalse(name.endswith((".sqlite3", ".token", ".log", ".pyc")), name)
        # The extracted copy can build itself without reaching back to checkout.
        rebuilt = self.folder / "rebuilt.zip"
        subprocess.run([sys.executable, str(self.source / "scripts/build_release.py"), "--output", str(rebuilt)],
                       cwd=self.cwd, env=self.env, check=True, capture_output=True, timeout=15)
        with zipfile.ZipFile(rebuilt) as bundle:
            self.assertEqual(bundle.namelist(), names)

    def test_unpacked_web_two_cli_members_and_durable_restart(self):
        process, url = self.start_server()
        for path, marker in (("/", b"AI"), ("/bootstrap.js", b"file:"), ("/chat.js", b"chat_join"), ("/chat.css", b"{")):
            with urlopen(url + path, timeout=5) as response:
                self.assertIn(marker, response.read())
        token = self.post(url, "/api/local-session", {}, Origin=url)["admin_token"]
        invitation = self.post(url, "/api/rooms", {"action": "create", "room": "demo"}, token)["invite"]
        for name in ("agent-a", "agent-b", "observer"):
            self.cli(name, "join", "demo", "--name", name, "--protocol", "2", "--client", "manual", "--server", url, "--invite=" + invitation)
        mids = [self.cli("agent-a", "send", "demo", "--name", "agent-a", "--server", url, "--to", "agent-b", "--text", text)["message_id"]
                for text in ("第一条约束", "第二条补充")]
        batch = self.cli("agent-b", "receive", "demo", "--name", "agent-b", "--server", url)
        self.assertEqual([m["id"] for m in batch["messages"]], mids)
        result = self.cli("agent-b", "send", "demo", "--name", "agent-b", "--server", url, "--text", "合并结果",
                          "--reply-to", mids[0], "--delivery-id", batch["delivery_id"], "--ack-ids", *mids)
        self.assertEqual(result["acknowledged_count"], 2)
        self.assertFalse(result["pending"])
        self.assertEqual(self.cli("observer", "receive", "demo", "--name", "observer", "--server", url)["messages"], [])
        answer = self.cli("agent-a", "receive", "demo", "--name", "agent-a", "--server", url)
        self.assertEqual(answer["messages"][0]["content"], "合并结果")
        self.cli("agent-a", "ack", "demo", "--name", "agent-a", "--server", url,
                 "--delivery-id", answer["delivery_id"], "--message-ids", answer["messages"][0]["id"])
        process.terminate()
        process.communicate(timeout=8)
        _, restarted_url = self.start_server(port=urlsplit(url).port)
        self.assertEqual(restarted_url, url)
        original = self.cli("agent-b", "context", "demo", "--name", "agent-b", "--server", url, "--message-id", mids[0])
        self.assertEqual(original["messages"][0]["content"], "第一条约束")
        self.assertNotIn("checkpoint", original)
        self.assertEqual(self.cli("agent-b", "receive", "demo", "--name", "agent-b", "--server", url)["messages"], [])

    def test_proxy_origin_preserved_and_local_session_not_exposed(self):
        # A server behind HTTPS receives the public Host/Origin via its proxy.
        # These requests simulate that HTTP hop, not certificate issuance.
        import socket
        with socket.socket() as reserved:
            reserved.bind(("127.0.0.1", 0))
            port = reserved.getsockname()[1]
        _, _ = self.start_server(port=port, public_url="https://chat.example.test")
        local = "http://127.0.0.1:" + str(port)
        headers = {"Host": "chat.example.test", "Origin": "https://chat.example.test", "X-Forwarded-For": "192.0.2.10"}
        with urlopen(Request(local + "/", headers=headers), timeout=5) as response:
            self.assertEqual(response.status, 200)
        with self.assertRaises(HTTPError) as denied:
            self.post(local, "/api/local-session", {}, **headers)
        self.assertEqual(denied.exception.code, 403)
        token = (self.folder / "server-data/chat-server.token").read_text().strip()
        result = self.post(local, "/api/rooms", {"action": "create", "room": "network"}, token, **headers)
        self.assertTrue(result["url"].startswith("https://chat.example.test/#"))
        headers["Origin"] = "https://unrelated.example.test"
        with self.assertRaises(HTTPError) as denied:
            self.post(local, "/api/rooms", {}, token, **headers)
        self.assertEqual(denied.exception.code, 403)

    @unittest.skipUnless(shutil.which("docker"), "Docker CLI required for Compose config validation")
    def test_compose_configuration_resolves_from_distributed_template(self):
        env = {**self.env, "CHAT_DOMAIN": "chat.example.test"}
        result = subprocess.run(["docker", "compose", "-f", str(self.source / "deploy/compose.yaml"), "config", "--format", "json"],
                                cwd=self.cwd, env=env, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        config = json.loads(result.stdout)
        relay, proxy = config["services"]["relay"], config["services"]["caddy"]
        self.assertFalse(relay.get("ports"))
        self.assertEqual(relay["command"][-1], "https://chat.example.test")
        self.assertEqual(proxy["environment"]["CHAT_DOMAIN"], "chat.example.test")
        self.assertEqual(proxy["depends_on"]["relay"]["condition"], "service_healthy")
        self.assertTrue(any(v["target"] == "/data" and v["type"] == "volume" for v in relay["volumes"]))
