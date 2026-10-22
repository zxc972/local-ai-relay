"""Prepare native channels; retire model-driven scheduled polling."""
from __future__ import annotations

import json
import os
import shlex
import shutil
import sys
import time
import uuid
from pathlib import Path

if __package__:
    from .core import RelayError, private_write
else:
    from core import RelayError, private_write


def read_state(db, role):
    row = db.execute("SELECT value FROM meta WHERE key=?", ("listener:" + role,)).fetchone()
    return json.loads(row[0]) if row else {"enabled": False, "status": "off"}


def write_state(db, role, state):
    db.execute("INSERT OR REPLACE INTO meta VALUES(?,?)",
               ("listener:" + role, json.dumps(state, ensure_ascii=False)))


def check_no_background(db, role):
    if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='background_listeners'").fetchone():
        state = db.execute("SELECT enabled,instance,lease_until FROM background_listeners WHERE role=?", (role,)).fetchone()
        if state and (state["enabled"] or (state["instance"] and (state["lease_until"] or 0) > time.time())):
            raise RelayError("该角色已有通用后台监听；请先 wake stop，等待进程退出后再启用原生通道", 409)


def describe(db, role):
    state = read_state(db, role)
    public = {k: v for k, v in state.items() if k not in ("instance", "pid")}
    if state.get("client") == "claude" and state.get("status") == "listening":
        if time.time() - state.get("last_checked_at", 0) > 10:
            public["status"] = "not_running"
    return public


def save_config(path, content):
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    private_write(temporary, content)
    os.replace(temporary, path)


def prepare(room, role, client, scheduled=None):
    if __package__:
        from .connection import Connection, other_role
    else:
        from connection import Connection, other_role
    other_role(role)
    if client not in ("codex", "claude"):
        raise RelayError("client 必须为 codex 或 claude")
    connection = Connection(room)
    if client == "codex":
        if scheduled:
            raise RelayError("定时唤醒模型的 heartbeat 接入已停用。请使用 connect --adapter codex --thread-id 现有会话ID")
        with room.store.transaction() as db:
            connection.check_peer(db, role, required=False)
            state = {"enabled": False, "client": "codex", "status": "requires_event_adapter"}
            write_state(db, role, state)
        return {"room": room.name, "role": role, "auto_receive": state,
                "instructions": "旧 heartbeat 不再启用。使用 airelay connect 房间 --as 角色 --adapter codex "
                "--thread-id 现有会话ID 配置事件唤醒；需宿主的 Codex queue 入口可用。"
                "不得用 AI 轮询、定时任务或启动另一个模型会话替代。"}
    if scheduled:
        raise RelayError("heartbeat 已停用；使用后台事件接入")
    with room.store.transaction() as db:
        connection.check_peer(db, role, required=False)
        check_no_background(db, role)
        previous = read_state(db, role)
        state = previous if previous.get("enabled") and previous.get("client") == client else {
            "enabled": True, "client": client, "status": "requires_channel_restart",
            "configured_at": time.time(),
        }
        write_state(db, role, state)
    name = "airelay-" + room.name + "-" + role
    path = room.store.root / ("channel-" + role + ".mcp.json")
    config = {"mcpServers": {name: {
        "command": sys.executable,
        "args": [str(Path(__file__).with_name("channel_server.py")), "--data-dir", str(room.base),
                 "--room", room.name, "--as", role],
        "env": {"CODEX_THREAD_ID": "", "AIRELAY_SESSION_ID": ""},
    }}}
    save_config(path, json.dumps(config, ensure_ascii=False, indent=2) + "\n")
    return {"room": room.name, "role": role, "auto_receive": state, "mcp_config": str(path),
            "launch_command": room.command("claude", room.name, "--as", role),
            "instructions": "已准备 Claude 原生推送通道；没有启动新聊天，也没有发送任务。"
            "需要在 Claude 所在项目的外部终端运行 launch_command，以 --continue 恢复最近的聊天并启用通道。"
            "若当前正在 Claude 内，不要从工具里嵌套启动 Claude；向用户呈现该启动命令。"
            "首次启动由 Claude 显示本地开发通道确认；只有真实 Claude 调用 relay_ready 后才显示 listening。"}


def disable(room, role):
    if __package__:
        from .connection import Connection, other_role
    else:
        from connection import Connection, other_role
    other_role(role)
    connection = Connection(room)
    with room.store.transaction() as db:
        connection.check_peer(db, role, required=False)
        write_state(db, role, {**read_state(db, role), "enabled": False, "status": "off"})
    return {"room": room.name, "role": role, "status": "disabled",
            "instructions": "已关闭该角色自动收件。若关联 Codex heartbeat，请同时在应用中暂停它，避免空转。"}


def poll(room, role):
    # Compatibility for already-scheduled callers: never claim or execute work.
    return {"room": room.name, "role": role, "status": "disabled", "message": None,
            "instructions": "旧 heartbeat 收件已停用。请停止调度，改用后台事件适配器。"}


def launch_claude(room, role, resume=None):
    if os.environ.get("CLAUDECODE") or not sys.stdin.isatty() or not sys.stdout.isatty():
        raise RelayError("请在 Claude 所在项目的外部交互终端运行此命令；不能从 AI 的 Bash 工具中嵌套启动 Claude")
    binary = shutil.which("claude")
    if not binary:
        raise RelayError("未找到 Claude Code，请先安装客户端")
    plan = prepare(room, role, "claude")
    name = "airelay-" + room.name + "-" + role
    args = [binary] + (["--resume", resume] if resume else ["--continue"])
    args += ["--mcp-config", plan["mcp_config"], "--dangerously-load-development-channels", "server:" + name]
    args += ["--", f"仅建立本地中转站连接：你是房间 {room.name} 的 {role}。"
             "请调用该通道的 relay_ready 工具确认已经接入，然后简短确认并待命。"
             "不要发送任务、测试消息或从上下文推导工作。之后通道提醒到达时按其 instructions 自动收件。"]
    print("恢复 Claude 聊天并启用本地推送通道：\n" + shlex.join(args), flush=True)
    environment = {k: v for k, v in os.environ.items() if k not in ("CODEX_THREAD_ID", "AIRELAY_SESSION_ID")}
    os.execvpe(binary, args, environment)
