"""Small authenticated chat server. SQLite and HTTP only; never invokes an AI."""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import secrets
import shlex
import ssl
import subprocess
import sys
import threading
import time
import unicodedata
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlencode, urlsplit

if __package__:
    from .chat_delivery import COALESCE_SECONDS, ids
    from .chatroom import ChatRoom, MAX_MEMBERS, member_name
    from .chat_safety import local_trusted
    from .core import RelayError, Store, integer, private_write
    from .room_cli import ROOM_PATTERN, room_name
else:
    from chat_delivery import COALESCE_SECONDS, ids
    from chatroom import ChatRoom, MAX_MEMBERS, member_name
    from chat_safety import local_trusted
    from core import RelayError, Store, integer, private_write
    from room_cli import ROOM_PATTERN, room_name

STATIC = Path(__file__).resolve().parent / "static"
DEFAULT_PORT = 8789
HISTORY_BYTE_LIMIT = 1_000_000
COLLABORATION_VISIBLE = "(recipient IS NULL OR (sender_kind='agent' AND recipient_kind='agent'))"


def server_url(value):
    try:
        url = urlsplit(value)
        port = url.port
    except (ValueError, TypeError):
        raise RelayError("服务地址无效")
    if (url.scheme not in ("http", "https") or not url.hostname or url.username or url.password
            or url.query or url.fragment or url.path not in ("", "/")):
        raise RelayError("服务地址须为 http(s)://主机:端口，不包含密码、路径或参数")
    return url.scheme + "://" + url.netloc.lower()


def owner_for(token):
    if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", token):
        raise RelayError("成员凭据无效", 401)
    return "network:" + hashlib.sha256(token.encode()).hexdigest()


def wire(result):
    # Do not expose server-side paths, adapter configuration, or local commands.
    keys = {"room", "name", "status", "messages", "message_id", "seq", "to", "duplicate",
            "acknowledged", "next_after_seq", "pending_through_seq", "joined", "member_id", "membership_epoch",
            "protocol_version", "delivery_id", "message_ids", "pending_message_ids", "remaining", "pending", "budget", "instructions", "acknowledged_count"}
    return {key: value for key, value in result.items() if key in keys}


