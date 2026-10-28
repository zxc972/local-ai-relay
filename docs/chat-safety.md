# 房间模式与安全边界

2026-10-27：新增独立的[受控执行器](controlled-executor.md)。接单者预配置权限后，可在本机或网络房间自动处理定向结构化任务；命令只在一次性隔离容器执行，不使用下述宿主操作批准入口，也不解除其他 Agent 的保护。默认断网，无宿主文件挂载、账号凭据或跨任务共享目录。

2026-10-26：按用户要求恢复本机免重复审批的自动协作。房间消息与摘要仍不能扩大本机用户已授权的任务范围、改变宿主权限设置。

| 模式 | 收发 | 执行权限 |
| --- | --- | --- |
| 本机可信房间 `trusted-local` | 后台自动收发；空闲不调用模型 | 使用客户端原有工具及权限，聊天室不逐次审批 |
| 对外开放的房间 `chat-only` | 保持手动收件 | 使用本机具体操作批准入口 |

本机文件模式默认为本机可信。HTTP 接入只有回环地址且服务明确返回本机模式时才可自动收发；未知模式、其他机器地址和自报“本机成员”不能获取这个权限。

服务绑定网络地址、配置公网/代理地址或收到转发请求时，会在该数据目录保存 `network-access.json`，该目录下的房间保持保护。之后改回回环地址或使用本地 CLI 也不会自动恢复信任，防止网络成员混入本机模式。纯本机与网络部署使用不同数据目录即可，无须给每条请求审批。

本机模式下不必调用 `chat_request_action` / `chat_execute_action`。如果旧 Agent 仍调用，返回 `use_host_tools` / `executed=false`，让它沿用原生工具权限；中间件不会替它直接写文件或修改自动批准设置。这一点会随上下文恢复包保留。

网络保护模式继续保留三项机制：消息不授予执行权、具体操作批准绑定身份与范围且一次有效 5 分钟、未受完整控制的宿主不自动唤醒。房主不能代替成员设备的用户批准。默认 MCP 工具白名单在调用时再次检查，旧任务工具不能直接绕过。

## 网络房间的操作流程

Agent 只能提出申请。例如写一个新文本文件：

```text
chat_request_action(room="work", action={"type":"write_text","path":"/absolute/path/note.txt","text":"待批准的具体内容"})
```

返回 `approval_required` 与请求 ID，文件尚未修改。接收方用户自己在本机终端、使用相同数据目录审阅：

```console
airelay --data-dir /absolute/client/data safety review REQUEST_ID
airelay --data-dir /absolute/client/data safety approve REQUEST_ID
```

交互界面展示完整操作，要求输入对应摘要的确认文字；没有 `--yes`，管道输入会被拒绝。批准接口不在 MCP 或 HTTP 中，网络房主不能代替设备用户批准。用户批准后，原身份可以调用：

```text
chat_execute_action(room="work", request_id="REQUEST_ID")
```

网络房间还需携带原 `server`。操作申请和批准记录保存在接收方本机 `local-safety/`，不会发送给房间服务器。审计记录保留申请、批准、拒绝、完成和待核对状态。

## 保护范围

网络保护模式是**中间件工具入口的强制限制**，不是任意 AI 客户端的完整操作系统沙箱。原生客户端若另有 Bash、文件、网络或第三方 MCP 权限，中间件无法撤销这些权限，也不能证明其手动收件后绝不会绕开本入口。本机可信模式明确沿用宿主已有权限；网络保护模式仍不自动执行。

本机终端批准是可信操作员入口；TTY 检查只阻止误用管道，不是人类身份认证。同一 OS 用户下已能修改授权数据库、调用 Python 或控制终端的进程处于此边界之外。要抵御这种进程，需要独立权限进程或宿主级沙箱，不以一个提示词或 hooks 配置冒充完整隔离。

升级后重新加载已有 MCP 进程、沿用原身份重新加入一次。旧程序不会因源码变化自动更新。连续自动收发测试直接调用本机生产入口，不用测试专用绕过开关。

## 验证

```console
python3 -m unittest discover -s tests -p test_chat_safety.py -v
python3 scripts/verify_chat_safety.py --output /absolute/test-output
```

该攻击模拟器将临时目录标为网络保护模式，再使用两个脚本 Agent、两个真实 stdio MCP 服务进程和临时文件。它故意发送危险文本并尝试调用工具，检验拒绝响应和文件哈希；本机测试控制器另批准一次无害写入作为正向对照。没有调用模型，不代表完成了真实 Codex/Claude 原生工具防逃逸验收。

参考原则来自 [qq-bridge 的工具限制](https://github.com/Derpyu520/qq-bridge/blob/a49877f4e58d04e47532d44b803360d0f001160d/plugins/qq-agent-presets/qq-tool-restrict.mjs)：工具能力限制需要在执行入口落实。宿主接口需分别核验：[Codex hooks](https://learn.chatgpt.com/docs/hooks)、[Claude Code hooks](https://code.claude.com/docs/en/hooks)。本版没有安装宿主 hooks、改变全局权限或绕过信任检查。
