"""The same MCP tool contract for either side, regardless of client or model."""
from __future__ import annotations

import threading

if __package__:
    from .core import RelayError
    from .message_bus import INSTRUCTIONS, Mailbox
else:
    from core import RelayError
    from message_bus import INSTRUCTIONS, Mailbox


def schema(properties, required):
    return {"type": "object", "properties": properties, "required": required, "additionalProperties": False}


TEXT = {"type": "string", "minLength": 1, "maxLength": 100000}
ROOM = {"type": "string", "pattern": "^[A-Za-z0-9][A-Za-z0-9_-]{0,47}$", "maxLength": 48}
ROLE = {"type": "string", "enum": ["sender", "receiver"]}
ENDPOINT = {"room": ROOM, "role": ROLE}
TOOLS = [
    {"name": "relay_connect", "description": "只连接、不发送任务。sender 指挥，receiver 执行。启用已配置的本地后台唤醒适配器，然后结束本轮待命。needs_adapter 时报告缺少宿主事件接入，禁止用模型轮询代替。",
     "inputSchema": schema({**ENDPOINT, "listen": {"type": "boolean", "default": True}}, ["room", "role"])},
    {"name": "relay_send", "description": "仅限 sender 向 receiver 下发指令。key 用于幂等重试；reply_to 可关联刚收到的 result，确认该结果并下发后续指令。receiver 不能调用此工具反向指挥 sender。",
     "inputSchema": schema({**ENDPOINT, "role": {"type": "string", "enum": ["sender"]}, "content": TEXT,
                            "reply_to": TEXT, "key": {"type": "string", "minLength": 1, "maxLength": 200}},
                           ["room", "role", "content"])},
    {"name": "relay_receive", "description": "后台事件到达后只调用一次，原子领取一条消息。receiver 收 command，sender 收 result。无消息立即返回待命；禁止循环调用。",
     "inputSchema": schema(ENDPOINT, ["room", "role"])},
    {"name": "relay_listen", "description": "兼容单次收件接口。无消息后结束本轮，不自动续接。持续等待必须由本地后台进程承担，禁止 AI 重复调用此工具。优先使用事件触发的 relay_receive。",
     "inputSchema": schema({**ENDPOINT, "timeout": {"type": "integer", "minimum": 0, "maximum": 30, "default": 0}},
                           ["room", "role"])},
    {"name": "relay_reply", "description": "仅限 receiver 对已领取的 sender 指令回传实际结果、证据或问题，同时确认原指令。message_id 必须指向原指令；相同回复自动去重。sender 下发后续指令用 relay_send。",
     "inputSchema": schema({**ENDPOINT, "role": {"type": "string", "enum": ["receiver"]}, "message_id": TEXT, "content": TEXT},
                           ["room", "role", "message_id", "content"])},
    {"name": "relay_ack", "description": "sender 审阅并认可 receiver 回传的结果后确认该结果，不再发送无目的的确认消息。receiver 应通过 relay_reply 回传结果来确认原指令。",
     "inputSchema": schema({**ENDPOINT, "role": {"type": "string", "enum": ["sender"]}, "message_id": TEXT},
                           ["room", "role", "message_id"])},
    {"name": "relay_status", "description": "查看房间登记情况和当前实际挂起的 MCP 监听。已连接和已在监听分别报告，不读取消息正文。",
     "inputSchema": schema({"room": ROOM}, ["room"])},
    {"name": "relay_disconnect", "description": "用户要求停止时断开此角色并终止其挂起监听，保留历史与未确认消息。不要自动重连。",
     "inputSchema": schema(ENDPOINT, ["room", "role"])},
]
BY_NAME = {tool["name"]: tool for tool in TOOLS}
for tool in TOOLS:
    tool["annotations"] = {"readOnlyHint": tool["name"] == "relay_status", "openWorldHint": False}


def validate(name, arguments):
    if name not in BY_NAME or not isinstance(arguments, dict):
        raise RelayError("未知工具或无效参数")
    spec = BY_NAME[name]["inputSchema"]
    if set(arguments) - set(spec["properties"]) or any(key not in arguments for key in spec["required"]):
        raise RelayError("参数包含未定义字段或缺少必填字段")
    for key, value in arguments.items():
        field = spec["properties"][key]
        if field["type"] == "string":
            if not isinstance(value, str) or not value.strip() or len(value) > field.get("maxLength", 100000):
                raise RelayError(f"{key} 必须是有效文本")
            if "enum" in field and value not in field["enum"]:
                raise RelayError(f"{name} 的 {key} 必须为 " + " 或 ".join(field["enum"]))
        elif field["type"] == "integer":
            if type(value) is not int or not field["minimum"] <= value <= field["maximum"]:
                raise RelayError(f"{key} 不是允许范围内的整数")
        elif field["type"] == "boolean" and type(value) is not bool:
            raise RelayError(f"{key} 必须是布尔值")


def invoke(data_dir, name, arguments, cancelled=None, progress=None):
    validate(name, arguments)
    arguments = dict(arguments)
    mailbox = Mailbox(data_dir, arguments.pop("room"))
    if name == "relay_connect":
        return mailbox.connect(**arguments)
    if name == "relay_send":
        return mailbox.send(**arguments)
    if name == "relay_listen":
        return mailbox.listen(**arguments, cancelled=cancelled, progress=progress)
    if name == "relay_receive":
        return mailbox.receive(**arguments, cancelled=cancelled)
    if name == "relay_reply":
        return mailbox.reply(**arguments)
    if name == "relay_ack":
        return mailbox.ack(**arguments)
    if name == "relay_status":
        return mailbox.status()
    return mailbox.disconnect(**arguments)


class Endpoint:
    """Keep the role chosen for each room on this Agent's MCP connection."""
    def __init__(self, data_dir):
        self.data_dir = data_dir
        self.roles = {}
        self.lock = threading.Lock()

    def invoke(self, name, arguments, cancelled=None, progress=None):
        validate(name, arguments)
        room, role = arguments["room"], arguments.get("role")
        with self.lock:
            expected = self.roles.get(room)
            if name == "relay_connect":
                if expected and role != expected:
                    raise RelayError(f"此 MCP 连接在房间 {room} 已登记为 {expected}，不能改为 {role}", 403)
                result = invoke(self.data_dir, name, arguments, cancelled, progress)
                self.roles[room] = role
                return result
            if role is not None:
                if expected is None:
                    raise RelayError("请先通过 relay_connect 登记此 Agent 的房间与角色", 409)
                if role != expected:
                    raise RelayError(f"此 MCP 连接是 {expected}，不能以 {role} 身份调用工具", 403)
        # Do not hold the role lock during a hanging listen: sends and disconnects
        # must remain available on the same MCP connection.
        return invoke(self.data_dir, name, arguments, cancelled, progress)
