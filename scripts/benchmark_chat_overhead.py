"""Repeatable v2 tool-payload benchmark. Temporary rooms; no host/model calls.

Run on each source revision with --label before/after --output PATH.
The old API uses one combined answer plus one bulk ACK, not N replies.
"""
import argparse
import contextlib
import hashlib
import inspect
import json
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from chatroom import ChatRoom
from chatroom_mcp import ChatEndpoint


def size(value):
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode())


class Trace:
    def __init__(self, endpoint):
        self.endpoint, self.calls = endpoint, []

    def call(self, name, **args):
        arguments = {"room": "benchmark", **args}
        result = self.endpoint.invoke(name, arguments)
        text = json.dumps(result, ensure_ascii=False)
        self.calls.append({"tool": name, "arguments_bytes": size(arguments),
                           "result_json_bytes": size(result),
                           "mcp_text_bytes": len(text.encode()),
                           "mcp_envelope_bytes": size({"content": [{"type": "text", "text": text}],
                                                       "structuredContent": result, "isError": False})})
        return result

    def metrics(self):
        return {"calls": len(self.calls), **{key: sum(c[key] for c in self.calls)
                for key in ("arguments_bytes", "result_json_bytes", "mcp_text_bytes", "mcp_envelope_bytes")}}


@contextlib.contextmanager
def fixture(mode):
    with tempfile.TemporaryDirectory(prefix="relay-bench-") as tmp:
        chat = ChatRoom(Path(tmp), "benchmark")
        chat.create()
        chat.join("you", protocol_version=2, trusted_human=True)
        endpoint = ChatEndpoint(Path(tmp))
        try:
            endpoint.invoke("chat_join", {"room": "benchmark", "name": "agent", "client": mode, "protocol_version": 2})
            seed = chat.send("you", "约束：先审查方案，保留来源，尚未执行。", to="agent")
            batch = endpoint.invoke("chat_receive", {"room": "benchmark"})
            endpoint.invoke("chat_ack", {"room": "benchmark", "delivery_id": batch["delivery_id"], "message_ids": [seed["message_id"]]})
            endpoint.invoke("chat_checkpoint", {"room": "benchmark", "expected_version": 0, "entries": [
                {"speaker": "you", "kind": "constraint", "text": "保留用户的原始限制、不同成员的意见、来源和未完成事项。" * 15,
                 "sources": [seed["message_id"]]}]})
            yield chat, endpoint
        finally:
            endpoint.close()


def measure():
    scenarios = {}
    supports_batch = "ack_ids" in inspect.signature(ChatRoom.send).parameters
    for mode in ("manual", "claude"):
        with fixture(mode) as (chat, endpoint):
            sent = chat.send("you", "目前有什么需要我确认的？", to="agent")
            receive = Trace(endpoint)
            batch = receive.call("chat_receive")
            assert batch["messages"][0]["content"] == "目前有什么需要我确认的？"
            lookup = Trace(endpoint)
            found = lookup.call("chat_context", message_id=sent["message_id"])
            assert found["messages"][0]["content"] == batch["messages"][0]["content"]
            restore = Trace(endpoint)
            recovered = restore.call("chat_context")
            assert recovered["checkpoint"]["entries"][0]["text"] == "保留用户的原始限制、不同成员的意见、来源和未完成事项。" * 15
            assert recovered["checkpoint_version"] == 1
            reply = Trace(endpoint)
            result = reply.call("chat_send", content="当前没有待确认事项。", reply_to=sent["message_id"], delivery_id=batch["delivery_id"], key="short-reply")
            assert result["acknowledged"] and not result["pending"]
            if mode == "claude":
                assert result["background_task"]["run_in_background"]
            scenarios[mode + "_short_receive"] = receive.metrics()
            scenarios[mode + "_short_reply"] = reply.metrics()
            scenarios[mode + "_lookup"] = lookup.metrics()
            scenarios[mode + "_restore"] = restore.metrics()

        with fixture(mode) as (chat, endpoint):
            sent = [chat.send("you", text, to="agent") for text in ("请看方案", "暂不执行", "保留来源", "一起回答即可")]
            trace = Trace(endpoint)
            batch = trace.call("chat_receive")
            mids = [m["message_id"] for m in sent]
            assert [m["id"] for m in batch["messages"]] == mids
            result = trace.call("chat_send", content="已审查方案，来源已保留；尚未执行。", reply_to=mids[0], delivery_id=batch["delivery_id"],
                                key="combined-reply", **({"ack_ids": mids} if supports_batch else {}))
            if not supports_batch:
                result = trace.call("chat_ack", delivery_id=batch["delivery_id"], message_ids=mids[1:])
            assert not result["pending"]
            if mode == "claude":
                assert result["background_task"]["run_in_background"]
            with chat.store.connect() as db:
                assert db.execute("SELECT COUNT(*) FROM chat_messages WHERE sender='agent'").fetchone()[0] == 1
                assert db.execute("SELECT COUNT(*) FROM chat_receipts WHERE delivery_id=? AND acked=1", (batch["delivery_id"],)).fetchone()[0] == 4
            scenarios[mode + "_four_message_roundtrip"] = trace.metrics()

    for budget in (4000, 8000):
        with fixture("manual") as (chat, endpoint):
            content = "详细约束与证据引用需要原样保留。" * 500
            sent = chat.send("you", content, to="agent")
            trace = Trace(endpoint)
            batch = trace.call("chat_receive")
            page = batch["messages"][0]
            text, repeated_checkpoint_bytes = page["content"], 0
            while not page["complete"]:
                packet = trace.call("chat_context", message_id=sent["message_id"], delivery_id=batch["delivery_id"], offset=page["next_offset"], budget=budget)
                if packet.get("checkpoint"):
                    repeated_checkpoint_bytes += size(packet["checkpoint"])
                assert size(packet) <= budget
                page = packet["messages"][0]
                text += page["content"]
            assert text == content
            result = trace.call("chat_ack", delivery_id=batch["delivery_id"], message_ids=[sent["message_id"]])
            assert not result["pending"]
            scenarios["long_24000_bytes_budget_" + str(budget)] = {**trace.metrics(), "content_bytes": len(content.encode()),
                                                                 "repeated_checkpoint_bytes": repeated_checkpoint_bytes}
    return {"supports_batch_reply": supports_batch, "scenarios": scenarios}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--label", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    # Identical timestamp widths and body/summary content across revisions.
    # UUID values vary, their serialized widths are fixed. Temp path lengths match.
    with patch("time.time", return_value=1792771200.0):
        result = measure()
    result.update(label=args.label, measurement="UTF-8 serialized bytes and tool calls; not host tokens or billing",
                  source_sha256={name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
                                 for name in ("chatroom.py", "chat_context.py", "chat_delivery.py", "chatroom_mcp.py", "chat_background.py")})
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))
