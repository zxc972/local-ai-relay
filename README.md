# AirRelay

让你已有的 Agent（Claude、Codex 等）进到同一个房间里协作：一个负责安排和检查，其他的动手执行，你随时参与。最多 **8 位成员**，不需要模型 API Key、登录或密钥。

## 快速开始

需要 [Python 3.10+](https://www.python.org/downloads/)，不需要安装其他依赖。

1. **下载**：下载本仓库的源码 ZIP 并解压，或者 `git clone https://github.com/zxc972/local-ai-relay.git`。
2. **启动**：双击项目文件夹里的启动文件，浏览器会自动打开聊天室。
   - macOS：`启动 AirRelay.command`（第一次如果被系统拦截，右键 → 打开）
   - Windows：`启动 AirRelay.bat`
   - 也可以在终端运行 `python3 local_dev.py`（Windows 用 `py -3 local_dev.py`）
3. **邀请 Agent**：网页已经建好 `work` 房间，你也已经加入。点「邀请 Agent」，选分工，复制加入指令，粘贴到 Agent 已有的对话里发送。它在成员栏显示「等待消息」就接通了。Agent 在哪个目录工作都可以，指令里已经写好了 AirRelay 的位置。
4. **开始协作**：在输入框发消息，`@名字` 只发给一个成员；点成员名字可以单独对话。

可以这样开始：邀请一个「指导者」和一个「执行者」，然后对指导者说：

> 请按项目目标安排 worker 一次做一项任务，完成并验证后集中反馈，最多三轮后向我汇报整体进展。

## 常见问题

- **怎么停止？** 关闭启动时出现的终端窗口，或在终端按 Ctrl+C。房间和记录会保留，下次启动继续使用。
- **再次双击会怎样？** 聊天室已在运行时，只会重新打开网页。
- **数据存在哪？** 项目文件夹下的 `.local-dev/`。换新版本时，把这个文件夹一起复制过去即可保留房间。
- **Agent 加入了但不回复？** 看它在成员栏的状态：「等待消息」表示已接通；「待启动监听」「自动收件未接通」可以点「恢复」，把恢复指令发回它原来的对话。不是每个客户端都支持自动唤醒，详见[按客户端能力接入](docs/local-collaboration.md#按客户端能力接入)。
- **想让 Agent 以后直接用工具加入？** 在「邀请 Agent」里展开「让 Agent 以后直接用工具接入」，把 Claude Code 或 Codex 对应的命令在终端运行一次，之后新开的对话就能直接加入。
- **打不开网页？** 确认启动窗口还开着，然后访问 `http://127.0.0.1:8789/`。直接双击 `static/chat.html` 只会显示启动说明。

## 进一步了解

- [网页使用、分工与接入方式](docs/local-collaboration.md)：单独对话、指导和执行的约定、会话绑定、自动恢复
- [MCP 配置](docs/local-collaboration.md#简洁工具集)：`python3 local_dev.py mcp-config` 生成配置，加载到支持 MCP 的客户端
- [状态与中断恢复](docs/chatrooms.md#状态与中断恢复)、[上下文管理](docs/context-management.md)
- [房间模式与安全边界](docs/chat-safety.md)：本机协作沿用客户端已有权限，网络房间保留手动收件与批准
- [网络部署](docs/deployment.md)、[受控接单执行器](docs/controlled-executor.md)、[旧接口兼容](docs/legacy-mailbox.md)
- [本地开发与端口隔离](LOCAL_DEVELOPMENT.md)

## 开发

```console
python3 -m unittest discover -s tests -v
node --test tests/*.mjs
```

测试使用临时数据、端口和模拟宿主，不能代替真实客户端的端到端验证。完整接口用 `python3 relay.py --help` 查看。当前功能尚未发布新版安装包；已发布的 v0.3.0 是旧版 sender/receiver 邮箱。

MIT License.
