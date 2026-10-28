# 本地版独立开发

本目录 `local-ai-relay_local` 用于后续本地 Agent 协作功能的开发。正在运行的服务继续使用原目录和原配置，本目录的修改不会自动部署到它。

| 环境 | 目录 | 默认端口 | 数据 |
| --- | --- | --- | --- |
| 现用服务 | `../local-ai-relay` | 8787 | 原目录 `.relay/` |
| 另一开发副本 | `../local-ai-relay-dev` | 8788 | 该目录 `.relay/` |
| 本地版开发 | 当前目录 | 8789 | 当前目录 `.local-dev/` |

本目录已有的 `.relay/` 是复制遗留数据，不作为开发运行状态使用。不要直接恢复里面的成员身份、监听器或执行器，尤其不要按照里面的 PID 关闭进程。

启动本地开发网页：

```console
cd local-ai-relay_local
python3 local_dev.py serve --open
```

上面的目录名是开发副本示例，使用你实际克隆或解压的目录。Windows 将 `python3` 换成 `py -3`。浏览器地址为 `http://127.0.0.1:8789/`。需要停止时，只在该命令窗口按 Ctrl+C。不会安装后台常驻服务或修改现用客户端配置。

开发命令统一使用此入口，例如：

```console
python3 local_dev.py room create local-test
python3 local_dev.py mcp-config
```

`local_dev.py` 固定数据目录；源码中的默认数据位置也单独指定为 `.local-dev/`，防止继承现用环境的 `AIRELAY_DATA_DIR`。显式参数只用于有意安排的独立测试，不应指向现用数据。不要使用已安装且可能指向旧目录的全局 `airelay`。

本地入口提供简洁 MCP 工具集，邀请可选择“指导 / 执行 / 自由交流”，接入按后台通知和回调能力选择。分工是提示约定，不新增权限系统；不代表 WorkBuddy、豆包或其他客户端已经完成自动唤醒验证。完整接口通过 `python3 relay.py` 使用，测试用显式临时数据目录。
