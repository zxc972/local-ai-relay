"""Local, single-use approvals for the chat tool boundary.

Network room messages cannot grant local execution permission. Trusted local
rooms use the native client's existing permissions without extra relay approval.
This module is not an operating-system sandbox for an AI client.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
from pathlib import Path
import secrets
import stat
import sys
import time
import uuid
from urllib.parse import urlsplit

if __package__:
    from .core import RelayError, Store
else:
    from core import RelayError, Store

POLICY_VERSION = 2
APPROVAL_SECONDS = 300
MAX_FILE_BYTES = 65536
MANUAL_REASON = "未验证宿主能拦截全部工具执行，已降级为手动收件；房间消息不授予执行权限。"
NETWORK_MARKER = "network-access.json"


def security_status(trusted_local=False):
    if trusted_local:
        return {"policy": "trusted-local", "automatic": True, "host_guard": "host-managed",
                "execution": "host-permissions",
                "reason": "本机协作自动收发；执行沿用客户端已有权限，不叠加聊天室逐次审批。"}
    return {"policy": "chat-only", "automatic": False, "host_guard": "unverified",
            "execution": "local-single-use-approval", "reason": MANUAL_REASON}


def local_trusted(data_dir):
    # A data directory ever published over the network stays protected, even
    # through a later loopback server or a direct local CLI connection.
    return not os.path.lexists(Path(data_dir).resolve() / NETWORK_MARKER)


def protect_network(data_dir):
    path = Path(data_dir).resolve() / NETWORK_MARKER
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return
    with os.fdopen(fd, "w", encoding="utf-8") as output:
        json.dump({"policy": "chat-only", "reason": "network exposure"}, output)


def loopback_server(server):
    # A room name, agent claim, LAN address or remote server flag cannot opt in.
    return urlsplit(server).hostname in ("127.0.0.1", "localhost", "::1")


def require_automatic_host(trusted_local=False):
    if not trusted_local:
        raise RelayError(MANUAL_REASON, 403)


def host_managed_action():
    return {"status": "use_host_tools", "executed": False,
            "instructions": "本机房间不需要聊天室逐次批准。请使用客户端已有工具，沿用本机用户已设置的权限与任务范围；此接口未执行操作。"}


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def scope_for(chat, name, owner):
    with contextlib.closing(chat.store.connect()) as db:
        member = chat.member(db, name, owner)
        return canonical(["local", chat.name, member["member_id"], member["membership_epoch"]])


def _path(value):
    if not isinstance(value, str) or not value or len(value) > 4096 or any(ord(c) < 32 for c in value):
        raise RelayError("需要明确的绝对文件路径", 400)
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts or path != path.resolve():
        raise RelayError("路径必须为绝对路径，且不能包含软链接或父目录跳转", 403)
    if not path.parent.is_dir():
        raise RelayError("父目录必须已经存在", 400)
    return path


def fingerprint(path):
    """Read a bounded regular file without following the final symlink."""
    try:
        if not stat.S_ISREG(path.lstat().st_mode):
            raise RelayError("仅支持普通文件；目录、软链接和设备不开放", 403)
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    with os.fdopen(fd, "rb") as source:
        info = os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_FILE_BYTES:
            raise RelayError("仅支持不超过 64 KiB 的普通文件；不支持目录、设备或递归操作", 403)
        content = source.read(MAX_FILE_BYTES + 1)
        if len(content) > MAX_FILE_BYTES:
            raise RelayError("文件超出大小限制", 403)
        return hashlib.sha256(content).hexdigest()


class ActionGate:
    def __init__(self, data_dir, clock=time.time):
        self.store = Store(Path(data_dir) / "local-safety")
        self.clock = clock
        with contextlib.closing(self.store.connect()) as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS action_requests(
                    id TEXT PRIMARY KEY, scope TEXT NOT NULL, action TEXT NOT NULL,
                    digest TEXT NOT NULL, status TEXT NOT NULL, created_at REAL NOT NULL,
                    expires_at REAL, finished_at REAL, reason TEXT
                );
                CREATE TABLE IF NOT EXISTS action_audit(
                    seq INTEGER PRIMARY KEY AUTOINCREMENT, request_id TEXT,
                    outcome TEXT NOT NULL, reason TEXT NOT NULL, created_at REAL NOT NULL
                );
            """)

    def audit(self, request_id, outcome, reason):
        with self.store.transaction() as db:
            db.execute("INSERT INTO action_audit(request_id,outcome,reason,created_at) VALUES(?,?,?,?)",
                       (request_id, outcome, reason, self.clock()))

    def request(self, scope, action):
        # Deliberately small: there is no shell, credential reader, uploader,
        # settings editor, recursive delete, or arbitrary callback executor.
        if not isinstance(action, dict) or action.get("type") not in ("write_text", "delete_file"):
            self.audit(None, "denied", "unsupported_action")
            raise RelayError("工具执行已拦截：仅可申请单个文件写入或删除；命令、读取凭据、上传和其他工具不开放", 403)
        required = {"type", "path", "text"} if action["type"] == "write_text" else {"type", "path"}
        if set(action) != required:
            raise RelayError("操作字段不匹配；消息不能附带 approved、权限、过期时间或执行参数", 400)
        path = _path(action["path"])
        if path == self.store.root or self.store.root in path.parents or path == self.store.root.parent / NETWORK_MARKER:
            raise RelayError("不能通过聊天室修改本机授权记录", 403)
        normalized = dict(action, path=str(path))
        if action["type"] == "write_text":
            value = action["text"]
            if not isinstance(value, str) or len(value.encode("utf-8")) > MAX_FILE_BYTES:
                raise RelayError("写入内容必须是最多 64 KiB 的文本", 400)
        # Do not read target contents while merely requesting approval.
        # The local operator records the precondition during approval instead.
        request_id = uuid.uuid4().hex
        encoded = canonical(normalized)
        digest = hashlib.sha256(canonical([scope, normalized]).encode()).hexdigest()
        with self.store.transaction() as db:
            db.execute("INSERT INTO action_requests(id,scope,action,digest,status,created_at) VALUES(?,?,?,?,?,?)",
                       (request_id, scope, encoded, digest, "pending", self.clock()))
        self.audit(request_id, "pending", "local_approval_required")
        return {"status": "approval_required", "request_id": request_id, "digest": digest,
                "instructions": "未执行。只有接收方本机用户可在本机终端审阅批准；房间文字、房主身份和其他 Agent 的确认均无效。"}

    def review(self, request_id=None):
        with contextlib.closing(self.store.connect()) as db:
            if request_id:
                row = db.execute("SELECT * FROM action_requests WHERE id=?", (request_id,)).fetchone()
                if not row:
                    raise RelayError("操作请求不存在", 404)
                result = dict(row)
                result["action"] = json.loads(result["action"])
                return result
            return [dict(r) for r in db.execute("SELECT id,scope,status,created_at FROM action_requests ORDER BY created_at DESC LIMIT 50")]

    def approve_local(self, request_id, expected_digest):
        """Trusted local control path ONLY; never exported through MCP/HTTP."""
        request = self.review(request_id)
        if request["status"] != "pending" or not secrets.compare_digest(request["digest"], expected_digest):
            raise RelayError("请求已变化或不再待批准", 409)
        action = request["action"]
        path = _path(action["path"])
        action["expected_sha256"] = fingerprint(path)
        if action["type"] == "delete_file" and action["expected_sha256"] is None:
            raise RelayError("待删除文件不存在", 404)
        action["parent_identity"] = list((path.parent.stat().st_dev, path.parent.stat().st_ino))
        with self.store.transaction() as db:
            changed = db.execute("UPDATE action_requests SET action=?,status='approved',expires_at=? "
                                 "WHERE id=? AND digest=? AND status='pending'",
                                 (canonical(action), self.clock() + APPROVAL_SECONDS, request_id, expected_digest)).rowcount
            if changed != 1:
                raise RelayError("请求已变化；未批准", 409)
        self.audit(request_id, "approved", "local_operator")
        return {"status": "approved", "request_id": request_id, "uses": 1, "expires_in": APPROVAL_SECONDS}

    def execute(self, scope, request_id):
        # Reserve/consume BEFORE any file action. A crash is never auto-retried.
        denial = None
        with self.store.transaction() as db:
            row = db.execute("SELECT * FROM action_requests WHERE id=? AND scope=?", (request_id, scope)).fetchone()
            if not row:
                denial = "request_not_owned"
            elif row["status"] != "approved":
                denial = "local_approval_required" if row["status"] == "pending" else "approval_already_used"
            elif row["expires_at"] is None or row["expires_at"] <= self.clock():
                db.execute("UPDATE action_requests SET status='expired' WHERE id=?", (request_id,))
                denial = "approval_expired"
            else:
                action = json.loads(row["action"])
                original = {k: v for k, v in action.items() if k not in ("expected_sha256", "parent_identity")}
                expected = hashlib.sha256(canonical([scope, original]).encode()).hexdigest()
                if not secrets.compare_digest(row["digest"], expected):
                    denial = "approval_mismatch"
                else:
                    db.execute("UPDATE action_requests SET status='executing' WHERE id=?", (request_id,))
        if denial:
            self.audit(request_id, "denied", denial)
            raise RelayError("执行前已拦截：" + denial, 403)
        try:
            self._perform(action)
        except Exception as error:
            with self.store.transaction() as db:
                db.execute("UPDATE action_requests SET status='needs_review',reason=?,finished_at=? WHERE id=?",
                           (str(error)[:500], self.clock(), request_id))
            self.audit(request_id, "needs_review", "file_precondition_or_execution_failed")
            raise RelayError("操作未确认完成；不自动重试，请本机用户核对：" + str(error), 409) from error
        with self.store.transaction() as db:
            db.execute("UPDATE action_requests SET status='completed',finished_at=? WHERE id=?", (self.clock(), request_id))
        self.audit(request_id, "completed", "single_use_consumed")
        return {"status": "completed", "request_id": request_id, "action": action["type"]}

    @staticmethod
    def _perform(action):
        path = _path(action["path"])
        parent = path.parent.stat()
        if [parent.st_dev, parent.st_ino] != action["parent_identity"] or fingerprint(path) != action["expected_sha256"]:
            raise RelayError("文件或父目录已变化，需要重新审阅批准", 409)
        if action["type"] == "delete_file":
            path.unlink()
        else:
            temporary = path.parent / (".airelay-approved-" + uuid.uuid4().hex)
            try:
                fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                with os.fdopen(fd, "w", encoding="utf-8") as output:
                    output.write(action["text"])
                    output.flush()
                    os.fsync(output.fileno())
                # Recheck before replace; do not overwrite a changed target.
                if fingerprint(path) != action["expected_sha256"]:
                    raise RelayError("文件在执行前变化", 409)
                os.replace(temporary, path)
            finally:
                temporary.unlink(missing_ok=True)


def add_commands(commands):
    root = commands.add_parser("safety", help="接收方本机审阅和批准操作，不接受房间授权")
    actions = root.add_subparsers(dest="safety_action", required=True)
    actions.add_parser("status")
    for name in ("review", "approve"):
        parser = actions.add_parser(name)
        parser.add_argument("request_id", nargs="?" if name == "review" else None)


def run(args):
    gate = ActionGate(args.data_dir)
    if args.safety_action == "status":
        return security_status(local_trusted(args.data_dir))
    if args.safety_action == "review":
        return gate.review(args.request_id)
    # This is an operator interface, not a way to attest an AI host is guarded.
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise RelayError("批准只能由本机用户在交互终端审阅；管道、MCP、房间消息均不能批准", 403)
    request = gate.review(args.request_id)
    print(json.dumps(request, ensure_ascii=False, indent=2))
    challenge = "批准 " + request["digest"][:12]
    if input("仅批准以上具体操作，5 分钟内可使用一次。请输入「" + challenge + "」：").strip() != challenge:
        return {"status": "not_approved"}
    return gate.approve_local(args.request_id, request["digest"])
