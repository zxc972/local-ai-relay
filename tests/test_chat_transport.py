"""Real broken HTTP responses must not kill an idle remote listener."""
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from chat_remote import RemoteChat, request
from core import RelayError
from transport_fixture import transport_only


class TransportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="chat transport ")
        self.addCleanup(self.temp.cleanup)
        self.responses, self.calls = [], []
        test = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                test.calls.append((self.path, json.loads(self.rfile.read(int(self.headers["Content-Length"])))))
                self.connection.sendall(test.responses.pop(0))

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop)
        self.url = "http://127.0.0.1:" + str(self.server.server_address[1])

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)

    @staticmethod
    def broken():
        return b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\nConnection: close\r\n\r\n80\r\n{"

    def test_invalid_status_line_is_a_recoverable_transport_error(self):
        self.responses.append(b"not an HTTP status\r\n\r\n")
        with self.assertRaises(RelayError) as failed:
            request(self.url, "/api/rooms/work/peek", "test-token", {})
        self.assertEqual(failed.exception.status, 503)

    def test_truncated_send_returns_uncertain_with_the_same_key_and_no_retry(self):
        self.responses.append(self.broken())
        client = RemoteChat(Path(self.temp.name), self.url, "work", "reader")
        with self.assertRaises(RelayError) as failed:
            client.send("请只发送一次", key="one-attempt")
        self.assertEqual(failed.exception.status, 503)
        self.assertIn("key=one-attempt", str(failed.exception))
        self.assertEqual(len(self.calls), 1)

    @transport_only
    def test_transport_watcher_recovers_after_truncated_response_without_model_polling(self):
        payload = json.dumps({"pending": False, "message": {"id": "one", "sender": "writer", "seq": 1}}).encode()
        self.responses.extend([self.broken(), b"HTTP/1.0 200 OK\r\nContent-Length: " + str(len(payload)).encode() + b"\r\n\r\n" + payload])
        client = RemoteChat(Path(self.temp.name), self.url, "work", "reader")
        with client.store.transaction() as db:
            db.execute("UPDATE remote_client SET joined=1,mode='host-task' WHERE id=1")

        class FastEvent(threading.Event):
            def wait(self, timeout=None):
                return super().wait(.01)

        result = client.run(client.reserve("host-task"), once=True, stopped=FastEvent())
        self.assertEqual(result["message_id"], "one")
        self.assertEqual([path for path, _ in self.calls], ["/api/rooms/work/peek"] * 2)
        self.assertEqual(client.row()["state"], "awaiting_receipt")


if __name__ == "__main__":
    unittest.main()
