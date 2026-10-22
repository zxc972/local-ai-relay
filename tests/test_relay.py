import concurrent.futures
import contextlib
import io
import json
import os
import queue
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from core import RelayError, Store
from mcp_server import serve
from relay import initialize, install_client
from web import create_server


class MailboxTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.now = [1000.0]
        self.store = Store(self.temp.name, clock=lambda: self.now[0])

    def tearDown(self):
        self.temp.cleanup()

    def task(self, key="first", **kwargs):
        return self.store.send_task("审阅任务", "验证一个修改", key, **kwargs)

    def test_roundtrip_persists_across_independent_processes_and_round_limit(self):
        task = self.task(max_rounds=2)
        other = Store(self.temp.name, clock=lambda: self.now[0])
        first = other.claim()
        self.assertEqual(first["message"]["id"], task["message_id"])
        self.assertIsNone(self.store.claim()["message"])
        reply = other.complete(task["message_id"], first["lease"], "已完成；测试通过")
        waited = self.store.wait(task["task_id"], timeout=0)
        self.assertEqual(waited["messages"][0]["id"], reply["message_id"])
        self.assertFalse(self.store.wait(task["task_id"], after_seq=waited["next_seq"], timeout=0)["messages"])
        feedback = self.store.feedback(task["task_id"], "再检查边界", reply["message_id"], "feedback-1")
        second = other.claim()
        self.assertEqual(second["message"]["id"], feedback["message_id"])
        self.assertEqual(len(second["history"]), 3)
        second_reply = other.complete(second["message"]["id"], second["lease"], "已验证边界")
        with self.assertRaises(RelayError):
            self.store.feedback(task["task_id"], "超过轮数", second_reply["message_id"], "feedback-2")
        other.close_task(task["task_id"])
        self.assertEqual(self.store.get_task(task["task_id"])["task"]["status"], "closed")

    def test_idempotent_submissions_and_results(self):
        a = self.task()
        self.assertEqual(self.task()["task_id"], a["task_id"])
        with self.assertRaises(RelayError):
            self.store.send_task("不同", "不同", "first")
        claim = self.store.claim()
        result = self.store.complete(a["message_id"], claim["lease"], "结果")
        self.assertEqual(result["message_id"], self.store.complete(a["message_id"], claim["lease"], "结果")["message_id"])
        with self.assertRaises(RelayError):
            self.store.complete(a["message_id"], claim["lease"], "另一个结果")
        f = self.store.feedback(a["task_id"], "建议", result["message_id"], "same-feedback")
        self.assertEqual(f["message_id"], self.store.feedback(a["task_id"], "建议", result["message_id"], "same-feedback")["message_id"])
        self.assertEqual(len(self.store.list_tasks()), 1)

    def test_concurrent_consumers_do_not_duplicate_delivery(self):
        self.task()
        self.task(key="second")
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: self.store.claim(), range(8)))
        self.assertEqual(sum(bool(r["message"]) for r in results), 1)

    def test_expired_lease_requires_explicit_retry_and_invalidates_old_receipt(self):
        task = self.task()
        first = self.store.claim(60)
        self.now[0] += 61
        self.assertIsNone(self.store.claim()["message"])
        self.assertEqual(self.store.get_task(task["task_id"])["task"]["status"], "stalled")
        self.store.retry(task["task_id"])
        second = self.store.claim()
        self.assertNotEqual(first["lease"], second["lease"])
        with self.assertRaises(RelayError):
            self.store.complete(task["message_id"], first["lease"], "旧结果")
        self.store.complete(task["message_id"], second["lease"], "新结果")

    def test_late_result_resolves_uncertainty_without_reexecution(self):
        task = self.task()
        first = self.store.claim(60)
        self.now[0] += 61
        self.store.list_tasks()
        self.store.complete(task["message_id"], first["lease"], "延迟回传")
        self.assertEqual(self.store.get_task(task["task_id"])["task"]["status"], "waiting_review")

    def test_close_discards_queued_task_and_preserves_running_result(self):
        first = self.task()
        claim = self.store.claim()
        self.store.close_task(first["task_id"])
        self.store.complete(first["message_id"], claim["lease"], "停止前完成的部分")
        self.assertEqual(self.store.get_task(first["task_id"])["task"]["status"], "closed")
        second = self.task(key="second")
        self.store.close_task(second["task_id"])
        self.assertIsNone(self.store.claim()["message"])

    def test_closed_expired_execution_does_not_block_later_tasks(self):
        first = self.task()
        self.store.claim(60)
        self.store.close_task(first["task_id"])
        second = self.task(key="second")
        self.now[0] += 61
        self.assertEqual(self.store.claim()["message"]["id"], second["message_id"])

    def test_renew_preserves_receipt_and_extends_lease(self):
        first = self.task()
        claim = self.store.claim(60)
        self.now[0] += 30
        self.store.renew(first["message_id"], claim["lease"])
        self.now[0] += 60
        self.assertEqual(self.store.get_task(first["task_id"])["task"]["status"], "running")

    def test_invalid_feedback_does_not_advance_round(self):
        first = self.task()
        with self.assertRaises(RelayError):
            self.store.feedback(first["task_id"], "建议", "not-a-result", "bad")
        self.assertEqual(self.store.get_task(first["task_id"])["task"]["round"], 1)
        self.assertEqual(len(self.store.get_task(first["task_id"])["messages"]), 1)

    def test_invalid_inputs_are_rejected(self):
        for invalid in [True, 0, 13, "4"]:
            with self.assertRaises(RelayError):
                self.task(max_rounds=invalid)
        with self.assertRaises(RelayError):
            self.store.send_task("", "content", "key")
        with self.assertRaises(RelayError):
            self.store.send_task("title", "content", "key", sender="codex")

    def test_blocked_outcome_is_preserved_for_peer(self):
        first = self.task()
        claim = self.store.claim()
        self.store.complete(first["message_id"], claim["lease"], "需要用户提供文件", outcome="blocked")
        self.assertEqual(self.store.wait(first["task_id"], timeout=0)["messages"][0]["outcome"], "blocked")


class InterfaceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(self.temp.name)
        self.thread_id = "12345678-1234-1234-1234-123456789abc"
        initialize(self.store, self.thread_id, "测试聊天", 1)

    def tearDown(self):
        self.temp.cleanup()

    def test_cli_claim_complete_and_binding_guard(self):
        task = self.store.send_task("本地 CLI 验证", "返回结果", "cli-task")
        cmd = [sys.executable, str(ROOT / "relay.py"), "--data-dir", self.temp.name]
        env = {**os.environ, "CODEX_THREAD_ID": self.thread_id, "PYTHONUTF8": "1"}
        bad = subprocess.run(cmd + ["bridge", "claim", "--thread-id", "wrong"], capture_output=True, text=True, encoding="utf-8", env=env)
        self.assertEqual(bad.returncode, 2)
        got = subprocess.run(cmd + ["bridge", "claim"], capture_output=True, text=True, encoding="utf-8", check=True, env=env)
        receipt = json.loads(got.stdout)
        Path(receipt["result_file"]).write_text("真正的回传", encoding="utf-8")
        subprocess.run(cmd + ["bridge", "complete", task["message_id"], "--file", receipt["result_file"]],
                       capture_output=True, text=True, encoding="utf-8", check=True, env=env)
        self.assertEqual(self.store.wait(task["task_id"], timeout=0)["messages"][0]["content"], "真正的回传")

    def test_project_install_preserves_existing_servers_and_rejects_conflicts(self):
        project = Path(self.temp.name) / "project"
        project.mkdir()
        original = {"mcpServers": {"existing": {"command": "existing-command"}}, "extra": True}
        path = project / ".mcp.json"
        path.write_text(json.dumps(original))
        install_client(self.store, project, "both")
        merged = json.loads(path.read_text(encoding="utf-8"))
        self.assertTrue(merged["extra"])
        self.assertEqual(merged["mcpServers"]["existing"], original["mcpServers"]["existing"])
        self.assertTrue((project / ".cursor/mcp.json").is_file())
        merged["mcpServers"]["local-ai-relay"] = {"command": "conflicting-command"}
        path.write_text(json.dumps(merged))
        with self.assertRaises(RelayError):
            install_client(self.store, project)
        self.assertEqual(json.loads(path.read_text(encoding="utf-8")), merged)

    def test_mcp_parse_errors_initialization_and_invalid_arguments(self):
        source = io.StringIO('\n'.join([
            "invalid json",
            json.dumps({"jsonrpc":"2.0","id":1,"method":"tools/list"}),
            json.dumps({"jsonrpc":"2.0","id":2,"method":"initialize","params":{"protocolVersion":"2025-06-18"}}),
            json.dumps({"jsonrpc":"2.0","id":3,"method":"tools/list"}),
        ]) + "\n")
        output = io.StringIO()
        serve(self.store, source, output, legacy=True)
        responses = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(responses[0]["error"]["code"], -32700)
        self.assertEqual(responses[1]["error"]["code"], -32002)
        self.assertEqual(len(responses[3]["result"]["tools"]), 6)

    def test_real_stdio_mcp_send_and_receive(self):
        process = subprocess.Popen([sys.executable, str(ROOT / "mcp_server.py"), "--data-dir", self.temp.name, "--legacy-tasks"],
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
                                   env={**os.environ, "PYTHONUTF8": "1"})
        inbox = queue.Queue()
        def read():
            for line in process.stdout:
                inbox.put(json.loads(line))
        reader = threading.Thread(target=read, daemon=True)
        reader.start()
        def call(rid, method, params):
            process.stdin.write(json.dumps({"jsonrpc":"2.0","id":rid,"method":method,"params":params}) + "\n")
            process.stdin.flush()
            response = inbox.get(timeout=5)
            self.assertEqual(response["id"], rid)
            return response["result"]
        try:
            call(1, "initialize", {"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"test","version":"1"}})
            self.assertEqual(len(call(2, "tools/list", {})["tools"]), 6)
            sent = call(3, "tools/call", {"name":"relay_send_task","arguments":{"title":"MCP 真调用","content":"检查往返","idempotency_key":"mcp-e2e"}})
            self.assertFalse(sent["isError"])
            task_id = sent["structuredContent"]["task_id"]
            claim = self.store.claim()
            self.store.complete(claim["message"]["id"], claim["lease"], "已实际回传")
            result = call(4, "tools/call", {"name":"relay_wait","arguments":{"task_id":task_id,"timeout":0}})
            self.assertEqual(result["structuredContent"]["messages"][0]["content"], "已实际回传")
            bad = call(5, "tools/call", {"name":"relay_send_task","arguments":{"sender":"codex"}})
            self.assertTrue(bad["isError"])
        finally:
            process.stdin.close()
            process.wait(timeout=5)
            reader.join(timeout=2)
            process.stdout.close()
            process.stderr.close()

    def test_http_auth_origin_host_static_and_api(self):
        server = create_server(self.store, 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_address[1]}"
        token = (self.store.root / "ui.token").read_text(encoding="utf-8").strip()
        def request(path, auth=True, extra=None, payload=None):
            headers = {"Authorization": "Bearer " + token} if auth else {}
            headers.update(extra or {})
            if payload is not None:
                headers["Content-Type"] = "application/json"
            req = urllib.request.Request(base + path, headers=headers,
                                         data=json.dumps(payload).encode() if payload is not None else None)
            return urllib.request.urlopen(req, timeout=5)
        try:
            with request("/", auth=False) as response:
                self.assertIn("当前 Codex 聊天", response.read().decode())
                self.assertIn("frame-ancestors 'none'", response.headers["Content-Security-Policy"])
            for params, expected in [({"auth":False},401), ({"extra":{"Origin":"https://evil.example"}},403), ({"extra":{"Host":"evil.example"}},403)]:
                with self.assertRaises(urllib.error.HTTPError) as error:
                    request("/api/state", **params)
                self.assertEqual(error.exception.code, expected)
            with request("/api/tasks", payload={"title":"网页任务","content":"测试","idempotency_key":"http-task"}) as response:
                task = json.loads(response.read())
            with request("/api/tasks/" + task["task_id"]) as response:
                self.assertEqual(json.loads(response.read())["messages"][0]["sender"], "user")
            with request("/api/setup") as response:
                self.assertIn("local-ai-relay", json.loads(response.read())["mcp"]["mcpServers"])
            with self.assertRaises(urllib.error.HTTPError) as error:
                request("/api/tasks", payload=[])
            self.assertEqual(error.exception.code, 400)
        finally:
            server.shutdown()
            thread.join(timeout=2)
            server.server_close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
