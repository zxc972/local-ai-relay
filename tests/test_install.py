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
        status = self.run_process([str(self.python), "-m", "local_ai_relay", "status", "install-check"])
        self.assertEqual(json.loads(status.stdout)["room"], "install-check")
        self.assertTrue((self.folder / "user data" / "rooms" / "install-check" / "relay.sqlite3").exists())

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
        self.assertTrue({"relay_connect", "relay_send", "relay_reply", "relay_status"} <= names)

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
        self.assertFalse(any("/.relay/" in name or name.endswith((".sqlite3", ".token")) for name in names))
        script = "from importlib.resources import files; print(files('local_ai_relay').joinpath('static/index.html').is_file())"
        self.assertEqual(self.run_process([str(self.python), "-c", script]).stdout.strip(), "True")

    def test_default_storage_is_outside_installed_package(self):
        script = (
            "import json; import local_ai_relay.core as c; "
            "print(json.dumps({'data':str(c.DEFAULT_DATA),'module':c.__file__}))")
        env = {key: value for key, value in self.env.items() if key != "AIRELAY_DATA_DIR"}
        paths = json.loads(self.run_process([str(self.python), "-c", script], env=env).stdout)
        package = Path(paths["module"]).parent.resolve()
        self.assertNotIn(package, Path(paths["data"]).resolve().parents)
        self.assertNotEqual(package, Path(paths["data"]).resolve())


if __name__ == "__main__":
    unittest.main()
