"""Build and install the distributable without consulting a package index."""
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import venv
import zipfile
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
BUILD_TOOLS = all(importlib.util.find_spec(name) for name in ("pip", "setuptools", "wheel"))


@unittest.skipUnless(BUILD_TOOLS, "Install setuptools and wheel to run installed-package checks")
class InstalledPackageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix="airelay install space ")
        cls.addClassCleanup(cls.temp.cleanup)
        cls.folder = Path(cls.temp.name)
        source = cls.folder / "source"
        source.mkdir()
        for path in ROOT.glob("*.py"):
            shutil.copy2(path, source / path.name)
        for name in ("pyproject.toml", "MANIFEST.in", "README.md", "LICENSE", "CONTRIBUTING.md"):
            shutil.copy2(ROOT / name, source / name)
        for name in ("static", "bin", "extensions", "examples"):
            if (ROOT / name).exists():
                shutil.copytree(ROOT / name, source / name)
        cls.env = {key: value for key, value in os.environ.items()
                   if key not in ("PYTHONPATH", "CODEX_THREAD_ID", "AIRELAY_SESSION_ID")}
        cls.env.update({"AIRELAY_DATA_DIR": str(cls.folder / "user data"),
                        "PYTHONUTF8": "1", "PIP_DISABLE_PIP_VERSION_CHECK": "1"})
        build = subprocess.run(
            [sys.executable, "-m", "pip", "wheel", "--no-build-isolation", "--no-deps",
             "--no-index", "--wheel-dir", str(cls.folder / "wheels"), str(source)],
            cwd=cls.folder, env=cls.env, text=True, encoding="utf-8", capture_output=True, timeout=90)
        if build.returncode:
            raise AssertionError(build.stdout + build.stderr)
        wheels = list((cls.folder / "wheels").glob("*.whl"))
        if len(wheels) != 1:
            raise AssertionError(f"Expected one wheel, got {wheels}")
        cls.wheel = wheels[0]
        environment = cls.folder / "isolated environment"
        venv.EnvBuilder(with_pip=True).create(environment)
        scripts = environment / ("Scripts" if os.name == "nt" else "bin")
        cls.python = scripts / ("python.exe" if os.name == "nt" else "python")
        cls.console = scripts / ("airelay.exe" if os.name == "nt" else "airelay")
        install = subprocess.run(
            [str(cls.python), "-m", "pip", "install", "--no-index", "--no-deps", str(cls.wheel)],
            cwd=cls.folder, env=cls.env, text=True, encoding="utf-8", capture_output=True, timeout=90)
        if install.returncode:
            raise AssertionError(install.stdout + install.stderr)
        # Remove the staged source before running any installed entry points.
        shutil.rmtree(source)
        cls.cwd = cls.folder / "unrelated cwd"
        cls.cwd.mkdir()

    def run_process(self, command, input=None, env=None):
        process = subprocess.run(command, cwd=self.cwd, env=env or self.env, input=input,
                                 text=True, encoding="utf-8", capture_output=True, timeout=15)
        self.assertEqual(process.returncode, 0, process.stdout + process.stderr)
        return process

    def test_console_and_module_work_outside_checkout(self):
        self.assertTrue(self.console.is_file())
        help_text = self.run_process([str(self.console), "--help"]).stdout
        self.assertIn("mcp-config", help_text)
        self.assertIn("wake", help_text)
        self.assertIn("events", help_text)
        self.assertIn("create,join,send,receive,ack,watch,wait,status,leave", self.run_process([str(self.console)]).stdout)
        status = self.run_process([str(self.python), "-m", "local_ai_relay", "status", "install-check"])
        self.assertEqual(json.loads(status.stdout)["room"], "install-check")
        self.assertTrue((self.folder / "user data" / "rooms" / "install-check" / "relay.sqlite3").exists())
        for arguments in (("create", "installed-chat"), ("join", "installed-chat", "--name", "reader"),
                          ("join", "installed-chat", "--name", "writer"),
                          ("send", "installed-chat", "--name", "writer", "--text", "安装后消息")):
            self.run_process([str(self.console), "room", *arguments])
        received = self.run_process([str(self.console), "room", "wait", "installed-chat", "--name", "reader",
                                     "--after-seq", "0", "--timeout", "0"])
        self.assertEqual(json.loads(received.stdout)["messages"][0]["content"], "安装后消息")

    def test_generated_mcp_launch_uses_installed_files(self):
        config = json.loads(self.run_process([str(self.console), "mcp-config"]).stdout)
        server = config["mcpServers"]["local-ai-relay"]
        requests = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                "protocolVersion": "2025-06-18", "capabilities": {},
                "clientInfo": {"name": "installed-smoke", "version": "1"}}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        ]
        response = self.run_process([server["command"], *server["args"]],
                                    input="".join(json.dumps(r) + "\n" for r in requests))
        packets = {p["id"]: p for p in map(json.loads, response.stdout.splitlines()) if "id" in p}
        self.assertEqual(packets[1]["result"]["serverInfo"]["version"], "0.3.0")
        names = {tool["name"] for tool in packets[2]["result"]["tools"]}
        self.assertFalse({"relay_connect", "relay_send", "relay_reply", "relay_status"} & names)
        self.assertTrue({"chat_request_action", "chat_execute_action"} <= names)
        self.assertTrue({"chat_create", "chat_join", "chat_send", "chat_receive", "chat_ack",
                         "chat_wait", "chat_status", "chat_leave"} <= names)

    def test_installed_background_workers_notify_both_roles(self):
        log = self.folder / "wake events.jsonl"
        handler = self.folder / "fake host.py"
        handler.write_text(
            "import json, sys\n"
            "event = json.load(sys.stdin)\n"
            "with open(sys.argv[1], 'a', encoding='utf-8') as output:\n"
            "    output.write(json.dumps(event) + '\\n')\n", encoding="utf-8")
        config = self.folder / "fake host.json"
        config.write_text(json.dumps({"type": "command", "argv": [
            str(self.python), str(handler), str(log)], "timeout": 5}), encoding="utf-8")

        def cli(*args):
            return json.loads(self.run_process([str(self.console), *args]).stdout)

        def events_until(count):
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if log.exists():
                    lines = log.read_text(encoding="utf-8").splitlines()
                    if len(lines) >= count:
                        return [json.loads(line) for line in lines]
                time.sleep(.05)
            self.fail(f"Installed background worker did not deliver {count} events")

        try:
            for role in ("sender", "receiver"):
                cli("connect", "installed-workers", "--as", role, "--wake-config", str(config))
                self.assertTrue(cli("wake", "status", "installed-workers", "--as", role)["running"])
            time.sleep(.3)
            self.assertFalse(log.exists(), "Idle workers must not invoke host handlers")
            command = cli("send", "installed-workers", "--as", "sender", "--text", "installed smoke")
            first = events_until(1)[0]
            self.assertEqual((first["role"], first["message_id"]), ("receiver", command["message_id"]))
            self.run_process(first["receive_argv"])
            result = cli("send", "installed-workers", "--as", "receiver", "--reply-to",
                         command["message_id"], "--text", "verified")
            second = events_until(2)[1]
            self.assertEqual((second["role"], second["message_id"]), ("sender", result["message_id"]))
            self.run_process(second["receive_argv"])
            cli("ack", "installed-workers", result["message_id"], "--as", "sender")
        finally:
            for role in ("sender", "receiver"):
                cli("wake", "stop", "installed-workers", "--as", role)
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if all(not cli("wake", "status", "installed-workers", "--as", role)["worker_active"]
                       for role in ("sender", "receiver")):
                    break
                time.sleep(.05)
            else:
                self.fail("Installed background workers did not stop")

    def test_wheel_includes_resources_but_no_private_data(self):
        with zipfile.ZipFile(self.wheel) as archive:
            names = set(archive.namelist())
        self.assertIn("local_ai_relay/static/index.html", names)
        self.assertIn("local_ai_relay/static/style.css", names)
        self.assertIn("local_ai_relay/bin/airelay", names)
        self.assertIn("local_ai_relay/chat_background.py", names)
        for resource in ("chat_server.py", "chat_remote.py", "static/chat.html", "static/chat.js", "static/chat.css"):
            self.assertIn("local_ai_relay/" + resource, names)
        self.assertFalse(any("/.relay/" in name or name.endswith((".sqlite3", ".token")) for name in names))
        script = "from importlib.resources import files; print(files('local_ai_relay').joinpath('static/index.html').is_file())"
        self.assertEqual(self.run_process([str(self.python), "-c", script]).stdout.strip(), "True")

    def test_installed_chat_rejects_callback_and_manual_receive_reply_works(self):
        log = self.folder / "chat wakes.jsonl"
        handler = self.folder / "chat host.py"
        handler.write_text("import json, sys\n"
                           "event = json.load(sys.stdin)\n"
                           "with open(sys.argv[1], 'a', encoding='utf-8') as out:\n"
                           "    out.write(json.dumps(event) + '\\n')\n", encoding="utf-8")
        config = self.folder / "chat host.json"
        config.write_text(json.dumps({"type": "command", "argv": [str(self.python), str(handler), str(log)],
                                      "timeout": 3}), encoding="utf-8")
        def cli(*args):
            return json.loads(self.run_process([str(self.console), "room", *args]).stdout)
        # Explicitly protect this fixture: local automatic behavior has separate coverage.
        Path(self.env["AIRELAY_DATA_DIR"]).mkdir(parents=True, exist_ok=True)
        (Path(self.env["AIRELAY_DATA_DIR"]) / "network-access.json").write_text("{}")
        cli("create", "installed-bg-chat")
        cli("join", "installed-bg-chat", "--name", "writer", "--client", "manual")
        try:
            joined = cli("join", "installed-bg-chat", "--name", "reader", "--wake-config", str(config))
            self.assertFalse(joined["background"]["running"], joined)
            self.assertEqual(joined["background"]["state"], "manual_only")
            self.assertIsNone(joined["next_tool"])
            time.sleep(.3)
            self.assertFalse(log.exists())
            sent = cli("send", "installed-bg-chat", "--name", "writer", "--to", "reader", "--text", "installed hello")
            self.assertFalse(log.exists(), "Unverified callback must never execute")
            received = cli("receive", "installed-bg-chat", "--name", "reader")
            self.assertEqual(received["messages"][0]["id"], sent["message_id"])
            reply = cli("send", "installed-bg-chat", "--name", "reader", "--reply-to", sent["message_id"],
                        "--text", "installed reply")
            self.assertTrue(reply["acknowledged"])
            time.sleep(.3)
            self.assertFalse(log.exists())
        finally:
            cli("leave", "installed-bg-chat", "--name", "reader")
            cli("leave", "installed-bg-chat", "--name", "writer")
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if all(not m["background"]["worker_active"] for m in cli("status", "installed-bg-chat")["members"]):
                    break
                time.sleep(.025)
            else:
                self.fail("Installed chat worker did not stop")

    def test_default_storage_is_outside_installed_package(self):
        script = (
            "import json; import local_ai_relay.core as c; "
            "print(json.dumps({'data':str(c.DEFAULT_DATA),'module':c.__file__}))")
        env = {key: value for key, value in self.env.items() if key != "AIRELAY_DATA_DIR"}
        paths = json.loads(self.run_process([str(self.python), "-c", script], env=env).stdout)
        package = Path(paths["module"]).parent.resolve()
        self.assertNotIn(package, Path(paths["data"]).resolve().parents)
        self.assertNotEqual(package, Path(paths["data"]).resolve())

    def test_installed_network_server_clients_reject_native_watcher(self):
        data = self.folder / "network-server"
        data.mkdir(exist_ok=True)
        (data / "network-access.json").write_text("{}")
        log = self.folder / "network-server.log"
        with log.open("w") as output:
            process = subprocess.Popen([str(self.console), "--data-dir", str(data), "serve", "--port", "0"],
                                       cwd=self.cwd, env=self.env, stdout=output, stderr=output)
        watcher = None
        try:
            deadline = time.monotonic() + 5
            startup = None
            while time.monotonic() < deadline:
                try:
                    startup = json.loads(log.read_text(encoding="utf-8"))
                    break
                except ValueError:
                    time.sleep(.025)
            self.assertIsNotNone(startup, log.read_text())
            url = urlsplit(startup["url"])
            base = url.scheme + "://" + url.netloc
            self.assertEqual(url.fragment, "")
            session_req = Request(base + "/api/local-session", data=b"{}",
                                  headers={"Content-Type": "application/json", "Origin": base})
            with urlopen(session_req, timeout=3) as response:
                token = json.load(response)["admin_token"]
            req = Request(base + "/api/rooms", data=json.dumps({"action": "create", "room": "installed-network"}).encode(),
                          headers={"Content-Type": "application/json", "Authorization": "Bearer " + token})
            with urlopen(req, timeout=3) as response:
                invite = json.load(response)["invite"]
            with urlopen(base + "/chat.js", timeout=3) as response:
                self.assertIn("chat_join", response.read().decode())
            def argv(name, action, *extra):
                return [str(self.console), "--data-dir", str(self.folder / ("network-" + name)), "room", action,
                        "installed-network", "--name", name, "--server", base, *extra]
            def cli(name, action, *extra):
                return json.loads(self.run_process(argv(name, action, *extra)).stdout)
            cli("writer", "join", "--invite=" + invite, "--client", "manual")
            cli("reader", "join", "--invite=" + invite, "--client", "claude")
            watcher = subprocess.Popen(argv("reader", "watch"), cwd=self.cwd, env=self.env,
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            sent = cli("writer", "send", "--text", "installed network hello")
            output, error = watcher.communicate(timeout=6)
            self.assertEqual(watcher.returncode, 2, error)
            self.assertIn("手动收件", output)
            packet = cli("reader", "receive")
            self.assertEqual(packet["messages"][0]["content"], "installed network hello")
            self.assertTrue(cli("reader", "send", "--text", "ok", "--reply-to", sent["message_id"])["acknowledged"])
            cli("reader", "leave"); cli("writer", "leave")
        finally:
            if watcher and watcher.poll() is None:
                watcher.terminate(); watcher.communicate(timeout=5)
            process.terminate(); process.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()
