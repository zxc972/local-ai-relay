"""Each MCP profile loads only its own implementation, in a fresh process."""
import json
from pathlib import Path
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]

PROBE = r'''
import io, json, sys, tempfile
sys.path.insert(0, sys.argv[1])
from core import Store
from mcp_server import serve
mode = sys.argv[2]
requests = [{"jsonrpc":"2.0", "id":1, "method":"initialize", "params":{
    "protocolVersion":"2025-06-18", "clientInfo":{"name":"loading-test"}}},
    {"jsonrpc":"2.0", "id":2, "method":"tools/list"}]
output = io.StringIO()
with tempfile.TemporaryDirectory(prefix="mcp-loading-") as data:
    serve(Store(data), io.StringIO("\n".join(map(json.dumps, requests))), output,
          legacy=mode=="tasks", legacy_mailbox=mode=="mailbox", simple=mode=="simple")
packets = [json.loads(line) for line in output.getvalue().splitlines()]
assert len(packets) == 2 and all("result" in packet for packet in packets)
modules = {"chatroom_mcp", "message_bus_mcp", "message_bus", "connection", "background", "chat_executor"}
print(json.dumps({"tools":len(packets[1]["result"]["tools"]),
                  "loaded":sorted(modules.intersection(sys.modules))}))
'''


class MCPLoadingTests(unittest.TestCase):
    def test_profiles_start_without_loading_unrelated_endpoints(self):
        mailbox = ["background", "connection", "message_bus", "message_bus_mcp"]
        for mode, count, loaded in (("simple", 9, ["chatroom_mcp"]),
                                    ("chat", 13, ["chatroom_mcp"]),
                                    ("mailbox", 8, mailbox), ("tasks", 6, [])):
            with self.subTest(mode=mode):
                run = subprocess.run([sys.executable, "-I", "-B", "-c", PROBE, str(ROOT), mode],
                                     capture_output=True, text=True, timeout=10)
                self.assertEqual(run.returncode, 0, run.stderr)
                self.assertEqual(json.loads(run.stdout), {"tools": count, "loaded": loaded})


if __name__ == "__main__":
    unittest.main()
