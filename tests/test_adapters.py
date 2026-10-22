"""Adapter checks use fake host processes only; no models or real messages."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import adapters


class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="relay adapter 空间 ")
        self.folder = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def script(self, name, content):
        path = self.folder / name
        path.write_text(content, encoding="utf-8")
        return str(path)

    def test_command_receives_exact_json_on_stdin_without_shell_interpolation(self):
        script = self.script("fake host.py", "import json, pathlib, sys\n"
                             "data=json.load(sys.stdin)\n"
                             "pathlib.Path('receipt.json').write_text(json.dumps({'event':data,'argv':sys.argv[1:]},ensure_ascii=False),encoding='utf-8')\n")
        dangerous = "$(touch SHOULD_NOT_EXIST); `touch SHOULD_NOT_EXIST`; %PATH% & 中文"
        config = {"type": "command", "argv": [sys.executable, script, dangerous], "cwd": str(self.folder)}
        event = {"room": "work", "role": "receiver", "message_id": "event-1", "content": dangerous}
        result = adapters.deliver(config, event)
        self.assertTrue(result["accepted"], result)
        self.assertEqual(result["status"], "accepted")
        self.assertEqual(json.loads((self.folder / "receipt.json").read_text(encoding="utf-8")),
                         {"event": event, "argv": [dangerous]})
        self.assertFalse((self.folder / "SHOULD_NOT_EXIST").exists())

    def test_started_process_failure_is_uncertain_even_after_receipt(self):
        script = self.script("partial.py", "import pathlib, sys\n"
                             "pathlib.Path('received').write_text(sys.stdin.read())\nsys.exit(3)\n")
        result = adapters.deliver({"type": "command", "argv": [sys.executable, script],
                                   "cwd": str(self.folder)}, {"message_id": "one"})
        self.assertTrue((self.folder / "received").exists())
        self.assertFalse(result["accepted"])
        self.assertEqual(result["status"], "uncertain")
        self.assertEqual(result["exit_code"], 3)
        self.assertFalse(result["retry_safe"])

    def test_timeout_is_uncertain_and_missing_executable_failed(self):
        script = self.script("slow.py", "import time\ntime.sleep(5)\n")
        result = adapters.deliver({"type": "command", "argv": [sys.executable, script], "timeout": .05}, {})
        self.assertEqual(result["status"], "uncertain")
        result = adapters.deliver({"type": "command", "argv": [str(self.folder / "absent")]}, {})
        self.assertEqual(result["status"], "failed")

    def test_doctor_does_not_execute_custom_command(self):
        script = self.script("mutating.py", "raise RuntimeError('must not run')\n")
        with mock.patch.object(adapters.subprocess, "run", side_effect=AssertionError("must not run")):
            result = adapters.doctor({"type": "command", "argv": [sys.executable, script], "cwd": str(self.folder)})
        self.assertTrue(result["ready"])
        self.assertFalse(result["delivery_tested"])
        invalid = adapters.doctor({"type": "command", "argv": [sys.executable], "cwd": str(self.folder / "absent")})
        self.assertFalse(invalid["ready"])

    def test_codex_uses_existing_thread_safe_argv_and_redacts_auth(self):
        config = {"type": "codex", "thread_id": "11111111-2222-4333-8444-555555555555",
                  "binary": r"C:\Program Files\Codex\codex.exe", "remote": "wss://host.example/",
                  "remote_auth_token_env": "RELAY_TEST_TOKEN"}
        event = {"role": "sender", "message_id": "one", "content": "新结果 $(touch nope)"}
        completed = subprocess.CompletedProcess([], 0, stdout="queued secret-token", stderr="")
        with mock.patch.dict(os.environ, {"RELAY_TEST_TOKEN": "secret-token"}), \
                mock.patch.object(adapters, "_executable", side_effect=lambda p: p), \
                mock.patch.object(adapters.socket, "create_connection", return_value=mock.MagicMock()), \
                mock.patch.object(adapters.subprocess, "run", return_value=completed) as run:
            result = adapters.deliver(config, event)
        args, options = run.call_args
        argv = args[0]
        self.assertEqual(argv[:4], [config["binary"], "queue", "--thread", config["thread_id"]])
        self.assertEqual(json.loads(argv[5].split("\n", 1)[1]), event)
        self.assertEqual(argv[-4:], ["--remote", config["remote"], "--remote-auth-token-env", "RELAY_TEST_TOKEN"])
        self.assertFalse(options["shell"])
        self.assertNotIn("secret-token", json.dumps(result))
        self.assertTrue(result["accepted"])
        self.assertNotIn("resume", argv)
        self.assertNotIn("thread/start", argv)

    def test_codex_doctor_only_queries_capabilities_and_running_daemon(self):
        commands = []
        def fake_run(argv, **kwargs):
            commands.append(argv)
            failed = argv[1:] == ["app-server", "daemon", "version"]
            return subprocess.CompletedProcess(argv, 1 if failed else 0,
                                               stdout="codex 0.163.0" if not failed else "",
                                               stderr="control socket absent" if failed else "")
        with mock.patch.object(adapters, "_executable", side_effect=lambda p: p), \
                mock.patch.object(adapters.subprocess, "run", side_effect=fake_run):
            result = adapters.doctor({"type": "codex", "thread_id": "existing", "binary": "fake-codex"})
        self.assertFalse(result["ready"])
        self.assertFalse(result["delivery_tested"])
        self.assertEqual([c[1:] for c in commands], [["--version"], ["queue", "--help"], ["app-server", "daemon", "version"]])
        self.assertNotIn("--message", repr(commands))

    def test_codex_does_not_queue_when_existing_endpoint_is_absent(self):
        commands = []
        def fake_run(argv, **kwargs):
            commands.append(argv)
            failed = argv[1:] == ["app-server", "daemon", "version"]
            return subprocess.CompletedProcess(argv, 1 if failed else 0, stdout="help", stderr="missing" if failed else "")
        with mock.patch.object(adapters, "_executable", side_effect=lambda p: p), \
                mock.patch.object(adapters.subprocess, "run", side_effect=fake_run):
            result = adapters.deliver({"type": "codex", "thread_id": "existing", "binary": "fake-codex"}, {})
        self.assertEqual(result["status"], "failed")
        self.assertNotIn("--message", repr(commands))

    def test_invalid_config_and_event_never_launch_process(self):
        configs = [
            {"type": "command", "argv": "echo hi"},
            {"type": "command", "argv": [sys.executable], "timeout": float("inf")},
            {"type": "command", "argv": [sys.executable], "timeout": 301},
            {"type": "codex", "thread_id": ""},
            {"type": "codex", "thread_id": "existing", "remote": "https://example.com/"},
            {"type": "codex", "thread_id": "existing", "remote": "wss://user:pass@example.com/"},
            {"type": "codex", "thread_id": "existing", "remote": "ws://localhost:999999/"},
        ]
        with mock.patch.object(adapters.subprocess, "run", side_effect=AssertionError("must not run")):
            for config in configs:
                self.assertEqual(adapters.deliver(config, {})["status"], "failed", config)
            self.assertEqual(adapters.deliver({"type": "command", "argv": [sys.executable]}, {"not_json": object()})["status"], "failed")

    def test_windows_native_path_and_wrapper_validation(self):
        config = adapters.normalize_config({"type": "codex-queue", "thread_id": "existing",
                                            "binary": r"C:\Program Files\Codex\codex.exe"})
        self.assertEqual(config["type"], "codex")
        self.assertEqual(config["binary"], r"C:\Program Files\Codex\codex.exe")
        # Patch the narrow OS test without creating WindowsPath objects on Unix.
        with mock.patch.object(adapters.os, "name", "nt"), \
                mock.patch.object(adapters, "Path", side_effect=lambda value: mock.Mock(suffix=".cmd")), \
                mock.patch.object(adapters.subprocess, "run", side_effect=AssertionError("must not run")):
            result = adapters.deliver({"type": "command", "argv": ["codex.cmd"]}, {})
        self.assertEqual(result["status"], "failed")


if __name__ == "__main__":
    unittest.main()
