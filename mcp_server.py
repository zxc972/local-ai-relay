"""Small stdio MCP adapter. It shares SQLite with the local chat bridge."""
from __future__ import annotations

import argparse
import concurrent.futures
import json
import sys
import threading

if __package__:
    from .core import DEFAULT_DATA, RelayError, Store
    from .message_bus_mcp import INSTRUCTIONS as MAILBOX_INSTRUCTIONS, TOOLS as MAILBOX_TOOLS, Endpoint
else:
    from core import DEFAULT_DATA, RelayError, Store
    from message_bus_mcp import INSTRUCTIONS as MAILBOX_INSTRUCTIONS, TOOLS as MAILBOX_TOOLS, Endpoint


def schema(properties, required):
    return {"type": "object", "properties": properties, "required": required, "additionalProperties": False}


STRING = {"type": "string", "minLength": 1}
TASK = {"task_id": STRING}
TOOLS = [
    {"name": "relay_send_task", "description": "向当前绑定的 Codex 聊天投递任务。异步执行，通常等待下一次聊天收件。保存 task_id，用 relay_wait 领取结果。网络/客户端重试时复用同一个 idempotency_key。",
     "inputSchema": schema({"title": {"type": "string", "minLength": 1, "maxLength": 160},
                            "content": STRING, "idempotency_key": STRING,
                            "max_rounds": {"type": "integer", "minimum": 1, "maximum": 12, "default": 4}},
                           ["title", "content", "idempotency_key"])},
    {"name": "relay_get_task", "description": "读取同一任务的完整消息记录与投递状态。只有 result 消息才是 Codex 回传的结果。",
     "inputSchema": schema(TASK, ["task_id"])},
    {"name": "relay_wait", "description": "等待 Codex 的新结果，最多 30 秒。首次 after_seq=0，后续使用返回的 next_seq；超时只表示尚无新结果。若需继续等候，可再次调用；用户停止时结束等待。",
     "inputSchema": schema({**TASK, "after_seq": {"type": "integer", "minimum": 0, "default": 0},
                            "timeout": {"type": "integer", "minimum": 0, "maximum": 30, "default": 25}}, ["task_id"])},
    {"name": "relay_feedback", "description": "对最新 Codex 结果给出意见并请求下一轮修改。reply_to 填最新 result 的 message id。受任务 max_rounds 限制。幂等重试必须复用 idempotency_key。",
     "inputSchema": schema({**TASK, "content": STRING, "reply_to": STRING, "idempotency_key": STRING},
                           ["task_id", "content", "reply_to", "idempotency_key"])},
    {"name": "relay_close", "description": "任务满意或无需继续时结束往返。停止后续投递，已经在运行的 Codex 工作需要用户在 Codex 中停止。",
     "inputSchema": schema(TASK, ["task_id"])},
    {"name": "relay_list_tasks", "description": "列出本地中转站任务及状态。不会读取其他 Codex 或 Claude 聊天。",
     "inputSchema": schema({"limit": {"type": "integer", "minimum": 1, "maximum": 200, "default": 50}}, [])},
]
BY_NAME = {t["name"]: t for t in TOOLS}
for tool in TOOLS:
    tool["annotations"] = {"readOnlyHint": tool["name"] in ("relay_get_task", "relay_wait", "relay_list_tasks"),
                           "openWorldHint": False}


def validate(name, arguments):
    if name not in BY_NAME:
        raise RelayError("未知工具")
    if not isinstance(arguments, dict):
        raise RelayError("arguments 必须是对象")
    spec = BY_NAME[name]["inputSchema"]
    if set(arguments) - set(spec["properties"]):
        raise RelayError("参数包含未定义的字段")
    if any(k not in arguments for k in spec["required"]):
        raise RelayError("缺少必填参数")
    for k, value in arguments.items():
        s = spec["properties"][k]
        if s["type"] == "string" and (not isinstance(value, str) or not value.strip()):
            raise RelayError(f"{k} 必须是非空文本")
        if s["type"] == "integer" and (type(value) is not int or value < s.get("minimum", 0) or value > s.get("maximum", 2**63-1)):
            raise RelayError(f"{k} 不是允许范围内的整数")


def invoke(store, name, arguments, cancelled=None):
    validate(name, arguments)
    if name == "relay_send_task":
        return store.send_task(**arguments)
    if name == "relay_get_task":
        return store.get_task(**arguments)
    if name == "relay_wait":
        return store.wait(**arguments, cancelled=cancelled)
    if name == "relay_feedback":
        return store.feedback(**arguments)
    if name == "relay_close":
        return store.close_task(**arguments)
    return {"tasks": store.list_tasks(**arguments)}


