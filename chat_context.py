"""Private, bounded checkpoints on the member's device. No model or timer."""
from __future__ import annotations

import contextlib
import hashlib
import json
import sqlite3
import time

if __package__:
    from .chat_delivery import BOUNDARY, RULES_V2, encoded, ids, units
    from .core import RelayError, Store, integer
else:
    from chat_delivery import BOUNDARY, RULES_V2, encoded, ids, units
    from core import RelayError, Store, integer

CHECKPOINT_BUDGET = 1500
CHECKPOINT_INTERVAL = 6000
KINDS = ("constraint", "decision", "claim", "completed", "pending", "question", "disagreement")
HOST_CAPABILITIES = {"usage": "unknown", "compact_control": "unsupported",
                     "compact_completion_event": False, "policy": "host_managed"}


def validate_entries(entries):
    if not isinstance(entries, list) or not 1 <= len(entries) <= 12:
        raise RelayError("entries 需要 1–12 条结构化记录")
    references = set()
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {"speaker", "kind", "text", "sources"}:
            raise RelayError("每条记录只包含 speaker、kind、text、sources")
        if entry["kind"] not in KINDS:
            raise RelayError("记录 kind 无效；保留约束、提案/声明、完成、待办和分歧的区别")
        for key in ("speaker", "text"):
            if not isinstance(entry[key], str) or not entry[key].strip():
                raise RelayError(key + " 必须是非空文本")
        references.update(ids(entry["sources"], "sources", 12))
    if len(references) > 32 or units(entries) > CHECKPOINT_BUDGET:
        raise RelayError("检查点超出 1500 UTF-8 字节上界预算或 32 个来源；精简正文，保留原文 ID", 413)
    return sorted(references)


class LocalContext:
    def __init__(self, root, scope, info, history, sources):
        self.info, self.history, self.sources = info, history, sources
        self.identity = info()
        if self.identity["protocol_version"] != 2:
            raise RelayError("上下文管理需要 protocol_version=2；先处理旧收件再升级", 409)
        key = hashlib.sha256(encoded([scope, self.identity["member_id"], self.identity["membership_epoch"]]).encode()).hexdigest()
        self.store = Store(root / "private-context" / key)
        with contextlib.closing(self.store.connect()) as db:
            db.execute("CREATE TABLE IF NOT EXISTS checkpoints(version INTEGER PRIMARY KEY,payload TEXT NOT NULL,"
                       "processed_units INTEGER NOT NULL,created_at REAL NOT NULL)")

    @classmethod
    def for_room(cls, chat, name, owner):
        return cls(chat.room.base, ["local", chat.name],
                   lambda: chat.context_info(name, owner),
                   lambda **args: chat.delivery.context(name, owner, **args),
                   lambda values: chat.delivery.sources(name, owner, values))

    @classmethod
    def for_remote(cls, remote):
        # Only identity, message IDs and history queries cross the network.
        # The entries/summary are never forwarded to the room service.
        return cls(remote.base, [remote.server, remote.name],
                   remote.context_info,
                   lambda **args: remote.call("context", args),
                   lambda values: remote.call("sources", {"message_ids": values}))

    def latest(self):
        with contextlib.closing(self.store.connect()) as db:
            row = db.execute("SELECT * FROM checkpoints ORDER BY version DESC LIMIT 1").fetchone()
        return dict(row) if row else None

    def hint(self):
        previous = self.latest()
        delta = max(0, self.identity["processed_units"] - (previous["processed_units"] if previous else 0))
        return {"checkpoint_version": previous["version"] if previous else 0,
                "checkpoint_due": delta >= CHECKPOINT_INTERVAL,
                "processed_since_checkpoint": delta, "unit": "utf8_bytes_upper_bound"}

    def context(self, budget=8000, **args):
        integer(budget, "budget", 4000, 8000)
        restore = all(args.get(key) is None for key in ("message_id", "delivery_id", "after_seq"))
        packet = self.history(budget=budget - 2500 if restore else min(6000, budget - 512), **args)
        if (packet["member_id"], packet["membership_epoch"]) != (self.identity["member_id"], self.identity["membership_epoch"]):
            raise RelayError("成员身份已变化；重新接入后再恢复上下文", 409)
        if not restore:
            # Exact reads/paging do not need the private checkpoint or its
            # source validation. The server has already checked this raw read.
            for key in ("member_id", "membership_epoch", "processed_units"):
                packet.pop(key, None)
            packet["budget"]["limit"] = budget
            if units(packet) > budget:
                raise RelayError("原文和元数据超出读取预算", 413)
            return packet
        # A source may have become inaccessible; never emit a stale checkpoint
        # without rechecking its references under the current identity.
        saved = self.latest()
        checkpoint = json.loads(saved["payload"]) if saved else None
        if checkpoint:
            self.sources(sorted({mid for e in checkpoint["entries"] for mid in e["sources"]}))
        packet.update(checkpoint=checkpoint, checkpoint_version=saved["version"] if saved else 0,
                      host_context=HOST_CAPABILITIES, instructions=RULES_V2,
                      context_budget={"limit": budget, "unit": "utf8_bytes_upper_bound", "estimated": True})
        packet["security"] = {key: self.identity.get("security", {}).get(key)
                              for key in ("policy", "execution")}
        packet["checkpoint_hint"] = self.hint()
        # Envelope budget is enforced as serialized bytes, including metadata.
        while units(packet) > budget and packet["messages"]:
            packet["messages"].pop(0 if args.get("after_seq") is None and not args.get("delivery_id") else -1)
            packet["has_more"] = True
        if args.get("after_seq") is not None:
            packet["next_after_seq"] = packet["messages"][-1]["seq"] if packet["messages"] else args["after_seq"]
        if units(packet) > budget:
            raise RelayError("检查点与元数据超出恢复预算", 413)
        return packet

    def save(self, entries, expected_version):
        integer(expected_version, "expected_version", 0, 2**63 - 1)
        references = validate_entries(entries)
        self.sources(references)
        current = self.info()
        if (current["member_id"], current["membership_epoch"]) != (self.identity["member_id"], self.identity["membership_epoch"]):
            raise RelayError("成员身份已变化；检查点未写入", 409)
        payload = {"schema": "airelay.context.v1", "entries": entries}
        if units(payload) > CHECKPOINT_BUDGET:
            raise RelayError("检查点连同元数据超出 1500 字节预算", 413)
        with self.store.transaction() as db:
            latest = db.execute("SELECT COALESCE(MAX(version),0) FROM checkpoints").fetchone()[0]
            if latest != expected_version:
                raise RelayError("检查点版本已变化；先 context 读取当前版本，不能覆盖新状态", 409)
            version = latest + 1
            db.execute("INSERT INTO checkpoints VALUES(?,?,?,?)", (version, encoded(payload), current["processed_units"], time.time()))
            db.execute("DELETE FROM checkpoints WHERE version<?", (version - 1,))
        return {"status": "checkpoint_saved", "version": version, "local_only": True,
                "instructions": BOUNDARY + "保存检查点不确认消息，也不代表宿主上下文已压缩。"}


def with_local_hint(chat, name, owner, result):
    if result.get("protocol_version") == 2:
        try:
            add_due_hint(result, LocalContext.for_room(chat, name, owner).hint())
        except (OSError, sqlite3.DatabaseError, RelayError):
            result["context_warning"] = "检查点提示暂不可用；以上发送/收件/确认结果仍有效，不重复操作。"
    return result


def add_due_hint(result, hint):
    if hint["checkpoint_due"]:
        result["checkpoint_hint"] = {key: hint[key] for key in ("checkpoint_version", "checkpoint_due")}
