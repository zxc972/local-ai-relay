"""Client-neutral connect/send/listen/reply protocol shared by MCP and CLI."""
from __future__ import annotations

import contextlib
import json
import time
import uuid

if __package__:
    from .connection import Connection, ROLES, ROLE_INFO, RULES, other_role
    from .core import RelayError, integer
    from .room_cli import Room
    from .background import Background
else:
    from connection import Connection, ROLES, ROLE_INFO, RULES, other_role
    from core import RelayError, integer
    from room_cli import Room
    from background import Background

INSTRUCTIONS = (
    "这是分工明确的通用消息中间件，不绑定客户端或模型。sender 是高阶指挥 Agent，receiver 是执行 Agent。"
    "sender 根据用户目标下发指令、审阅结果并决定后续指令；receiver 只接收指令、执行并回传结果或问题。"
    "先 relay_connect，只建立连接，不从上下文推导任务、不发送测试消息。"
    "空闲监听由本地后台进程负责，严禁让模型循环调用 relay_listen、receive 或 status。"
    "连接后没有新消息就结束本轮待命；后台事件抵达时只调用一次 relay_receive 领取。"
    "receiver 收到 command 后用 relay_reply 对原指令回传；sender 收到 result 后审阅，认可时 relay_ack，"
    "需要继续时用 relay_send 下发下一条指令，可用 reply_to 关联并确认本次结果。"
    "relay_send 仅限 sender，relay_reply 仅限 receiver，receiver 不能反向下达指令。"
    "只连接时不发送工作任务。用户停止时 relay_disconnect 并结束监听。"
    "若连接返回 needs_adapter，须接入宿主的事件适配器，不能声称自动监听已开启，也不能用模型轮询代替。"
    + RULES
)


def next_listen(room, role, timeout=25):
    return {"name": "relay_listen", "arguments": {"room": room, "role": role, "timeout": timeout}}


def policy(db, role):
    row = db.execute("SELECT value FROM meta WHERE key=?", ("mailbox:policy:" + role,)).fetchone()
    return json.loads(row[0]) if row else {"listen": False}


def set_policy(db, role, value):
    db.execute("INSERT OR REPLACE INTO meta VALUES(?,?)", ("mailbox:policy:" + role, json.dumps(value)))


