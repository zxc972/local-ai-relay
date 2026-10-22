"""Durable, local mailbox shared by independent MCP and chat processes."""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import secrets
import sqlite3
import sys
import time
import uuid
from pathlib import Path

def default_data_dir():
    """Use writable user data, while preserving an existing source checkout."""
    override = os.environ.get("AIRELAY_DATA_DIR")
    if override:
        return Path(override).expanduser()
    source = Path(__file__).resolve().parent
    if ((source / ".relay").is_dir() and (source / "tests").is_dir()
            and (source / "bin" / "airelay").is_file()
            and (source / "README.md").is_file()):
        return source / ".relay"
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
        return base / "local-ai-relay"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "local-ai-relay"
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / "local-ai-relay"


DEFAULT_DATA = default_data_dir()
MAX_CONTENT = 100_000


class RelayError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def text_field(value, name, maximum=MAX_CONTENT):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise RelayError(f"{name} 必须是 1–{maximum} 字符的文本")
    return value.strip()


def integer(value, name, minimum, maximum):
    if type(value) is not int or not minimum <= value <= maximum:
        raise RelayError(f"{name} 必须是 {minimum}–{maximum} 之间的整数")
    return value


def private_write(path: Path, value: str):
    # O_EXCL prevents a symlink or another initializer from replacing a secret.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        stream.write(value)


