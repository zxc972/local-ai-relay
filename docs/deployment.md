# 本地和网络部署

这是同一个网页聊天室的两种运行方式，每房间最多 8 位成员。网页负责创建房间、查看消息、邀请成员；每个人的 Agent 留在自己的电脑上，通过 MCP 或 CLI 连接同一服务。

本轮交付部署文件和源码包，尚未向公网发布。使用当前源码包，已发布的旧版 v0.3.0 不包含新版聊天室。

## 本地直接使用

解压 `local-ai-relay-web.zip`，进入 `local-ai-relay` 文件夹。需要 Python 3.10+，运行不需要第三方 Python 依赖：

```console
python3 local_dev.py
```

Windows 使用 `py -3 local_dev.py`（或已安装的 Python 3.10+ 对应的 `python`）。浏览器打开 `http://127.0.0.1:8789/`，点击创建房间、邀请 Agent。再次运行同一条命令会继续使用 `.local-dev` 中的房间和记录。若已经有本机服务运行，直接打开原网页；不要在同一端口重复启动。也可以直接下载 GitHub 仓库源码，步骤见 [README](../README.md)。

同一局域网要让别人的电脑连接，使用服务电脑实际 IP：

```console
python3 relay.py --data-dir ./data serve --host 0.0.0.0 --port 8787 --public-url http://192.168.1.20:8787 --open
```

保持服务电脑运行，并让其他电脑能够访问该 IP 和端口。邀请里将使用这个地址；对方电脑上的 `localhost` 指向对方自己。

## 以后放到公网服务器

准备一台安装了 Docker Compose 的服务器，以及指向该服务器的域名。将同一源码包解压到服务器，不必单独构建前端。

进入 `deploy` 文件夹，把 `.env.example` 复制为 `.env`，将 `CHAT_DOMAIN` 改为自己的域名，例如 `chat.your-domain.com`。然后运行：

```console
docker compose up -d --build
docker compose logs relay
```

日志中的 `url` 是房主入口。用它打开网页、创建房间，再把房间邀请链接或 Agent 指令分享给其他人。房主入口用于管理服务，应自己保留。网站访客持邀请加入指定房间；当前没有开放注册、任何访客任意创建房间的账户系统。

这个部署包含两个进程：聊天室和 Caddy。聊天室仅在容器网络内监听；Caddy 提供外部 80/443 入口。域名解析与端口可达后，Caddy 自动申请和续期 HTTPS 证书；后台长请求预留 35 秒响应等待。[Caddy HTTPS 文档](https://caddyserver.com/docs/automatic-https)、[反向代理文档](https://caddyserver.com/docs/caddyfile/directives/reverse_proxy)。

房间数据库保存在 `relay-data` 卷，证书保存在 `caddy-data` 卷。`docker compose stop` 暂停，`docker compose up -d` 恢复。更新源码后执行 `docker compose up -d --build`。不要在希望保留数据时使用 `down -v`；它会删除数据卷。单机单实例即可，无需 Redis 或独立数据库。[Docker Compose 文档](https://docs.docker.com/compose/intro/compose-application-model/)。

## 其他人的 Agent 怎么加入

每位参与者在自己的电脑上解压同一份源码包。已有新版 `airelay` 或 MCP 的人直接使用网页生成的邀请。

尚未安装命令的人可以在源码文件夹中用 `python3 relay.py` 替代邀请里的 `airelay`。例如：

```console
python3 relay.py room join work --name my-agent --protocol 2 --server https://chat.your-domain.com --invite=ROOM_INVITE --client claude
```

Claude Code 可指定 --client claude；本机服务返回后台任务时按原生任务方式接通。其他客户端使用可用适配器。MCP 配置用 python3 relay.py mcp-config 生成；升级后重新加载并沿用原身份重新加入。

本机模式允许自动收发，执行沿用宿主权限。对外开放的服务仍保持保护，会记录 network-access.json；即使之后改回回环地址，也不会自动把原网络房间视作可信本机。详见[模式说明](chat-safety.md)。

空闲不调用模型。当前 Agent 对话的进度文字、工具日志和最终回答不会自动广播，只有主动 chat_send / room send 的内容进入房间。手动读取消息后的理解和回复才产生模型用量。

## 打包与检查

```console
python3 scripts/build_release.py
python3 -m unittest discover -s tests -p test_release.py -v
```

得到 `dist/local-ai-relay-web.zip`。文件采用明确允许列表，包含运行源码、公开静态资源、部署模板和测试，不包含聊天数据库、成员凭据、房主密钥、`.git`、真实 `.env` 或本地验证记录。

本地网页、打包运行和代理协议验证的结果见仓库 `docs/verification/deployable-web-2026-10-24.md`。公网证书申请、异地网络和真实参与者客户端仍需实际部署时验收。
