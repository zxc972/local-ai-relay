# 受控执行器验证记录

日期：2026-10-27，Asia/Shanghai。本次增加了独立接单执行器，没有更改日常 Codex / Claude 的宿主权限。

## 已验证的结果

使用真实 Colima Linux VM、Docker 容器、临时 HTTP 聊天室、两个独立认证的脚本请求者及普通 Python 后台执行器。没有模拟容器结果，没有调用模型，没有使用真实个人文件。

**17 项检查全部通过，耗时 28.11 秒。** 共自动处理 16 条任务，另外检查了一次跨身份历史和消息 ID 访问。逐次手动批准数为 0，任务模型调用数为 0；每个任务结束后均检查没有残留任务容器。

| 验证 | 实际结果 |
| --- | --- |
| 正常文本处理 | 自动收件、读取本任务输入、运行、私聊回传，成功 |
| 读取、改写、删除本机临时哨兵文件 | 全部失败；文件哈希未变化，结果未含哨兵内容 |
| 软链接跳转到本机文件 | 无法读取；Docker socket 和宿主凭据不可访问 |
| 继承宿主环境秘密 | 容器命令未收到测试环境变量 |
| 访问公网、VM 宿主地址和本地服务 | 连接失败 |
| 实际内核限制 | UID 65534、NoNewPrivs=1、Seccomp=2、CapEff=0；根目录写入失败 |
| 请求者 A 与 B 的文件 | B 看不到 A 留在 `/work` 和 `/tmp` 的文件，B 只能读取自己提交的数据 |
| 请求者之间的私聊历史、消息 ID | B 的历史不含 A 的秘密；按 A 的消息 ID 查询返回 404 |
| 输入路径跳转和权限覆盖字段 | 进入容器之前拒绝，未执行 |
| 磁盘写满 | 被工作区容量限制阻止 |
| 输出洪泛 | 返回 `output_limit`，容器终止并清理 |
| 无限等待 | 返回 `timeout`，容器终止并清理 |
| 分配超量内存 | Docker 确认 OOM，返回 `memory_limit`，清理完成 |
| 创建过量子进程 | 触发进程数限制，未突破容器边界 |
| 攻击后的正常任务 | 请求者 B 的新任务仍可完成 |

注意：部分攻击探针返回 `completed`，表示探针程序成功验证“访问被拒绝”，不表示危险操作执行成功。

测试镜像：`python:3.12-slim`，固定 ID `sha256:12316debbfb86890f6b2d825820612741ba527ad5a44e3902759141d572bfe8f`。

攻击测试使用 1 CPU、128 MiB 内存、4 MiB 工作区、32 个进程、8192 字节输出限制。超时探针使用 2 秒，其余使用 10 秒。实际保存的默认接单配置为 1 CPU、256 MiB 内存、32 MiB 工作区、30 秒、每小时 120 个任务；网络关闭，不挂载宿主目录。

VM 配置禁用用户目录共享、SSH agent 转发、默认 Docker context 切换。实际检查：本机 Documents 路径不可见；Colima 自身的镜像缓存有只读辅助挂载，任务容器没有该挂载。

## 实际 work 房间接入

- 已保存 `.relay/executor.json`，仅本机用户可写，镜像固定。
- 已备份数据库至 `.relay/backups/executor-20261027-155035/`，更新并重启本地 8787 服务。
- 独立后台进程以 `executor` 加入 `work`，服务端返回 `executor_waiting`、`listening=true`。
- 用临时身份 `executor-check` 经真实 HTTP 私聊发送正常文本任务，后台独立进程自动回复 `LIVE SANDBOX CHECK`，退出后确认容器已清理。临时身份随后离开，原成员继续保留。
- 另按网页实际保留的 `@executor` 前缀格式发送任务，自动回复 `你好，受控执行器`；已保存 `web-smoke.json`。验证的是网页消息格式经真实 HTTP 的执行路径，没有声称做过浏览器点击测试。
- 本主聊天没有重新以 Codex 成员加入房间，也没有唤醒现有 Codex / Claude 成员。
- 后台 PID 文件：`.relay/executor.pid`。停止该 PID 对应的执行器进程（SIGTERM）会清理当前任务并离开；网页移除成员会停止接收后续任务。

## 回归及复现

- 完整 Python 回归：216 项全部通过，无跳过（临时环境补齐 setuptools / wheel）。
- 随后新增深层 JSON 拒绝与网页 `@executor` 前缀测试，执行器专项 14 项全部通过；涵盖私有传输、身份复用、长输入、额度、单进程锁、崩溃和回传中断恢复、CLI/MCP/网页格式提交。
- JavaScript：22 项全部通过。
- 原始真实容器结果：`.relay/verification/executor-20261027/summary.json`。
- 真实 work 冒烟结果：`.relay/verification/executor-20261027/live-smoke.json`。
- 发行包已更新，打包规则不包含本机配置、数据库、测试凭据或日志。

```console
python3 scripts/verify_executor.py --docker-context colima-airelay-executor --image python:3.12-slim --output /absolute/verification
python3 -m unittest discover -s tests -p test_chat_executor.py -v
```

## 验证边界

这些结果证明所测命令路径和隔离行为，不是对所有 Docker/内核逃逸的证明。严格执行的范围仅是这个容器执行器；尚未把任意原生 Codex / Claude 会话及其所有工具封装进沙箱。首版支持结构化命令和文本输入输出，不接入模型账号、不运行自然语言自主接单流程。

接单者和聊天室服务器管理员仍可能读取输入、历史或结果。本版不提供对机器所有者的保密、端到端加密、历史自动销毁或取证级擦除。容器清理与聊天记录保留是不同的事情。
