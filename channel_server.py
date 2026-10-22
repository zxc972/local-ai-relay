"""Claude Code channel over stdio, using the same durable sender/receiver inbox."""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
import uuid

if __package__:
    from .auto_receive import check_no_background, read_state, write_state
else:
    from auto_receive import check_no_background, read_state, write_state
if __package__:
    from .connection import Connection, RULES
else:
    from connection import Connection, RULES
if __package__:
    from .core import DEFAULT_DATA, RelayError
else:
    from core import DEFAULT_DATA, RelayError
if __package__:
    from .message_bus import Mailbox
else:
    from message_bus import Mailbox
if __package__:
    from .room_cli import Room
else:
    from room_cli import Room


TOOLS = [
    {"name": "relay_ready", "description": "确认当前 Claude 聊天已启用推送通道。连接后先调用一次；不发送任务。",
     "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False}},
    {"name": "relay_receive", "description": "收到通道提醒后领取一条消息。领取成功后才处理；不能因重复提醒重复执行。",
     "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False}},
    {"name": "relay_send", "description": "发送用户指定内容或回传真实结果。回传时 reply_to 指向已领取消息。不会自行创建任务。",
     "inputSchema": {"type": "object", "properties": {
         "content": {"type": "string", "minLength": 1, "maxLength": 100000},
         "reply_to": {"type": "string"}, "key": {"type": "string"}},
         "required": ["content"], "additionalProperties": False}},
    {"name": "relay_ack", "description": "确认已处理的消息。不需要回复的结果或通知应确认，不要自动发起下一轮。",
     "inputSchema": {"type": "object", "properties": {"message_id": {"type": "string"}},
                     "required": ["message_id"], "additionalProperties": False}},
]


def serve(room, role, source=sys.stdin, destination=sys.stdout):
    for stream in (source, destination):
        if stream in (sys.stdin, sys.stdout) and hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    connection = Connection(room)
    mailbox = Mailbox(room.base, room.name)
    instance = uuid.uuid4().hex
    stopped, confirmed = threading.Event(), threading.Event()
    output_lock = threading.Lock()
    initialized = False
    watcher = None
    client_name = "unknown"

    def emit(value):
        with output_lock:
            destination.write(json.dumps(value, ensure_ascii=False) + "\n")
            destination.flush()

    def watch():
        last_notified = None
        last_check = 0
        try:
            while not stopped.wait(.2):
                notice = None
                with room.store.transaction() as db:
                    state = read_state(db, role)
                    if not state.get("enabled") or state.get("instance") != instance:
                        return
                    connection.check_peer(db, role)
                    if time.time() - last_check >= 2:
                        last_check = time.time()
                        write_state(db, role, {**state, "last_checked_at": last_check})
                    busy = db.execute("SELECT 1 FROM channel_messages WHERE recipient=? AND state='received'",
                                      (role,)).fetchone()
                    if confirmed.is_set() and not busy:
                        notice = db.execute("SELECT id,sender FROM channel_messages WHERE recipient=? "
                                            "AND state='queued' ORDER BY seq LIMIT 1", (role,)).fetchone()
                if notice and notice["id"] != last_notified:
                    emit({"jsonrpc": "2.0", "method": "notifications/claude/channel", "params": {
                        "content": "房间收到新消息，请调用 relay_receive 领取后检查并处理。重复提醒不能重复执行。",
                        "meta": {"room": room.name, "message_id": notice["id"], "sender": notice["sender"]}}})
                    last_notified = notice["id"]
        except (OSError, RelayError) as error:
            print("AI Relay channel: " + str(error), file=sys.stderr)
            stopped.set()

    def start():
        nonlocal watcher
        if watcher is not None:
            return
        connection.join(role)
        with room.store.transaction() as db:
            # An explicit auto-off stays off across restarts until enabled again.
            check_no_background(db, role)
            state = read_state(db, role)
            enabled = state.get("enabled", False)
            write_state(db, role, {**state, "enabled": enabled, "client": "claude", "instance": instance,
                                   "pid": os.getpid(), "protocol_client": client_name,
                                   "confirmed_at": None, "last_checked_at": time.time(),
                                   "status": "awaiting_client_confirmation" if enabled else "off"})
        watcher = threading.Thread(target=watch, daemon=True)
        watcher.start()

    try:
        for line in source:
            if len(line) > 1_000_000:
                emit({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Message too large"}})
                continue
            try:
                request = json.loads(line)
                if not isinstance(request, dict) or request.get("jsonrpc") != "2.0":
                    raise ValueError("Invalid request")
            except ValueError:
                emit({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}})
                continue
            method, rid = request.get("method"), request.get("id")
            params = request.get("params", {})
            if not isinstance(params, dict):
                emit({"jsonrpc": "2.0", "id": rid, "error": {"code": -32602, "message": "Invalid params"}})
                continue
            if method == "notifications/initialized" and initialized:
                start()
                continue
            if "id" not in request:
                continue
            if method == "initialize":
                info = params.get("clientInfo", {})
                client_name = str(info.get("name", "unknown")) if isinstance(info, dict) else "unknown"
                offered = params.get("protocolVersion")
                version = offered if offered in ("2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25") else "2025-06-18"
                result = {"protocolVersion": version,
                          "capabilities": {"tools": {}, "experimental": {"claude/channel": {}}},
                          "serverInfo": {"name": "airelay-channel", "version": "1.0.0"},
                          "instructions": RULES + f"你是房间 {room.name} 的 {role}。"
                          "连接后首先调用 relay_ready 确认推送通道已加载，然后简短确认并待命，不发送测试消息或任务。"
                          "收到 <channel> 提醒时自动调用 relay_receive；只有领取成功后才处理该消息。"
                          "sender 是高阶指挥方，使用 relay_send 下发指令，收到结果后审阅并决定认可或继续指令。"
                          "receiver 是执行方，只接收指令并回传执行结果，回传仍调用 relay_send，但必须填写 reply_to=原指令id。"
                          "sender 下发后续修正指令可填写 reply_to=本轮结果id，原子确认结果并发送下一条指令。"
                          "认可结果后 relay_ack，不发送无目的的礼貌回复；receiver 不能反向下达指令。"
                          "通道在后台推送，无需运行 receive 轮询或要求用户手动提醒；权限审批仍由用户处理。"}
                initialized = True
            elif not initialized:
                emit({"jsonrpc": "2.0", "id": rid, "error": {"code": -32002, "message": "Initialize first"}})
                continue
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": TOOLS}
            elif method == "tools/call":
                try:
                    if watcher is None:
                        raise RelayError("请先完成 notifications/initialized")
                    name, arguments = params.get("name"), params.get("arguments", {})
                    tool = next((t for t in TOOLS if t["name"] == name), None)
                    if not tool or not isinstance(arguments, dict):
                        raise RelayError("工具或参数无效")
                    spec = tool["inputSchema"]
                    if set(arguments) - set(spec["properties"]) or any(k not in arguments for k in spec.get("required", [])):
                        raise RelayError("参数包含未定义字段或缺少必填字段")
                    if any(not isinstance(v, str) for v in arguments.values()):
                        raise RelayError("参数必须为文本")
                    if name == "relay_ready":
                        with room.store.transaction() as db:
                            state = read_state(db, role)
                            if not state.get("enabled") or state.get("instance") != instance:
                                raise RelayError("自动收件已关闭或另一个通道已接管")
                            write_state(db, role, {**state, "status": "listening", "confirmed_at": time.time(),
                                                   "last_checked_at": time.time()})
                        confirmed.set()
                        value = {"room": room.name, "role": role, "status": "listening", "message": None}
                    elif name == "relay_receive":
                        with room.store.transaction() as db:
                            state = read_state(db, role)
                            if not state.get("enabled") or state.get("instance") != instance:
                                raise RelayError("自动收件已关闭或此通道已失效")
                        value = connection.receive(role, timeout=0)
                    elif name == "relay_send":
                        if role == "receiver":
                            if not arguments.get("reply_to"):
                                raise RelayError("receiver 只能回传结果，必须指定 reply_to 原指令ID")
                            value = mailbox.reply(role, arguments["reply_to"], arguments["content"])
                        else:
                            value = mailbox.send(role, **arguments)
                    else:
                        if role != "sender":
                            raise RelayError("receiver 应先回传执行结果来确认原指令，不能只确认后跳过回报")
                        value = connection.ack(role, **arguments)
                    result = {"content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False)}],
                              "structuredContent": value, "isError": False}
                except (RelayError, TypeError, ValueError) as error:
                    result = {"content": [{"type": "text", "text": str(error)}], "isError": True}
            else:
                emit({"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": "Method not found"}})
                continue
            emit({"jsonrpc": "2.0", "id": rid, "result": result})
    finally:
        stopped.set()
        if watcher:
            watcher.join(timeout=3)
        with room.store.transaction() as db:
            state = read_state(db, role)
            if state.get("instance") == instance:
                write_state(db, role, {**state, "status": "not_running" if state.get("enabled") else "off"})


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Local AI Relay Claude channel (stdio)")
    parser.add_argument("--data-dir", default=str(DEFAULT_DATA))
    parser.add_argument("--room", required=True)
    parser.add_argument("--as", dest="role", choices=["sender", "receiver"], required=True)
    args = parser.parse_args()
    serve(Room(args.room, args.data_dir), args.role)
