"""Loopback-only dashboard; never runs commands or calls an AI provider."""
from __future__ import annotations

import json
import secrets
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

if __package__:
    from .core import RelayError
else:
    from core import RelayError

STATIC = Path(__file__).resolve().parent / "static"


def create_server(store, port=8765):
    token = (store.root / "ui.token").read_text(encoding="utf-8").strip()

    class Handler(BaseHTTPRequestHandler):
        server_version = "LocalAIRelay/1.0"

        def setup(self):
            super().setup()
            self.connection.settimeout(10)

        def log_message(self, format, *args):
            # Do not put task text, credentials, or URLs in console logs.
            pass

        def respond(self, status, value, content_type="application/json; charset=utf-8"):
            body = value if isinstance(value, bytes) else json.dumps(value, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
            self.end_headers()
            self.wfile.write(body)

        def allowed(self):
            actual_port = self.server.server_address[1]
            hosts = {f"127.0.0.1:{actual_port}", f"localhost:{actual_port}"}
            if self.headers.get("Host") not in hosts:
                self.respond(403, {"error": "Host not allowed"})
                return False
            origin = self.headers.get("Origin")
            if origin and origin not in {"http://" + host for host in hosts}:
                self.respond(403, {"error": "Origin not allowed"})
                return False
            return True

        def authorize(self):
            supplied = self.headers.get("Authorization", "")
            if not secrets.compare_digest(supplied, "Bearer " + token):
                self.respond(401, {"error": "请使用本机访问密钥登录"})
                return False
            return True

        def do_GET(self):
            if not self.allowed():
                return
            path = urlsplit(self.path).path
            assets = {"/": ("index.html", "text/html; charset=utf-8"),
                      "/app.js": ("app.js", "application/javascript; charset=utf-8"),
                      "/style.css": ("style.css", "text/css; charset=utf-8")}
            if path in assets:
                filename, mime = assets[path]
                self.respond(200, (STATIC / filename).read_bytes(), mime)
                return
            if path == "/health":
                self.respond(200, {"ok": True, "service": "local-ai-relay"})
                return
            if not self.authorize():
                return
            try:
                if path == "/api/state":
                    self.respond(200, store.state())
                elif path == "/api/setup":
                    self.respond(200, {"mcp": json.loads((store.root / "client.mcp.json").read_text(encoding="utf-8")),
                                       "instructions": (store.root / "other-ai.md").read_text(encoding="utf-8")})
                elif path.startswith("/api/tasks/") and len(path.split("/")) == 4:
                    self.respond(200, store.get_task(path.split("/")[-1]))
                else:
                    self.respond(404, {"error": "Not found"})
            except RelayError as error:
                self.respond(error.status, {"error": str(error)})

        def do_POST(self):
            if not self.allowed() or not self.authorize():
                return
            try:
                if self.headers.get_content_type() != "application/json":
                    raise RelayError("Content-Type must be application/json", 415)
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                except ValueError:
                    raise RelayError("Invalid Content-Length")
                if not 0 < length <= 1_000_000:
                    raise RelayError("请求体大小须在 1–1000000 字节", 413)
                try:
                    data = json.loads(self.rfile.read(length))
                except (ValueError, UnicodeError):
                    raise RelayError("Invalid JSON")
                if not isinstance(data, dict):
                    raise RelayError("请求体必须是 JSON 对象")
                path = urlsplit(self.path).path
                if path == "/api/tasks":
                    result = store.send_task(data.get("title"), data.get("content"), data.get("idempotency_key"),
                                             data.get("max_rounds", 4), sender="user")
                elif path.startswith("/api/tasks/") and len(path.split("/")) == 5:
                    task_id, action = path.split("/")[3:]
                    if action == "feedback":
                        result = store.feedback(task_id, data.get("content"), data.get("reply_to"),
                                                data.get("idempotency_key"), sender="user")
                    elif action == "close":
                        result = store.close_task(task_id)
                    elif action == "retry":
                        result = store.retry(task_id)
                    else:
                        raise RelayError("Not found", 404)
                else:
                    raise RelayError("Not found", 404)
                self.respond(200, result)
            except RelayError as error:
                self.respond(error.status, {"error": str(error)})
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception:
                self.respond(500, {"error": "中转站内部错误"})

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    return server
