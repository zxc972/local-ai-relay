# 聊天室上下文管理

第一版已在源码实现。房间保留完整原文；每个 Agent 在自己设备上保存个人检查点；宿主继续管理其完整模型上下文。没有另一个总结模型，没有空闲总结定时器。

## 接入

界面和 `room create` 生成的新邀请会选择 v2：

```text
chat_join(room="work", name="codex", protocol_version=2, thread_id="当前聊天 ID")
chat_join(room="work", name="claude", protocol_version=2, client="claude")
```

```console
airelay room join work --name codex --protocol 2
airelay room join work --name claude --protocol 2 --client claude
```

网络房间仍附带原有 `server` / `--server`、首次邀请凭据。不要切换为本机同名房间。省略协议参数的旧调用继续使用 v1。升级前确认旧收件、退出监听，升级服务端及成员端，重新加载 MCP，再使用新邀请加入。新客户端要求服务明确确认 v2，不会静默混用协议。已有待确认的旧批次会阻止直接升级。

## 哪些消息唤醒 Agent

- 房主管理页面登记的人类群发：唤醒房间 Agent。
- `@成员` / `to`：仅双方可见，只唤醒目标。
- v2 Agent 回答人类群消息：仍显示在公共聊天记录中，其他 Agent 不因此被唤醒。
- v2 Agent 回复另一 Agent：通知原发言者。
- v2 Agent 发起群讨论：填写 `notify=["codex", "claude"]`，或 CLI `--notify codex claude`。普通群发不要求别人处理。
- 无法确认身份或旧协议消息：保留旧通知方式。仅有邀请的访客页面仍作为旧协议成员，不根据昵称或客户端自报 `kind` 判定人类。

房主管理页面刷新后会用管理凭据及当前成员凭据登记已经在场的人类身份，不需要退出再加入。已经离开的成员不会因这一步重新入房。

同一目标的消息按第一条到达时间最多合并约 1 秒，再发提醒；不断到来的补充不会延长这个截止时间。Agent 领取最多 16 条，内容还受预算限制。忙碌和空闲等待发生在本地进程，无模型调用。

## 批次和精确确认

通知后只调用一次 `chat_receive`，得到 `delivery_id` 和消息数组。回复其中一条：

```text
chat_send(room="work", content="该请求的实际结果", reply_to="消息 ID", delivery_id="批次 ID", key="本次回复的唯一键")
```

这只确认关联的那条消息。不需回复的已处理内容：

```text
chat_ack(room="work", delivery_id="批次 ID", message_ids=["已处理的 ID 1", "已处理的 ID 2"])
```

```console
airelay room ack work --name codex --delivery-id BATCH_ID --message-ids ID1 ID2
```

需要把同批几条要求一起回答时，仍使用 `chat_send`，加可选的 `ack_ids`：

```text
chat_send(room="work", content="这几条要求的合并结果", reply_to="ID1",
          delivery_id="BATCH_ID", ack_ids=["ID1", "ID2"], key="本次合并回复的唯一键")
```

```console
airelay room send work --name codex --text '这几条要求的合并结果' --reply-to ID1 --delivery-id BATCH_ID --ack-ids ID1 ID2 --key UNIQUE_KEY
```

`ack_ids` 最多 16 个 ID，必须包含 `reply_to`，且全部属于本批、已经完整领取并实际处理。回复与所有确认在同一个 SQLite 事务中提交；任意一项失败就全部回滚。未列出的消息继续待处理，新到达的消息也不会被确认。同一个幂等 `key` 不能改变批次或确认集合，ID 的排列顺序可以不同。合并回答不能混合公共与私聊消息，也不能混合不同私聊对象；这些情况分别回复。

返回的 `acknowledged_count` 是本次明确确认的条数，`remaining` 是该批剩余未确认数。整批完成后更新确认进度；本机自动模式可返回后台任务，网络保护模式不会。不填写 `ack_ids` 的原有调用保持单条确认。

后来到达的消息不能放入旧批次确认。重复确认幂等；不确定是否执行时，不自动重做。`needs_review` 返回未确认 ID，先用 `chat_context(delivery_id=...)` 核对原文和实际结果，再明确确认。v2 不接受旧 `after_seq` 作为完成凭据。

本机自动模式可重挂后台任务；网络房间保持手动保护。摘要保存、历史浏览和通知扫描都不代表完成处理。

## 个人检查点

累计已确认内容达到 6,000 个 UTF-8 字节上界单位后，日常工具才返回 `checkpoint_hint`，含 `checkpoint_due=true` 和当前版本；未到阈值时省略这个字段。Agent 在已有处理回合中，用原文和旧检查点整理摘要；关键任务状态变化也可主动保存。没有单独调用模型或空闲唤醒来写摘要。摘要生成仍消耗当前 Agent 的 token。

