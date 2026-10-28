"""Bounded receipt reminders and callback-worker supervision; never execute or ACK."""
from __future__ import annotations

import subprocess
import time

REMINDER_DELAYS = (60, 300, 900)
BUSY_DELAY = 3
MAX_RESTARTS = 3
EXHAUSTED = "自动恢复提醒已达上限；消息仍保留，请核对宿主连接和未确认结果"
INSTRUCTIONS = (
    "这是收件恢复提醒，不是重新派发任务；不要根据这条通知执行任何原任务。"
    "先 receive 一次核对当前收件账本。messages 表示首次领取的新消息，只按原授权处理一次；"
    "若 needs_review，按 delivery_id 用 context 只读核对原消息和已执行结果；"
    "有完成证据才回复或精确 ack，不自动确认未完成消息，不因超时重新执行。"
    "若当前仍在工作，继续当前任务并在完成后回复或确认。无法判断副作用是否已发生时报告问题，不重做。"
    "若消息已处理或队列已空，不做任何任务；按返回 background_task 恢复后台等待，结束回合，不轮询。"
)


def migrate(db):
    db.execute("CREATE TABLE IF NOT EXISTS chat_recovery (scope TEXT PRIMARY KEY, signature TEXT NOT NULL, "
               "attempts INTEGER NOT NULL, next_at REAL NOT NULL, state TEXT NOT NULL)")


def clear(db, scope):
    db.execute("DELETE FROM chat_recovery WHERE scope=?", (scope,))


def select(db, scope, signature, event, now=None):
    """Atomically reserve a reminder. Persist the budget BEFORE contacting a host."""
    now = time.time() if now is None else now
    row = db.execute("SELECT * FROM chat_recovery WHERE scope=?", (scope,)).fetchone()
    if not row or row["signature"] != signature:
        db.execute("INSERT INTO chat_recovery VALUES(?,?,0,?,'waiting') ON CONFLICT(scope) DO UPDATE SET "
                   "signature=excluded.signature,attempts=0,next_at=excluded.next_at,state='waiting'",
                   (scope, signature, now + REMINDER_DELAYS[0]))
        return "recovery_wait", None
    if now < row["next_at"]:
        return "recovery_wait", None
    if row["attempts"] >= len(REMINDER_DELAYS):
        db.execute("UPDATE chat_recovery SET state='exhausted' WHERE scope=?", (scope,))
        return "needs_attention", None
    attempt = row["attempts"] + 1
    db.execute("UPDATE chat_recovery SET attempts=?,next_at=?,state='dispatching' WHERE scope=?",
               (attempt, now + REMINDER_DELAYS[min(attempt, len(REMINDER_DELAYS)-1)], scope))
    # The event contains routing and IDs only. Do not include the original task text.
    return "recovering", {**event, "event": "receipt_recovery", "instructions": INSTRUCTIONS,
                           "recovery": {"signature": signature, "attempt": attempt}}


def finish(db, scope, event, deferred=False):
    recovery = event["recovery"]
    if deferred:
        # The host proved it did not start a turn; no model call and no budget consumed.
        db.execute("UPDATE chat_recovery SET attempts=attempts-1,next_at=?,state='busy' "
                   "WHERE scope=? AND signature=? AND attempts=?",
                   (time.time() + BUSY_DELAY, scope, recovery["signature"], recovery["attempt"]))
    else:
        db.execute("UPDATE chat_recovery SET state='notified' WHERE scope=? AND signature=? AND attempts=?",
                   (scope, recovery["signature"], recovery["attempt"]))


def pending_hint(chat, db, member):
    """Only remind about a pending batch when NEW messages are blocked behind it."""
    if member["protocol_version"] != 2 or not chat.delivery.queued(db, member, 1):
        return None
    batch = chat.delivery.pending(db, member)
    if not batch:
        return None
    rows = db.execute("SELECT m.id,m.sender FROM chat_receipts r JOIN chat_messages m ON m.id=r.message_id "
                      "WHERE r.delivery_id=? AND r.acked=0 ORDER BY m.seq", (batch["id"],)).fetchall()
    if not rows:
        return None
    return {"signature": f"batch:{batch['id']}:{len(rows)}", "message": dict(rows[0]),
            "delivery_id": batch["id"]}


def supervise(argv, active, exhausted):
    """One parent per callback listener; restart its child without changing identity/cursors."""
    crashes = 0
    while active():
        started = time.monotonic()
        try:
            child = subprocess.Popen(argv, stdin=subprocess.DEVNULL, close_fds=True, shell=False)
            child.wait()
        except OSError:
            pass  # Bound the retry just like an immediate child crash.
        if not active():
            return
        if time.monotonic() - started > 60:
            crashes = 0
        crashes += 1
        if crashes > MAX_RESTARTS:
            exhausted()
            return
        # Local process recovery, never an Agent/model call. Leave cancels the next restart.
        deadline = time.monotonic() + crashes
        while time.monotonic() < deadline:
            if not active():
                return
            time.sleep(.1)