class ChatService:
    def __init__(self, data_dir, public_url=f"http://127.0.0.1:{DEFAULT_PORT}"):
        self.store = Store(data_dir)
        self.public_url = server_url(public_url)
        if __package__:
            from .chat_safety import loopback_server, protect_network
        else:
            from chat_safety import loopback_server, protect_network
        if not loopback_server(self.public_url):
            protect_network(self.store.root)
        path = self.store.root / "chat-server.token"
        try:
            private_write(path, secrets.token_urlsafe(32))
        except FileExistsError:
            pass
        self.admin_token = path.read_text(encoding="utf-8").strip()
        self.lock = threading.RLock()
        self.rooms = {}
        self.changed = threading.Event()
        self.stopping = threading.Event()

    def room(self, name, create=False, include_deleted=False):
        name = room_name(name)
        with self.lock:
            if name not in self.rooms:
                if not create and not (self.store.root / "rooms" / name / "relay.sqlite3").is_file():
                    raise RelayError("聊天室不存在", 404)
                chat = ChatRoom(self.store.root, name)
                chat.background
                with contextlib.closing(chat.store.connect()) as db:
                    db.execute("CREATE TABLE IF NOT EXISTS chat_network_seen "
                               "(name TEXT PRIMARY KEY, seen REAL NOT NULL, state TEXT NOT NULL, watching_until REAL)")
                    if "watching_until" not in {r["name"] for r in db.execute("PRAGMA table_info(chat_network_seen)")}:
                        db.execute("ALTER TABLE chat_network_seen ADD COLUMN watching_until REAL")
                self.rooms[name] = chat
            chat = self.rooms[name]
            if create:
                chat.create()
            with chat.store.transaction() as db:
                chat.require_room(db, include_deleted=include_deleted)
                db.execute("INSERT OR IGNORE INTO meta VALUES('chat:invite',?)", (secrets.token_urlsafe(32),))
            return chat

    def is_admin(self, token):
        return bool(token and secrets.compare_digest(token, self.admin_token))

    def invitation(self, chat):
        with contextlib.closing(chat.store.connect()) as db:
            token = db.execute("SELECT value FROM meta WHERE key='chat:invite'").fetchone()[0]
        return {"invite": token, "url": self.public_url + "/#" + urlencode({"room": chat.name, "invite": token})}

    def allowed_invite(self, chat, token):
        return self.is_admin(token) or secrets.compare_digest(token, self.invitation(chat)["invite"])

    def identity(self, chat, token, state=None):
        owner = owner_for(token)
        with chat.store.transaction() as db:
            row = db.execute("SELECT name FROM chat_members WHERE owner=? AND active=1", (owner,)).fetchone()
            if not row:
                raise RelayError("成员凭据失效或已离开，请重新加入", 401)
            if state:
                db.execute("INSERT INTO chat_network_seen(name,seen,state) VALUES(?,?,?) ON CONFLICT(name) DO UPDATE SET "
                           "seen=excluded.seen,state=excluded.state", (row["name"], time.time(), state))
        return row["name"], owner

    def snapshot(self, chat, include_deleted=False):
        value = chat.status(include_deleted=include_deleted)
        with contextlib.closing(chat.store.connect()) as db:
            seen = {r["name"]: r for r in db.execute("SELECT * FROM chat_network_seen")}
            identities = {r["name"]: r for r in db.execute("SELECT name,owner,protocol_version,pending_seq FROM chat_members")}
        members = []
        for member in value["members"]:
            if not member["joined"]:
                continue
            state = member["state"]
            listening = member["listening"]
            identity = identities[member["name"]]
            network = identity["owner"].startswith("network:")
            if network and not member.get("executor_reported"):
                last = seen.get(member["name"])
                online = bool(last and last["seen"] > time.time() - 45)
                listening = bool(chat.trusted_local() and online and (last["watching_until"] or 0) > time.time())
                state = (last["state"] if online else "offline") if chat.trusted_local() else "manual_only"
                if online and last["state"] == "viewing":
                    state = "viewing"
                if online and identity["pending_seq"] is not None and last["state"] not in ("recovery_wait", "recovering", "needs_attention"):
                    state = "processing"
            if value["room_state"] != "open":
                state, listening = "room_" + value["room_state"], False
            members.append({"name": member["name"], "state": state, "listening": listening,
                            "connection": "network" if network else "local", "protocol_version": identity["protocol_version"]})
        return {"room": chat.name, "room_state": value["room_state"], "members": members, "member_count": len(members),
                "max_members": MAX_MEMBERS, "latest_seq": value["latest_seq"], "security": chat.security()}

    def progress(self, chat, name, message_ids, collaboration=False):
        ids(message_ids, "message_ids", 100)
        with contextlib.closing(chat.store.connect()) as db:
            viewer = db.execute("SELECT member_id FROM chat_members WHERE name=?", (name,)).fetchone()
            viewer_id = viewer[0] if viewer else None
            placeholders = ",".join("?" for _ in message_ids)
            condition = COLLABORATION_VISIBLE if collaboration else "(recipient IS NULL OR sender_member_id=? OR recipient_member_id=?)"
            params = tuple(message_ids) if collaboration else (*message_ids, viewer_id, viewer_id)
            visible = db.execute(f"SELECT id,progress_tracked FROM chat_messages WHERE id IN ({placeholders}) AND "
                                 + condition + " ORDER BY seq", params).fetchall()
            values = []
            for message in visible:
                recipients = [{"name": row["name"], "state": "acknowledged" if row["acked_at"] is not None else
                               "received" if row["received_at"] is not None else "sent"}
                              for row in db.execute("SELECT name,received_at,acked_at FROM chat_progress WHERE message_id=? ORDER BY name",
                                                    (message["id"],))]
                values.append({"message_id": message["id"], "tracked": bool(message["progress_tracked"]), "recipients": recipients})
        return {"messages": values}

    def history(self, chat, name, after, peer=None, collaboration=False):
        after = integer(after, "after", 0, 2**63 - 1)
        if peer is not None:
            peer = member_name(peer)
            if not name or collaboration:
                raise RelayError("单独对话需要自己的成员身份", 403)
        with contextlib.closing(chat.store.connect()) as db:
            condition = COLLABORATION_VISIBLE if collaboration else (
                "(recipient IS NULL OR sender_member_id=(SELECT member_id FROM chat_members WHERE name=?) "
                "OR recipient_member_id=(SELECT member_id FROM chat_members WHERE name=?))")
            params = (after,) if collaboration else (after, name, name)
            if peer is not None:
                condition += " AND ((sender=? AND recipient=?) OR (sender=? AND recipient=?))"
                params += (name, peer, peer, name)
            rows = db.execute("SELECT seq,id,sender,recipient,content,reply_to,created_at FROM chat_messages "
                              "WHERE seq>? AND " + condition + " ORDER BY seq LIMIT 100", params)
            messages = []
            cursor = chat.latest(db)
            size = len(json.dumps({"messages": [], "cursor": cursor, "read_only": True}).encode())
            for row in rows:
                message = dict(row)
                extra = len(json.dumps(message, ensure_ascii=False).encode("utf-8")) + (2 if messages else 0)
                # Keep each page within the remote client's response limit.
                # The first legal message always fits; never advance past an omitted row.
                if messages and size + extra > HISTORY_BYTE_LIMIT:
                    break
                messages.append(message)
                size += extra
            if messages:
                cursor = messages[-1]["seq"]
        return {"messages": messages, "cursor": cursor, "read_only": True}

    def peek(self, chat, name, owner, after_id, wait, recovery_state=None):
        if recovery_state not in (None, "recovery_wait", "recovering", "needs_attention"):
            raise RelayError("无效恢复状态")
        if chat.trusted_local():
            return self._transport_peek(chat, name, owner, after_id, wait, recovery_state)
        integer(wait, "wait", 0, 20)
        with chat.store.transaction() as db:
            member = chat.member(db, name, owner)
            db.execute("INSERT INTO chat_network_seen(name,seen,state,watching_until) VALUES(?,?,'manual_only',0) "
                       "ON CONFLICT(name) DO UPDATE SET seen=excluded.seen,state='manual_only',watching_until=0",
                       (name, time.time()))
            return {"message": None, "pending": member["pending_seq"] is not None,
                    "state": "manual_only", "instructions": "宿主工具拦截未验证，自动唤醒已关闭；请由本机用户手动收件。"}

    def _transport_peek(self, chat, name, owner, after_id, wait, recovery_state=None):
        """Wait without a model; only for a room limited to this computer."""
        if __package__:
            from .chat_recovery import pending_hint
        else:
            from chat_recovery import pending_hint
        wait = integer(wait, "wait", 0, 20)
        deadline = time.monotonic() + wait
        while True:
            if not chat.trusted_local():
                return self.peek(chat, name, owner, after_id, 0)
            with chat.store.transaction() as db:
                member = chat.member(db, name, owner)
                if member["protocol_version"] == 2:
                    row = chat.delivery.first_notification(db, member)
                    message = dict(row) if row else None
                else:
                    rows = chat.visible(db, name, member["confirmed_seq"], limit=1)
                    message = dict(rows[0]) if rows else None
                pending = member["pending_seq"] is not None
                recovery = pending_hint(chat, db, member) if pending else None
                if pending:
                    # Do not advance a client's wake cursor to the NEXT batch
                    # while an earlier one is still being processed.
                    message = None
                if message and member["protocol_version"] == 2 and time.time() < message["created_at"] + COALESCE_SECONDS:
                    message = None
                ready = bool(message and message["id"] != after_id)
                done = ready or time.monotonic() >= deadline or self.stopping.is_set()
                state = ("processing" if pending else "message_available" if ready else
                         "awaiting_receipt" if message else "joined" if done else "waiting")
                if recovery_state and (message or recovery):
                    state = recovery_state
                db.execute("INSERT INTO chat_network_seen VALUES(?,?,?,?) ON CONFLICT(name) DO UPDATE SET "
                           "seen=excluded.seen,state=excluded.state,watching_until=excluded.watching_until",
                           (name, time.time(), state, 0 if done else time.time() + 2))
            if done:
                return {"message": {k: message[k] for k in ("id", "sender", "seq")} if message else None,
                        "pending": pending, "recovery_status": True, **({"recovery_hint": recovery} if recovery else {})}
            self.changed.wait(.3)  # Also observes local CLI writers, without any model call.
            self.changed.clear()

    def dispatch(self, path, token, data):
        if path == "/api/rooms":
            if not self.is_admin(token):
                raise RelayError("需要房主访问密钥", 401)
            if data.get("action") == "create":
                chat = self.room(data.get("room"), create=True)
                return {**self.snapshot(chat), **self.invitation(chat)}
            if set(data) - {"include_deleted"} or ("include_deleted" in data and not isinstance(data["include_deleted"], bool)):
                raise RelayError("无效操作")
            rooms = []
            for folder in sorted((self.store.root / "rooms").glob("*")):
                if folder.is_dir() and re.fullmatch(ROOM_PATTERN, unicodedata.normalize("NFC", folder.name)):
                    try:
                        chat = self.room(folder.name, include_deleted=True)
                        snapshot = self.snapshot(chat, include_deleted=True)
                        if (snapshot["room_state"] == "deleted") == bool(data.get("include_deleted")):
                            rooms.append({**snapshot, **self.invitation(chat)})
                    except RelayError as error:
                        if error.status != 404:
                            raise
            return {"rooms": rooms, "server": self.public_url, "max_members": MAX_MEMBERS, "room_management": True}
        match = re.fullmatch(r"/api/rooms/([^/]+)/([a-z]+)", path)
        if not match:
            raise RelayError("接口不存在", 404)
        room_id, action = unquote(match.group(1)), match.group(2)
        if action in ("close", "reopen", "delete", "restore"):
            if not self.is_admin(token):
                raise RelayError("只有用户可以管理房间", 403)
            if data:
                raise RelayError("房间操作不接受额外参数")
            chat = self.room(room_id, include_deleted=True)
            result = chat.change_state(action)
            self.changed.set()
            return result
        chat = self.room(room_id, include_deleted=self.is_admin(token))
        if action in ("activity", "activityprogress"):
            # Separate owner view: never widen an Agent's history/receive permissions.
            if not self.is_admin(token) or not chat.trusted_local():
                raise RelayError("协作记录仅供本机房主查看", 403)
            if action == "activity":
                if set(data) - {"after"}:
                    raise RelayError("协作记录参数无效")
                return self.history(chat, None, data.get("after", 0), collaboration=True)
            if set(data) != {"message_ids"}:
                raise RelayError("消息状态需要 message_ids")
            return self.progress(chat, None, data["message_ids"], collaboration=True)
        if action == "human":
            # Upgrade an already joined browser identity without rejoining a
            # removed member, guessing by nickname, or trusting an agent flag.
            if not self.is_admin(token):
                raise RelayError("只有房主管理会话可以登记人类身份", 403)
            if set(data) != {"member_token"}:
                raise RelayError("身份登记参数无效")
            name, owner = self.identity(chat, data["member_token"])
            with chat.store.transaction() as db:
                chat.member(db, name, owner)
                db.execute("UPDATE chat_members SET kind='human' WHERE name=?", (name,))
            return {"name": name, "status": "human_registered"}
        if action in ("join", "humanjoin"):
            if set(data) - {"name", "member_token", "protocol_version"} or not {"name", "member_token"} <= set(data):
                raise RelayError("加入参数无效")
            if action == "humanjoin" and not self.is_admin(token):
                raise RelayError("只有房主管理会话可以登记人类身份", 403)
            if not self.allowed_invite(chat, token):
                raise RelayError("房间邀请无效", 401)
            name, owner = member_name(data["name"]), owner_for(data["member_token"])
            with self.lock:
                with contextlib.closing(chat.store.connect()) as db:
                    previous = db.execute("SELECT owner FROM chat_members WHERE name=?", (name,)).fetchone()
                    if previous and previous["owner"] != owner:
                        raise RelayError("昵称已保留，请使用自己的凭据或换一个昵称", 409)
                    reused = db.execute("SELECT name FROM chat_members WHERE owner=? AND name<>?", (owner, name)).fetchone()
                    if reused:
                        raise RelayError("一个成员凭据只能绑定一个昵称", 409)
                result = wire(chat.join(name, owner, protocol_version=data.get("protocol_version", 1), trusted_human=action == "humanjoin"))
                self.identity(chat, data["member_token"], "joined")
            return {**result, "security": chat.security()}
        if action in ("state", "history", "progress"):
            name = None
            if not self.allowed_invite(chat, token):
                name, _ = self.identity(chat, token, "viewing" if action == "history" else None)
            if action == "history":
                if set(data) - {"after", "peer"}:
                    raise RelayError("历史记录参数无效")
                return self.history(chat, name, data.get("after", 0), peer=data.get("peer"))
            if action == "progress":
                if set(data) != {"message_ids"}:
                    raise RelayError("消息状态需要 message_ids")
                return self.progress(chat, name, data["message_ids"])
            result = self.snapshot(chat, include_deleted=self.is_admin(token))
            result["room_management"] = self.is_admin(token)
            result["collaboration_view"] = self.is_admin(token) and chat.trusted_local()
            if self.is_admin(token):
                result.update(self.invitation(chat))
            return result
        if action == "remove":
            if not self.is_admin(token):
                raise RelayError("需要房主访问密钥", 403)
            result = wire(chat.leave(member_name(data.get("name")), owner=None))
        else:
            name, owner = self.identity(chat, token)
            allowed = {"send": {"content", "to", "reply_to", "key", "delivery_id", "notify", "ack_ids"}, "receive": set(),
                       "ack": {"message_id", "message_ids", "delivery_id"}, "contextinfo": set(),
                       "context": {"message_id", "delivery_id", "offset", "after_seq", "budget"}, "sources": {"message_ids"},
                       "review": set(), "leave": set(), "peek": {"after_id", "wait", "recovery_state"}, "workerstatus": {"state"}}
            if action not in allowed or set(data) - allowed[action]:
                raise RelayError("接口或参数无效", 400)
            required = {"send": "content", "sources": "message_ids"}.get(action)
            if required and required not in data:
                raise RelayError("缺少参数：" + required)
            if action == "peek":
                return self.peek(chat, name, owner, data.get("after_id"), data.get("wait", 20), data.get("recovery_state"))
            if action == "workerstatus":
                return chat.workerstatus(name, owner, **data)
            if action == "contextinfo":
                return chat.context_info(name, owner)
            if action == "context":
                return chat.delivery.context(name, owner, **data)
            if action == "sources":
                return chat.delivery.sources(name, owner, **data)
            if action == "review":
                return wire(chat.wait(name, timeout=0, owner=owner, replay=True))
            if action == "receive":
                result = wire(chat.receive(name, owner=owner))
            elif action == "send":
                result = wire(chat.send(name, owner=owner, **data))
            elif action == "ack":
                result = wire(chat.ack(name, owner=owner, **data))
            else:
                result = wire(chat.leave(name, owner=owner))
        self.changed.set()
        return result


class BoundedHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 32

    def __init__(self, *args, **kwargs):
        self.slots = threading.BoundedSemaphore(40)
        super().__init__(*args, **kwargs)

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()

    def server_close(self):
        if hasattr(self, "service"):
            self.service.stopping.set()
            self.service.changed.set()
        super().server_close()


def create_server(data_dir, host="127.0.0.1", port=None, public_url=None, cert=None, key=None):
    port = DEFAULT_PORT if port is None else port
    if bool(cert) != bool(key):
        raise RelayError("HTTPS 需要同时指定 --cert 和 --key")
    if host not in ("127.0.0.1", "localhost") and not public_url:
        raise RelayError("网络监听请同时设置 --public-url 为其他电脑能访问的地址")
    service = ChatService(data_dir, public_url or f"{'https' if cert else 'http'}://127.0.0.1:{port}")

    class Handler(BaseHTTPRequestHandler):
        server_version = "AIChatroom/1"

        def setup(self):
            super().setup()
            self.connection.settimeout(30)

        def log_message(self, *args):
            pass  # No credentials or chat bodies in access logs.

        def respond(self, status, value, mime="application/json; charset=utf-8"):
            body = value if isinstance(value, bytes) else json.dumps(value, ensure_ascii=False).encode()
            self.send_response(status)
            for k, v in {"Content-Type": mime, "Content-Length": str(len(body)), "Cache-Control": "no-store",
                         "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer",
                         "X-Frame-Options": "DENY", "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"}.items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def allowed(self):
            port = self.server.server_address[1]
            hosts = {urlsplit(service.public_url).netloc, f"127.0.0.1:{port}", f"localhost:{port}"}
            host = self.headers.get("Host", "").lower()
            origin = self.headers.get("Origin")
            origins = {"http://" + h for h in hosts} | {"https://" + h for h in hosts}
            if host not in hosts or (origin and origin not in origins):
                raise RelayError("访问地址或来源不匹配", 403)

        def do_GET(self):
            try:
                self.allowed()
                path = urlsplit(self.path).path
                assets = {"/": ("chat.html", "text/html; charset=utf-8"),
                          "/bootstrap.js": ("bootstrap.js", "application/javascript; charset=utf-8"),
                          "/chat.js": ("chat.js", "application/javascript; charset=utf-8"),
                          "/airrelay.svg": ("airrelay.svg", "image/svg+xml"),
                          "/chat.css": ("chat.css", "text/css; charset=utf-8")}
                if path == "/health":
                    self.respond(200, {"ok": True, "service": "ai-chatroom", "max_members": MAX_MEMBERS,
                                       "chat_protocols": [1, 2], "context_management": "local-checkpoint-v1"})
                elif path in assets:
                    file, mime = assets[path]
                    self.respond(200, (STATIC / file).read_bytes(), mime)
                else:
                    raise RelayError("页面不存在", 404)
            except RelayError as error:
                self.respond(error.status, {"error": str(error)})

        def direct_local(self, browser=False):
            host_origin = ("https" if cert else "http") + "://" + self.headers.get("Host", "")
            origins = (service.public_url, host_origin) if browser else (None, service.public_url, host_origin)
            return (self.server.local_access and self.client_address[0] == "127.0.0.1"
                    and self.headers.get("Origin") in origins
                    and self.headers.get("Sec-Fetch-Site") in (None, "same-origin")
                    and not any(self.headers.get(h) for h in ("Forwarded", "X-Forwarded-For", "X-Forwarded-Host")))

        def do_POST(self):
            try:
                self.allowed()
                if any(self.headers.get(h) for h in ("Forwarded", "X-Forwarded-For", "X-Forwarded-Host")):
                    if __package__:
                        from .chat_safety import protect_network
                    else:
                        from chat_safety import protect_network
                    protect_network(service.store.root)
                if self.headers.get_content_type() != "application/json":
                    raise RelayError("需要 JSON 请求", 415)
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                except ValueError:
                    raise RelayError("请求长度无效")
                if self.headers.get("Transfer-Encoding") or not 0 < length <= 500000:
                    raise RelayError("请求过大或格式无效", 413)
                try:
                    data = json.loads(self.rfile.read(length))
                except (ValueError, UnicodeError):
                    raise RelayError("JSON 格式无效")
                if not isinstance(data, dict):
                    raise RelayError("请求必须是 JSON 对象")
                if urlsplit(self.path).path == "/api/local-session":
                    # Only the directly opened loopback UI can obtain its local
                    # management session. Network/proxy deployments still use invitations.
                    if not self.direct_local(browser=True):
                        raise RelayError("自动进入只用于本机直接访问", 403)
                    if data:
                        raise RelayError("本机会话不接受额外参数")
                    # This endpoint is available only to the direct local browser.
                    # Pin invitations to this checkout and data, not a global CLI.
                    argv = [sys.executable, str(STATIC.parent / "relay.py"), "--data-dir", str(service.store.root)]
                    command = subprocess.list2cmdline(argv) if os.name == "nt" else shlex.join(argv)
                    self.respond(200, {"admin_token": service.admin_token, "cli_command": command,
                                       "local_join": local_trusted(service.store.root)})
                    return
                auth = self.headers.get("Authorization", "")
                token = auth[7:] if auth.startswith("Bearer ") else ""
                # Local users need no invitation. Only discovery/join use this
                # shortcut; messages, context and receipts still require the
                # automatically stored member identity. Never override a bad token.
                match = re.fullmatch(r"/api/rooms/([^/]+)/(state|join)", urlsplit(self.path).path)
                if not token and auth in ("", "Bearer ") and match and self.direct_local():
                    chat = service.room(unquote(match.group(1)))
                    if chat.trusted_local():
                        token = service.invitation(chat)["invite"]
                if not token or not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", token):
                    raise RelayError("需要访问凭据", 401)
                self.respond(200, service.dispatch(urlsplit(self.path).path, token, data))
            except RelayError as error:
                self.respond(error.status, {"error": str(error)})
            except (BrokenPipeError, ConnectionResetError, TimeoutError):
                pass
            except Exception:
                self.respond(500, {"error": "聊天室服务发生错误"})

    server = BoundedHTTPServer((host, port), Handler)
    server.service = service
    if not public_url:
        service.public_url = f"{'https' if cert else 'http'}://127.0.0.1:{server.server_address[1]}"
    server.local_access = (server.server_address[0] == "127.0.0.1" and
                           urlsplit(service.public_url).hostname in ("127.0.0.1", "localhost"))
    if not server.local_access:
        if __package__:
            from .chat_safety import protect_network
        else:
            from chat_safety import protect_network
        protect_network(service.store.root)
    if cert:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(cert, key)
        server.socket = context.wrap_socket(server.socket, server_side=True)
    return server
