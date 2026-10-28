# 受控执行器：预先配置、自动接单

执行器以独立成员加入现有聊天室，接收定向任务，在一次性 Docker 容器运行，再私聊回复原提交者。等待由普通 Python 进程完成，不调用模型，也不需要逐条批准。原有 Codex / Claude 桌面聊天权限不变。

首版有意只接收**命令参数和文本文件**，适合代码验证、文本处理和生成小型文本结果。它不把陌生人的自然语言交给接单者的日常全权限 Agent；也不复制 Codex / Claude 登录状态。目前结果通过 stdout / stderr 返回，文件产物可由任务读取并打印；不提供任意目录上传、压缩包解包或二进制产物下载。

## 默认权限

| 项目 | 权限 |
| --- | --- |
| 文件 | 仅本任务提交的文本输入与全新的 `/work`；不挂载本机目录、其他任务目录或 Docker socket |
| 网络 | 关闭；不能访问互联网、局域网或宿主服务 |
| 身份 | 容器 UID 65534，删除 Linux capabilities，禁止提升权限，要求 seccomp |
| 磁盘 | 根文件系统只读；工作区默认 32 MiB、临时区 8 MiB，均为任务专属 tmpfs |
| 资源 | 1 CPU、256 MiB 内存、禁用额外 swap、32 个进程、默认 30 秒 |
| 数据量 | 每任务最多 32 个文本文件，任务包 48000 UTF-8 字节；输出合计最多 8192 字节 |
| 频率 | 每执行器每小时最多 120 个任务，串行运行；达到上限暂不领取后续工作 |
| 权限配置 | 只读本地配置；消息不能修改镜像、挂载、网络、环境变量、超时或回传对象 |
| 失败行为 | Docker 不可用直接停止；绝不降级到宿主 Shell |

镜像初始化时固定为不可变的本机 `sha256:` ID，任务不能指定镜像；操作者须使用无秘密的可信 Python 3 镜像。首次准备镜像需要网络，任务执行时断网。

## 接单者配置一次

macOS 上可用独立 Colima VM，禁用宿主目录共享与 SSH 凭据转发，且不切换其他项目的 Docker context：

```console
colima start airelay-executor --cpu 2 --memory 2 --disk 8 --mount none --mount-inotify=false --ssh-agent=false --ssh-config=false --activate=false --port-forwarder none --template=false
docker --context colima-airelay-executor pull python:3.12-slim
airelay --data-dir /absolute/relay-data executor init --config /absolute/executor.json --room work --name executor --docker-context colima-airelay-executor
airelay --data-dir /absolute/relay-data executor doctor --config /absolute/executor.json
airelay --data-dir /absolute/relay-data executor run --config /absolute/executor.json
```

已有 Docker 的机器可使用自己的 context。`init` 不覆盖已有文件；本机用户可以在停止执行器后编辑 JSON 中的时间、内存、工作区容量及每小时任务数，重启生效。网络关闭、无宿主挂载等核心边界不能配置为关闭。配置只允许本机拥有者写入。

执行器使用独立昵称，占用聊天室的一个名额，不要复用日常 Codex / Claude 的昵称。保留上述普通后台进程即可；Ctrl+C 或 SIGTERM 会结束当前任务、清理容器并退出房间。不要用模型反复调用收件。可用系统进程管理器托管该命令，首版不安装系统启动项。

网络聊天室在 `init` 增加 `--server https://chat.example.com`；首次 `run` 增加 `--invite=邀请值`。凭据保存在接单者本机，只由运输层使用，不传入容器。使用同一个 `--data-dir` 重启会恢复身份及处理记录。互联网部署使用 HTTPS；任务输入、输出仍保存在现有聊天室服务器，所以服务器运营方也属于可信范围。

网络房间原有 `chat-only` 不会因此解除：仅这个专用执行器走容器路径，其他桌面 Agent 的宿主工具保护保持原状。

网页展示执行器等待、运行、离线状态；这是成员进程上报的在线信息，不是对远程机器隔离能力的证明，也不作为权限判断依据。

## 请求者提交

先按原流程加入聊天室。MCP 新增：

```text
chat_submit_job(room="work", to="executor", key="unique-job-001",
                argv=["python3", "-c", "from pathlib import Path; print(Path('input.txt').read_text().upper())"],
                files={"input.txt":"hello"})
```

网络接入带上原 `server`。同一连接的 `key` 是提交幂等键；未确定发送结果时只用相同键重试同一内容，不能换键重新投递。

无 MCP 时，将任务存为 `job.json`：

```json
{"type":"airelay.exec.v1","argv":["python3","-c","print('hello from sandbox')"],"files":{}}
```

```console
airelay --data-dir /absolute/client-data room submit-job work --name alice --to executor --key unique-job-001 --file /absolute/job.json
```

网页也可以选中 `executor` 私聊并发送上述完整 JSON。普通群聊、自然语言和 `airelay.exec.result.v1` 结果消息均不会作为代码执行。收到结果后不要再发回执行器，避免无意义往返。

## 数据隔离与恢复

- 每个任务使用新容器及新 tmpfs；即使同一请求者的连续任务也不共享文件或环境。
- 请求者身份来自聊天室认证，不接受任务 JSON 自报身份。输入必须私聊，结果固定回复原消息；原身份换人时拒绝回传。
- 新输入逐页读全后才处理、确认；其他请求者不能通过任务 ID 或历史接口读取私有消息。
- 执行前持久化任务 ID；结果回传失败仅重发已保存结果，不重复执行。进程意外中断后，下次启动先移除对应容器，报告 `interrupted`，不自动重做。
- 超时、输出超限、内存超限都清理容器。清理失败停止接单。操作系统崩溃或强制 SIGKILL 后的残留容器要由下次启动清理；容器的内存和进程限制仍然有效。
- 执行器记录与聊天室历史都保存在本机权限受限的数据库中。首版没有历史自动销毁或端到端加密；任务容器销毁不等于聊天室记录删除，也不是取证级擦除。

这保护执行边界及不同请求者之间的数据隔离，**不能让数据对接单者的机器管理员不可见**。也不覆盖 Docker/内核逃逸漏洞、已被控制的宿主、恶意镜像或日常 Agent 另行调用原生工具的路径。这里没有声称任意 Codex / Claude 会话已经被沙箱化，更没有把请求者数据保密建立在提示词上。

## 复现测试

```console
python3 -m unittest discover -s tests -p test_chat_executor.py -v
python3 scripts/verify_executor.py --docker-context colima-airelay-executor --image python:3.12-slim --output /absolute/verification
```

第二条使用真实 Docker、临时 HTTP 服务及两个带独立凭据的脚本请求者。它测试本机临时哨兵文件读取/改写/删除、软链接、宿主环境泄漏、网络外发、权限提升字段、跨请求者工作区和历史隔离、输出/时间/内存/磁盘/进程限制，并验证攻击后正常任务仍能执行。运行时不调用模型、不使用真实隐私文件，Docker 不可用会失败而不是跳过。

实现依据：[Docker 运行时隔离及资源选项](https://docs.docker.com/engine/containers/run/)。普通容器不是抵御宿主管理员的保密计算环境。
