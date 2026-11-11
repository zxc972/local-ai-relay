"""Local entry works without optional legacy/executor modules being importable."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
LOCAL_RUNNER = '''
import importlib.abc, sys
class NoOptionalModules(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname in {"chat_executor", "message_bus_mcp", "message_bus", "connection", "background"}:
            raise ImportError("Local commands loaded optional module: " + fullname)
sys.meta_path.insert(0, NoOptionalModules())
from relay import main
main(sys.argv[1:], local_only=True)
'''


class LocalCLITests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="airrelay local cli ")
        self.addCleanup(temporary.cleanup)
        self.data = Path(temporary.name)
        self.env = {k: v for k, v in os.environ.items()
                    if k not in ("AIRELAY_SESSION_ID", "CODEX_THREAD_ID", "AIRELAY_DATA_DIR")}

    def run_local(self, *args, stdin=None):
        return subprocess.run([sys.executable, "-c", LOCAL_RUNNER, "--data-dir", str(self.data), *args],
                              cwd=ROOT, env=self.env, input=stdin, capture_output=True,
                              text=True, encoding="utf-8", timeout=10)

    def test_local_room_and_config_work_without_optional_modules(self):
        for args in (("room", "create", "isolated"), ("room", "status", "isolated"),
                     ("mcp-config",)):
            with self.subTest(args=args):
                result = self.run_local(*args)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertNotIn("error", json.loads(result.stdout))
        config = json.loads((self.data / "mcp.json").read_text())
        self.assertIn("--simple", config["mcpServers"]["local-ai-relay"]["args"])
        requests = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
                    {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}]
        result = self.run_local("mcp", stdin="\n".join(map(json.dumps, requests)) + "\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        response = json.loads(result.stdout.splitlines()[1])
        self.assertEqual(len(response["result"]["tools"]), 9)

    def test_optional_commands_are_unavailable_before_any_side_effect(self):
        for args in (("executor",), ("safety",), ("init",), ("connect",), ("mcp", "--legacy-mailbox")):
            with self.subTest(args=args):
                result = self.run_local(*args)
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertNotIn("Local commands loaded optional module", result.stderr)
                self.assertEqual(list(self.data.iterdir()), [])

    def test_local_help_uses_the_same_parser_and_hides_data_override(self):
        result = subprocess.run([sys.executable, str(ROOT / "local_dev.py"), "--help"],
                                cwd=self.data, env=self.env, capture_output=True,
                                text=True, encoding="utf-8", timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("mcp-config,mcp,room,serve", result.stdout)
        self.assertNotIn("--data-dir", result.stdout)

    def test_abbreviated_data_flag_cannot_override_the_fixed_local_directory(self):
        result = subprocess.run([sys.executable, str(ROOT / "local_dev.py"),
                                 "--data-d", str(self.data), "room", "create", "unexpected"],
                                cwd=self.data, env=self.env, capture_output=True,
                                text=True, encoding="utf-8", timeout=10)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertEqual(list(self.data.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