class Mailbox:
    def __init__(self, data_dir, room):
        self.room = Room(room, data_dir)
        self.connection = Connection(self.room)

    def followup(self, role):
        # No empty tool result should instruct the model to run another wait.
        return None

    def connect(self, role, listen=True):
        other_role(role)
        if type(listen) is not bool:
            raise RelayError("listen 必须是布尔值")
        with self.room.store.transaction() as db:
            self.connection.check_peer(db, role, required=False)
            db.execute("INSERT INTO channel_peers VALUES(?,?,?) ON CONFLICT(role) DO UPDATE SET "
                       "session_id=COALESCE(channel_peers.session_id,excluded.session_id)",
                       (role, self.connection.session_id(), self.room.store.clock()))
            previous = policy(db, role)
            set_policy(db, role, {"listen": listen,
                                 "generation": previous.get("generation", uuid.uuid4().hex),
                                 "connected_at": time.time()})
            result = self.connection.snapshot(db, role)
        purpose = ("你是高阶 sender：负责下发指令、审阅 receiver 的执行结果并决定下一步。"
                   if role == "sender" else
                   "你是执行 receiver：负责接收 sender 的指令、执行并回传结果或问题，不能反向分派任务。")
        background = Background(self.room)
        state = background.status(role)
        if listen and state.get("configured") and not state.get("running") and not state.get("worker_active"):
            background.start(role)
            state = background.status(role)
        elif not listen and state.get("enabled"):
            background.stop(role)
            state = background.status(role)
        mode = "background" if state.get("running") else ("needs_adapter" if listen else "standby")
        if state.get("worker_active") and not state.get("running"):
            mode = "stopping"
        elif state.get("configured") and not state.get("running") and listen:
            mode = "not_running"
        if state.get("needs_attention") or state.get("error"):
            mode = "needs_attention"
        return {**result, "transport": "mcp", "mode": mode, "listen_enabled": bool(state.get("running")),
                "background": state,
                "role_info": ROLE_INFO[role],
                "next_tool": None,
                "instructions": RULES + purpose + "本次只建立连接，没有发送任务。"
                "结束本轮待命，不循环调用监听或状态工具。background.running 为真时由后台进程等待新消息；"
                "mode=needs_adapter 表示尚缺宿主事件接入，应报告该状态，不得用 AI 轮询代替。"
                "事件到达时只调用一次 relay_receive，处理并回传后再次待命。"}

    def status(self):
        result = self.connection.status()
        with contextlib.closing(self.room.store.connect()) as db:
            for role in ROLES:
                configured = policy(db, role)
                row = db.execute("SELECT value FROM meta WHERE key=?", ("mailbox:waiter:" + role,)).fetchone()
                waiter = json.loads(row[0]) if row else {}
                result["peers"][role]["mcp"] = {
                    "listen_enabled": configured.get("listen", False),
                    "listening": waiter.get("until", 0) > time.time(),
                    "wait_until": waiter.get("until"),
                }
        result["transport"] = "mcp"
        background = Background(self.room)
        for role in ROLES:
            result["peers"][role]["background"] = background.status(role)
        result["mode"] = "background" if any(p["background"].get("running") for p in result["peers"].values()) else "standby"
        return result

    def receive(self, role, cancelled=None):
        return self.listen(role, timeout=0, cancelled=cancelled)

    def listen(self, role, timeout=0, cancelled=None, progress=None):
        other_role(role)
        integer(timeout, "timeout", 0, 30)
        request_id = uuid.uuid4().hex
        key = "mailbox:waiter:" + role
        with self.room.store.transaction() as db:
            self.connection.check_peer(db, role)
            configured = policy(db, role)
            if not configured.get("listen"):
                return {"room": self.room.name, "role": role, "status": "disconnected", "message": None,
                        "next_tool": None, "instructions": "持续监听未开启或已停止；不要自动重新连接。"}
            row = db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
            previous = json.loads(row[0]) if row else {}
            if previous.get("until", 0) > time.time():
                return {"room": self.room.name, "role": role, "status": "already_listening", "message": None,
                        "next_tool": None, "instructions": "该角色已有挂起的监听请求，等待原请求返回，不重复开启。"}
            db.execute("INSERT OR REPLACE INTO meta VALUES(?,?)",
                       (key, json.dumps({"request_id": request_id, "until": time.time() + timeout + 1})))

        def guard(db):
            current = policy(db, role)
            if not current.get("listen") or current.get("generation") != configured.get("generation"):
                return {"room": self.room.name, "role": role, "status": "disconnected", "message": None}
            if not db.execute("SELECT 1 FROM channel_peers WHERE role=?", (role,)).fetchone():
                return {"room": self.room.name, "role": role, "status": "disconnected", "message": None}
            return None

        last_progress = 0
        def on_wait():
            nonlocal last_progress
            if progress and time.monotonic() - last_progress >= 10:
                last_progress = time.monotonic()
                progress()

        try:
            result = self.connection.receive(role, timeout=timeout, cancelled=cancelled, guard=guard, on_wait=on_wait)
        finally:
            with self.room.store.transaction() as db:
                row = db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
                if row and json.loads(row[0]).get("request_id") == request_id:
                    db.execute("DELETE FROM meta WHERE key=?", (key,))
        result["next_tool"] = None
        if result["status"] == "empty":
            result.update(next_tool=None,
                          instructions=RULES + "没有新消息。结束本轮待命，由后台适配器在消息到达时唤醒。"
                          "不要再次调用 listen、receive、status，不要创建定时任务或轮询循环。")
        elif result["status"] == "message":
            message_id = result["message"]["id"]
            result["after_handling_tool"] = None
            if role == "receiver":
                result["reply_tool"] = {"name": "relay_reply", "arguments": {
                    "room": self.room.name, "role": role, "message_id": message_id,
                    "content": "填写实际执行结果、验证证据或遇到的问题"}}
                result["instructions"] = (RULES + "已领取 sender 的指令。你是执行 receiver，"
                                          "只在用户授权范围内执行；不反向指挥 sender。完成或受阻后用 reply_tool 回传真实情况，"
                                          "随后结束本轮，由后台监听下一条指令。不要重复执行已经领取的工作。")
            else:
                result["ack_tool"] = {"name": "relay_ack", "arguments": {
                    "room": self.room.name, "role": role, "message_id": message_id}}
                result["followup_tool"] = {"name": "relay_send", "arguments": {
                    "room": self.room.name, "role": role, "reply_to": message_id,
                    "content": "根据验收结果填写下一条明确指令"}}
                result["instructions"] = (RULES + "已领取 receiver 的执行结果。你是高阶 sender，"
                                          "审阅实际结果和证据，并依据用户已授权目标决定下一步。"
                                          "认可本轮结果时执行 ack_tool；需继续执行或修正时用 followup_tool 下发具体指令，"
                                          "该调用会同时确认本次结果。没有具体工作时不发送礼貌性回信。"
                                          "处理后结束本轮，由后台监听后续结果，不调用空闲轮询。")
        return result

    def send(self, role, content, key=None, reply_to=None):
        if role != "sender":
            raise RelayError("只有 sender 可以下发指令；receiver 必须用 relay_reply 回传原指令的结果", 403)
        result = self.connection.send(role, content, key, reply_to)
        return {**result, "kind": "command", "next_tool": self.followup(role),
                "instructions": "指令已持久化，正在监听的 receiver 将取得指令；尚不能据此声称已经执行。"
                "现在结束本轮，后台事件到达后再领取结果；不要轮询等待。"}

    def reply(self, role, message_id, content):
        if role != "receiver":
            raise RelayError("只有 receiver 可以回传执行结果；sender 应使用 relay_send 下发后续指令", 403)
        result = self.connection.send(role, content, reply_to=message_id)
        return {**result, "kind": "result", "next_tool": self.followup(role),
                "instructions": "执行结果已持久化并确认原指令，等待 sender 审阅。结束本轮，由后台监听后续指令。"}

    def ack(self, role, message_id):
        return {**self.connection.ack(role, message_id), "next_tool": self.followup(role),
                "instructions": "消息已确认。结束本轮，由后台等待新消息；不要轮询或为确认消息再发送确认。"}

    def disconnect(self, role):
        if __package__:
            from .auto_receive import read_state, write_state
        else:
            from auto_receive import read_state, write_state
        with contextlib.closing(self.room.store.connect()) as db:
            self.connection.check_peer(db, role, required=False)
        Background(self.room).stop(role)
        with self.room.store.transaction() as db:
            self.connection.check_peer(db, role, required=False)
            set_policy(db, role, {"listen": False, "generation": uuid.uuid4().hex})
            db.execute("DELETE FROM channel_peers WHERE role=?", (role,))
            write_state(db, role, {**read_state(db, role), "enabled": False, "status": "off"})
        return {"room": self.room.name, "role": role, "status": "disconnected", "message": None,
                "next_tool": None, "instructions": "已停止该角色收件并取消登记，保留消息。不要自动重新连接。"}