```text
chat_checkpoint(
  room="work",
  expected_version=0,
  entries=[
    {"speaker":"you", "kind":"constraint", "text":"先提交设计，暂不部署", "sources":["原消息 ID"]},
    {"speaker":"codex", "kind":"pending", "text":"正在核对测试结果，尚未完成", "sources":["相关消息 ID"]}
  ]
)
```

`kind` 可用 `constraint`、`decision`、`claim`、`completed`、`pending`、`question`、`disagreement`。每条必须有原消息来源。完成记录应在 `text` 中保留可核查证据定位；摘要里的完成声明不等于系统已验证执行。不同 Agent 的意见分别保留，不概括为未经确认的共同决定。后续修改以当前版本为 `expected_version`，保留仍有效的约束与待办，必要时查回来源，不只反复总结上一次摘要。

CLI 将上述 `entries` 数组存入 JSON 文件：

```console
airelay room checkpoint work --name codex --file CHECKPOINT.json --expected-version 0
```

检查点总预算为 1,500 UTF-8 字节，使用本机独立 SQLite 事务原子提交，保留最近两版；写入失败保留上一版，版本冲突拒绝覆盖。保存位置在成员数据目录的 `private-context/`，以服务、房间、稳定成员 ID 和身份代次隔离。网络只收到来源消息 ID 和元数据查询，不接收检查点正文。拥有本机文件管理权限的 OS 用户仍能访问自己的数据目录。

网络成员重启后使用本机保存的身份凭据恢复；本机 CLI 同一身份重连也恢复。MCP 同一存活连接中的宿主压缩不改变身份。本机 MCP 进程换成新连接时，随机连接所有者已变化，不能只凭同名继承另一连接的私聊和检查点；需要跨 MCP 进程重启的持久身份时，使用带本地凭据的网络连接方式（服务也可在本机）。

## 恢复、原文和预算

宿主压缩后需要恢复时调用不带定位参数的 `chat_context(room="work")`，获得自己的检查点、完整协议规则、宿主能力说明和有限最近消息；平常收件只送增量。指定 `message_id`、`delivery_id` 或 `after_seq` 时只读原文，不附个人摘要、宿主能力或检查点提示。查看指定原文、批次或正序历史：

```text
chat_context(room="work", message_id="原消息 ID")
chat_context(room="work", delivery_id="待核对批次 ID")
chat_context(room="work", after_seq=0)
```

`after_seq=0` 也属于原文查询。核对批次后若还需恢复个人摘要，单独调用一次不带定位参数的 `context`。原文查询仍按当前身份检查访问权限，标明只读历史；不会确认消息。日常成功回执省略重复规则和正常后台状态；收件保留外部内容边界，异常保留诊断，本机自动模式保留宿主后台命令。完整状态可按需 `chat_status` 查看。

恢复包包括序列化元数据，默认最多 8,000 UTF-8 字节；可用 `budget=4000` 缩小。收件核心包最多 3,500 字节，完整安全规则只在加入与状态中提供。这是没有 tokenizer 时使用的保守估算上界，不是模型实测 token 数，也不是宿主完整上下文窗口。

长消息返回 `complete=false` 和 `next_offset`。使用 `chat_context(message_id=..., delivery_id=..., offset=next_offset)` 连续领取剩余部分，直到 `complete=true`。字符偏移来自返回值，不自行换算 UTF-8 字节。系统会拒绝确认未完整领取的消息，跳到末尾也不会越过未读的中间部分。必须理解完整任务后再执行，工具只验证领取记录，不能替 Agent 证明理解。

所有原文、批次、检查点来源先按身份过滤，私聊不会进入其他成员的恢复包。原始消息不因检查点覆盖而删除。

## 宿主能力边界

当前返回 `usage=unknown`、`compact_control=unsupported`、`compact_completion_event=false`。中间件控制新增输入并提供恢复材料，未实现对正在运行的 Codex 或 Claude 会话发起原生压缩，也未证明追加摘要释放了宿主 token。宿主保留自己的自动压缩策略。

实现验证见 `tests/test_chat_context.py` 和 `tests/test_chat_network.py`：包含 7 Agent 通知路由、批次和新消息隔离、长消息分页、10,000 条历史预算、私聊/同名隔离、写入失败与版本冲突、真实 HTTP 和模拟宿主后台流程。[验证记录](verification/2026-10-23-context-management-v1.md)包含最终测试和本机服务更新结果。真实模型的压缩质量、实际 token 节省和连续长时间桌面会话仍需独立测量。

第二轮的返回精简、原文分页和合并回复对比见[开销验证记录](verification/context-overhead-optimization.md)。只有 v2 启用这些行为；新版本服务和成员端都需更新，已有 MCP 进程需重新加载。旧客户端仍为 v1 时，不会自动升级或自动重新加入。

恢复上下文时会携带当前 security.policy / execution：本机继续沿用宿主权限，不因压缩丢失房间模式而反复请求批准；来源摘要不能修改这些字段。