class Store:
    def __init__(self, data_dir=DEFAULT_DATA, clock=time.time):
        self.root = Path(data_dir).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.clock = clock
        self.path = self.root / "relay.sqlite3"
        with contextlib.closing(self.connect()) as db:
            # Two chats can create a room at the same instant. SQLite's initial
            # journal-mode switch can return BUSY without using busy_timeout.
            deadline = time.monotonic() + 10
            while True:
                try:
                    db.execute("PRAGMA journal_mode=WAL")
                    break
                except sqlite3.OperationalError as error:
                    if ("locked" not in str(error) and "busy" not in str(error)) or time.monotonic() >= deadline:
                        raise
                    time.sleep(.02)
            db.executescript("""
                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY, title TEXT NOT NULL,
                    status TEXT NOT NULL, round INTEGER NOT NULL,
                    max_rounds INTEGER NOT NULL, created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS messages (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    id TEXT NOT NULL UNIQUE, task_id TEXT NOT NULL REFERENCES tasks(id),
                    round INTEGER NOT NULL, kind TEXT NOT NULL,
                    sender TEXT NOT NULL, recipient TEXT NOT NULL,
                    content TEXT NOT NULL, reply_to TEXT UNIQUE,
                    created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS deliveries (
                    message_id TEXT PRIMARY KEY REFERENCES messages(id),
                    status TEXT NOT NULL DEFAULT 'queued',
                    lease TEXT, lease_until REAL, attempts INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS submissions (
                    key TEXT PRIMARY KEY, digest TEXT NOT NULL,
                    task_id TEXT NOT NULL, message_id TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS messages_task ON messages(task_id, seq);
                CREATE INDEX IF NOT EXISTS deliveries_status ON deliveries(status);
                CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            """)
        self.path.chmod(0o600)

    def connect(self):
        db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA busy_timeout=10000")
        db.execute("PRAGMA synchronous=FULL")
        return db

    @contextlib.contextmanager
    def transaction(self):
        db = self.connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def _task(db, task_id):
        row = db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if row is None:
            raise RelayError("任务不存在", 404)
        return dict(row)

    def _expire(self, db):
        # Unknown execution outcome is not permission to repeat side effects.
        expired = db.execute(
            "SELECT message_id FROM deliveries WHERE status='leased' AND lease_until<=?",
            (self.clock(),),
        ).fetchall()
        for row in expired:
            db.execute("UPDATE deliveries SET status=CASE WHEN (SELECT t.status FROM tasks t JOIN messages m "
                       "ON m.task_id=t.id WHERE m.id=message_id)='closed' THEN 'closed' ELSE 'uncertain' END "
                       "WHERE message_id=?", (row[0],))
            db.execute("UPDATE tasks SET status='stalled', updated_at=? WHERE id="
                       "(SELECT task_id FROM messages WHERE id=?) AND status!='closed'",
                       (self.clock(), row[0]))

    @staticmethod
    def _dedup(db, key, payload):
        key = text_field(key, "idempotency_key", 200)
        digest = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        previous = db.execute("SELECT * FROM submissions WHERE key=?", (key,)).fetchone()
        if previous and previous["digest"] != digest:
            raise RelayError("同一个幂等键已用于不同内容，请使用新的 idempotency_key", 409)
        return key, digest, dict(previous) if previous else None

    def _message(self, db, task_id, round_no, kind, sender, recipient, content, reply_to=None):
        mid = str(uuid.uuid4())
        db.execute("INSERT INTO messages(id,task_id,round,kind,sender,recipient,content,reply_to,created_at) "
                   "VALUES(?,?,?,?,?,?,?,?,?)",
                   (mid, task_id, round_no, kind, sender, recipient, content, reply_to, self.clock()))
        if recipient == "codex":
            db.execute("INSERT INTO deliveries(message_id) VALUES(?)", (mid,))
        return mid

    def send_task(self, title, content, idempotency_key, max_rounds=4, sender="peer"):
        title = text_field(title, "title", 160)
        content = text_field(content, "content")
        sender = text_field(sender, "sender", 80)
        if sender == "codex":
            raise RelayError("codex 是保留的发送者")
        integer(max_rounds, "max_rounds", 1, 12)
        with self.transaction() as db:
            key, digest, prev = self._dedup(db, idempotency_key, ["task", title, content, max_rounds, sender])
            if prev:
                return {"task_id": prev["task_id"], "message_id": prev["message_id"], "duplicate": True}
            tid, now = str(uuid.uuid4()), self.clock()
            db.execute("INSERT INTO tasks VALUES(?,?, 'queued',1,?,?,?)", (tid, title, max_rounds, now, now))
            mid = self._message(db, tid, 1, "task", sender, "codex", content)
            db.execute("INSERT INTO submissions VALUES(?,?,?,?)", (key, digest, tid, mid))
        return {"task_id": tid, "message_id": mid, "duplicate": False, "status": "queued"}

    def feedback(self, task_id, content, reply_to, idempotency_key, sender="peer"):
        content = text_field(content, "content")
        sender = text_field(sender, "sender", 80)
        if sender == "codex":
            raise RelayError("codex 是保留的发送者")
        with self.transaction() as db:
            key, digest, prev = self._dedup(db, idempotency_key, ["feedback", task_id, content, reply_to, sender])
            if prev:
                return {"task_id": prev["task_id"], "message_id": prev["message_id"], "duplicate": True}
            task = self._task(db, task_id)
            if task["status"] != "waiting_review":
                raise RelayError("任务当前未等待评审；请先等待本轮结果", 409)
            answer = db.execute("SELECT * FROM messages WHERE id=? AND task_id=? AND kind='result'",
                                (reply_to, task_id)).fetchone()
            if not answer or answer["round"] != task["round"]:
                raise RelayError("reply_to 必须指向本任务最新一轮的结果消息", 409)
            if task["round"] >= task["max_rounds"]:
                raise RelayError("已达到最大往返轮数，请结束此任务或由用户建立新任务", 409)
            round_no = task["round"] + 1
            mid = self._message(db, task_id, round_no, "feedback", sender, "codex", content, reply_to)
            db.execute("UPDATE tasks SET status='queued',round=?,updated_at=? WHERE id=?",
                       (round_no, self.clock(), task_id))
            db.execute("INSERT INTO submissions VALUES(?,?,?,?)", (key, digest, task_id, mid))
        return {"task_id": task_id, "message_id": mid, "round": round_no, "duplicate": False}

    def claim(self, lease_seconds=3600):
        integer(lease_seconds, "lease_seconds", 60, 86400)
        with self.transaction() as db:
            self._expire(db)
            db.execute("INSERT OR REPLACE INTO meta VALUES('last_poll',?)", (str(self.clock()),))
            if db.execute("SELECT 1 FROM deliveries WHERE status IN ('leased','uncertain')").fetchone():
                return {"message": None, "reason": "另一个任务仍在执行或需要人工处理"}
            row = db.execute("SELECT m.* FROM messages m JOIN deliveries d ON d.message_id=m.id "
                             "JOIN tasks t ON t.id=m.task_id WHERE d.status='queued' AND t.status='queued' "
                             "ORDER BY m.seq LIMIT 1").fetchone()
            if not row:
                return {"message": None}
            message, lease = dict(row), secrets.token_urlsafe(24)
            until = self.clock() + lease_seconds
            db.execute("UPDATE deliveries SET status='leased',lease=?,lease_until=?,attempts=attempts+1 "
                       "WHERE message_id=?", (lease, until, message["id"]))
            db.execute("UPDATE tasks SET status='running',updated_at=? WHERE id=?", (self.clock(), message["task_id"]))
            task = self._task(db, message["task_id"])
            # Return only conversation messages, never unrelated chats or machine context.
            history = [dict(r) for r in db.execute("SELECT * FROM messages WHERE task_id=? ORDER BY seq", (task["id"],))]
        return {"message": message, "task": task, "history": history, "lease": lease, "lease_until": until}

    def complete(self, message_id, lease, content, outcome="ok"):
        content = text_field(content, "content")
        if outcome not in ("ok", "blocked", "failed"):
            raise RelayError("outcome 必须为 ok、blocked 或 failed")
        with self.transaction() as db:
            delivery = db.execute("SELECT * FROM deliveries WHERE message_id=?", (message_id,)).fetchone()
            if not delivery or not isinstance(lease, str) or not secrets.compare_digest(delivery["lease"] or "", lease):
                raise RelayError("领取凭据不匹配", 409)
            if delivery["status"] == "done":
                answer = db.execute("SELECT * FROM messages WHERE reply_to=? AND kind='result'", (message_id,)).fetchone()
                if answer["content"] != content:
                    raise RelayError("已提交过不同的结果，不能覆盖", 409)
                return {"message_id": answer["id"], "task_id": answer["task_id"], "duplicate": True}
            if delivery["status"] not in ("leased", "uncertain"):
                raise RelayError("此消息已停止或重新排队，旧凭据已失效", 409)
            original = db.execute("SELECT * FROM messages WHERE id=?", (message_id,)).fetchone()
            mid = self._message(db, original["task_id"], original["round"], "result", "codex",
                                original["sender"], content, message_id)
            db.execute("UPDATE deliveries SET status='done' WHERE message_id=?", (message_id,))
            db.execute("UPDATE tasks SET status=CASE WHEN status='closed' THEN 'closed' ELSE 'waiting_review' END, "
                       "updated_at=? WHERE id=?", (self.clock(), original["task_id"]))
            db.execute("INSERT OR REPLACE INTO meta VALUES(?,?)", ("outcome:" + mid, outcome))
            status = self._task(db, original["task_id"])["status"]
        return {"message_id": mid, "task_id": original["task_id"], "status": status, "outcome": outcome, "duplicate": False}

    def renew(self, message_id, lease, lease_seconds=3600):
        integer(lease_seconds, "lease_seconds", 60, 86400)
        with self.transaction() as db:
            row = db.execute("SELECT * FROM deliveries WHERE message_id=?", (message_id,)).fetchone()
            if not row or not isinstance(lease, str) or not secrets.compare_digest(row["lease"] or "", lease):
                raise RelayError("领取凭据不匹配", 409)
            if row["status"] not in ("leased", "uncertain"):
                raise RelayError("任务已停止或已经回传", 409)
            task_id = db.execute("SELECT task_id FROM messages WHERE id=?", (message_id,)).fetchone()[0]
            if self._task(db, task_id)["status"] == "closed":
                raise RelayError("任务已关闭", 409)
            until = self.clock() + lease_seconds
            db.execute("UPDATE deliveries SET status='leased',lease_until=? WHERE message_id=?", (until, message_id))
            db.execute("UPDATE tasks SET status='running',updated_at=? WHERE id=?", (self.clock(), task_id))
        return {"message_id": message_id, "lease_until": until}

    def close_task(self, task_id):
        with self.transaction() as db:
            self._task(db, task_id)
            db.execute("UPDATE tasks SET status='closed',updated_at=? WHERE id=?", (self.clock(), task_id))
            db.execute("UPDATE deliveries SET status='closed' WHERE status IN ('queued','uncertain') "
                       "AND message_id IN (SELECT id FROM messages WHERE task_id=?)", (task_id,))
        return {"task_id": task_id, "status": "closed", "note": "停止后续投递；已经开始的聊天执行需要在 Codex 中手动停止"}

    def retry(self, task_id):
        # Explicit dashboard/CLI action only; MCP cannot re-run uncertain executions.
        with self.transaction() as db:
            task = self._task(db, task_id)
            if task["status"] != "stalled":
                raise RelayError("只能重试执行状态不确定的任务", 409)
            db.execute("UPDATE deliveries SET status='queued',lease=NULL,lease_until=NULL "
                       "WHERE status='uncertain' AND message_id IN (SELECT id FROM messages WHERE task_id=?)", (task_id,))
            db.execute("UPDATE tasks SET status='queued',updated_at=? WHERE id=?", (self.clock(), task_id))
        return {"task_id": task_id, "status": "queued"}

    def list_tasks(self, limit=100):
        integer(limit, "limit", 1, 200)
        with self.transaction() as db:
            self._expire(db)
            return [dict(row) for row in db.execute("SELECT * FROM tasks ORDER BY updated_at DESC LIMIT ?", (limit,))]

    def get_task(self, task_id):
        with self.transaction() as db:
            self._expire(db)
            task = self._task(db, task_id)
            messages = [dict(row) for row in db.execute("SELECT * FROM messages WHERE task_id=? ORDER BY seq", (task_id,))]
            for message in messages:
                if message["kind"] == "result":
                    outcome = db.execute("SELECT value FROM meta WHERE key=?", ("outcome:" + message["id"],)).fetchone()
                    message["outcome"] = outcome[0] if outcome else "ok"
            deliveries = [dict(row) for row in db.execute(
                "SELECT message_id,status,attempts,lease_until FROM deliveries "
                "WHERE message_id IN (SELECT id FROM messages WHERE task_id=?)", (task_id,))]
            return {"task": task, "messages": messages, "deliveries": deliveries}

    def wait(self, task_id, after_seq=0, timeout=25, cancelled=None):
        integer(after_seq, "after_seq", 0, 2**63-1)
        integer(timeout, "timeout", 0, 30)
        deadline = time.monotonic() + timeout
        while True:
            snapshot = self.get_task(task_id)
            messages = [m for m in snapshot["messages"] if m["seq"] > after_seq and m["sender"] == "codex"]
            if messages or snapshot["task"]["status"] in ("closed", "stalled") or time.monotonic() >= deadline:
                return {"task": snapshot["task"], "messages": messages,
                        "next_seq": max([after_seq] + [m["seq"] for m in messages]),
                        "timed_out": not messages and time.monotonic() >= deadline}
            if cancelled is not None and cancelled.wait(.25):
                return {"task": snapshot["task"], "messages": [], "next_seq": after_seq, "cancelled": True}
            if cancelled is None:
                time.sleep(.25)

    def binding(self):
        try:
            return json.loads((self.root / "binding.json").read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {"thread_id": None, "transport": "未绑定聊天"}

    def state(self):
        tasks = self.list_tasks()
        with contextlib.closing(self.connect()) as db:
            last = db.execute("SELECT value FROM meta WHERE key='last_poll'").fetchone()
            counts = {row["status"]: row["n"] for row in db.execute("SELECT status,COUNT(*) n FROM tasks GROUP BY status")}
        return {"binding": self.binding(), "tasks": tasks, "last_poll": float(last[0]) if last else None,
                "counts": {s: counts.get(s, 0) for s in
                           ("queued", "running", "waiting_review", "stalled", "closed")}}