def serve(store, source=sys.stdin, destination=sys.stdout, legacy=False):
    for stream in (source, destination):
        if stream in (sys.stdin, sys.stdout) and hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    output_lock = threading.Lock()
    active_lock = threading.Lock()
    active = {}
    initialized = False
    available_tools = TOOLS if legacy else MAILBOX_TOOLS
    mailbox_endpoint = Endpoint(store.root)

    def emit(message):
        with output_lock:
            destination.write(json.dumps(message, ensure_ascii=False) + "\n")
            destination.flush()

    def execute(request, cancel):
        request_id = request["id"]
        try:
            params = request.get("params", {})
            metadata = params.get("_meta", {})
            token = metadata.get("progressToken") if isinstance(metadata, dict) else None
            count = 0
            def progress():
                nonlocal count
                if type(token) not in (str, int) or cancel.is_set():
                    return
                count += 1
                emit({"jsonrpc": "2.0", "method": "notifications/progress", "params": {
                    "progressToken": token, "progress": count, "message": "正在监听中转站消息"}})
            if legacy:
                result = invoke(store, params.get("name"), params.get("arguments", {}), cancel)
            else:
                result = mailbox_endpoint.invoke(params.get("name"), params.get("arguments", {}), cancel, progress)
            response = {"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}],
                        "structuredContent": result, "isError": False}
        except (RelayError, TypeError, ValueError) as error:
            response = {"content": [{"type": "text", "text": str(error)}], "isError": True}
        except Exception:
            print("Local AI Relay: internal tool failure", file=sys.stderr)
            response = {"content": [{"type": "text", "text": "本地中转站内部错误，请查看日志"}], "isError": True}
        finally:
            with active_lock:
                active.pop(request_id, None)
        if not cancel.is_set():
            emit({"jsonrpc": "2.0", "id": request_id, "result": response})

    pool = concurrent.futures.ThreadPoolExecutor(max_workers=4)
    try:
        for line in source:
            if len(line.encode("utf-8")) > 1_000_000:
                emit({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Message too large"}})
                continue
            try:
                request = json.loads(line)
            except ValueError:
                emit({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}})
                continue
            if not isinstance(request, dict) or request.get("jsonrpc") != "2.0":
                emit({"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Invalid request"}})
                continue
            method = request.get("method")
            params = request.get("params", {})
            if not isinstance(params, dict):
                emit({"jsonrpc": "2.0", "id": request.get("id"), "error": {"code": -32602, "message": "Invalid params"}})
                continue
            if method == "notifications/cancelled":
                with active_lock:
                    request_id = params.get("requestId")
                    event = active.get(request_id) if type(request_id) in (str, int) else None
                if event:
                    event.set()
                continue
            if "id" not in request:
                continue
            rid = request["id"]
            if not isinstance(rid, (str, int)) or isinstance(rid, bool):
                emit({"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Invalid id"}})
                continue
            if method == "initialize":
                initialized = True
                offered = request.get("params", {}).get("protocolVersion")
                version = offered if offered in ("2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25") else "2025-06-18"
                result = {"protocolVersion": version, "capabilities": {"tools": {}},
                          "serverInfo": {"name": "local-ai-relay", "version": "0.3.0"},
                          "instructions": ("本地双 AI 任务邮箱。保存 task_id，等待结果后评审。" if legacy else MAILBOX_INSTRUCTIONS)}
            elif not initialized:
                emit({"jsonrpc": "2.0", "id": rid, "error": {"code": -32002, "message": "Initialize first"}})
                continue
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": available_tools}
            elif method == "tools/call":
                with active_lock:
                    if rid in active or len(active) >= 16:
                        emit({"jsonrpc": "2.0", "id": rid, "error": {"code": -32000, "message": "Busy or duplicate request id"}})
                        continue
                    event = threading.Event()
                    active[rid] = event
                pool.submit(execute, request, event)
                continue
            else:
                emit({"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": "Method not found"}})
                continue
            emit({"jsonrpc": "2.0", "id": rid, "result": result})
    finally:
        with active_lock:
            for event in active.values():
                event.set()
        pool.shutdown(wait=True, cancel_futures=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Local AI Relay MCP (stdio)")
    parser.add_argument("--data-dir", default=str(DEFAULT_DATA))
    parser.add_argument("--legacy-tasks", action="store_true", help="使用旧的 Codex 任务工具；默认提供通用双向消息工具")
    args = parser.parse_args()
    serve(Store(args.data_dir), legacy=args.legacy_tasks)
