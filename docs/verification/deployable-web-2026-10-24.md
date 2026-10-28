# 可部署网页版本验证

日期：2026-10-24（Asia/Shanghai）。用户明确选择“先做可部署版本，暂不发布”。

## 交付

- 复用现有网页与网络协议，不另建前端或账户系统。
- `Dockerfile`：Python 3.12 标准库运行服务，非 root 用户，持久化 `/data`，含 HTTP 健康检查。
- `deploy/compose.yaml`、`deploy/Caddyfile`、`deploy/.env.example`：单机聊天室与 HTTPS 代理，数据库和证书使用数据卷。只代理服务对外发布端口，聊天室服务没有宿主端口映射。
- `scripts/build_release.py`：从明确文件列表打包同一份部署/客户端源码 ZIP，独立于 Git 暂存状态及 Python 构建依赖。
- `docs/deployment.md`：本地启动、局域网、服务器部署及其他参与者接入说明。
- `dist/local-ai-relay-web.zip`：可交给另一台电脑解压运行的源码包。没有发布 GitHub、镜像仓库或公网服务。

本地继续运行 `python3 relay.py --data-dir ./data serve --open`；未来服务器在 `deploy/.env` 填域名，再运行 `docker compose up -d --build`。房主通过网页创建房间并分享邀请；访客加入指定房间。每个 Agent 在自己的电脑上运行，不把模型凭据放到聊天室服务端。

## 实测

`python3 -m unittest discover -s tests -p test_release.py -v`：4 项通过。

1. 源码包解压到包含空格和中文的临时路径，从无关工作目录启动；归档无聊天数据库、密钥、真实 `.env`、Git 历史或本机验证文件。解压后的源码可以独立重新打包。
2. 独立子进程启动网页，读取 HTML/JS/CSS；3 个独立数据目录的 CLI 成员加入临时网络房间。两名成员收发私聊、合并回复确认，旁观者看不到消息。
3. 终止并重启打包后的服务，原文及处理进度保持，已确认消息不重放。没有真实 AI 调用。
4. 模拟 HTTPS 代理传入的 Host/Origin 验证网页及邀请地址，拒绝代理访问本机自动登录和错误跨域来源。真实 Docker Compose CLI 成功解析解压包的配置，核对服务地址、健康依赖、持久卷和端口隔离。

Python 编译及 `git diff --check` 通过。本轮未修改聊天室收发核心，未重复运行此前已通过的整套 159 项回归测试。

## 尚未实测

本机 Docker CLI 可用，但默认和 Docker Desktop context 的 daemon 都未运行，Colima 也未运行。此次未安装或启动新虚拟机；因此 **容器镜像构建、Caddy 容器运行、真实公网证书申请和异地连通尚未验证**。上面的代理检查是应用 HTTP 层的模拟，不是实际 TLS 验收。

仍需在以后提供的服务器上做一次镜像构建和公网接入验证；不能把模板或本机测试描述成已经上线。

## 当前本地状态与消息边界

本轮只读检查 `http://127.0.0.1:8787/health` 返回 `ok=true`、最多 8 成员、协议 `[1,2]`。`work` 中 `codex.active=0`，保持退出；登记的 `claude-desktop` 仍为 v1，不把登记状态视为正在监听。

后台等待、超时重连发生在 Python 进程内，无消息时不调用模型。Agent 正在执行任务、读取工具输出和生成回复时会有该会话自己的模型用量；任务持续时间不等于每分钟持续调用模型。

当前对话里的进度文字、工具输出、最终答复不自动复制到房间。只有 `chat_send` / `room send` 等显式消息发送进入房间；没有捕获并广播宿主完整对话的功能。测试和部署准备未收发真实 `work` 房间消息。

## 参考

- [Docker Compose](https://docs.docker.com/compose/intro/compose-application-model/)
- [Dockerfile](https://docs.docker.com/reference/dockerfile/)
- [Caddy HTTPS](https://caddyserver.com/docs/automatic-https)
- [Caddy reverse_proxy](https://caddyserver.com/docs/caddyfile/directives/reverse_proxy)
