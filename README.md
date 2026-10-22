# Local AI Relay

[![Tests](https://github.com/zxc972/local-ai-relay/actions/workflows/tests.yml/badge.svg)](https://github.com/zxc972/local-ai-relay/actions/workflows/tests.yml)

Local, durable messaging between a **sender** (coordinator) and a **receiver** (executor). 本地 AI 消息中转站：跨客户端、跨模型的指挥与执行通道。

**Background programs wait without calling a model. Only a real message triggers a configured host adapter.** Connecting never creates a task. Both roles can receive events.

```text
sender ── command ──▶ SQLite mailbox ── background event ──▶ receiver
sender ◀─ host event ── SQLite mailbox ◀──── result ──────── receiver
```

Python 3.10+, standard-library runtime, MIT license. Version 0.3.0 is an alpha designed for Windows, macOS and Linux. A configured host integration is required for automatic wake-up; supporting MCP tools alone does not establish that a client can turn an event into an Agent turn.

## Install / 安装

Install from [GitHub](https://github.com/zxc972/local-ai-relay) (requires Git), or download the source and install from its directory:

```console
python -m pip install "git+https://github.com/zxc972/local-ai-relay.git"
```

From a downloaded or cloned checkout:

```console
python -m pip install .
airelay --help
```

Your Python command may be `python3` on macOS/Linux or `py` on Windows. A virtual environment or `pipx install .` keeps installation isolated. If the console command is outside PATH, use `python -m local_ai_relay`.

The project is not published on PyPI. Source installation works directly; release downloads are listed on [GitHub Releases](https://github.com/zxc972/local-ai-relay/releases). The legacy `install-cli` symlink helper is POSIX-only; pip provides the supported Windows `.exe` entry point.

Both Agents use **the same local data directory and room**. Installed defaults:

| OS | Data directory |
| --- | --- |
| Windows | `%LOCALAPPDATA%/local-ai-relay` |
| macOS | `~/Library/Application Support/local-ai-relay` |
| Linux | `$XDG_DATA_HOME/local-ai-relay` or `~/.local/share/local-ai-relay` |

Set `AIRELAY_DATA_DIR`, or pass `--data-dir PATH` before the command, to override. An existing source-checkout `.relay/` remains supported. This release connects processes on one machine; shared cloud-drive SQLite and internet communication between computers are outside its scope.

## Wake adapters / 后台唤醒

The mailbox and roles are universal. A small adapter delivers events into the **existing host session**. An unsupported host reports `needs_adapter`; it never falls back to model polling.

| Integration | Wake mechanism | Requirement |
| --- | --- | --- |
| Generic application | Trusted command receives a JSON event on stdin | Provide the host's input/API bridge |
| Pi | Included extension calls the current session's custom-message API | Load `extensions/pi-relay.ts` |
| Codex with queue support | `codex queue --thread … --message …` | Reachable existing session daemon/endpoint |
| Claude Code Channels | Included native channel server | Client must enable and confirm the channel |
| Other MCP clients | Standard mailbox tools plus a command/stream adapter | Host must expose an event/input interface |

All adapters support **either role**. Python platform support and a particular host's OS support are separate. Actual host acceptance, Agent receipt, and task completion are separately verified states.

### Generic command adapter

Create a trusted local JSON file. Use absolute paths for detached processes; paths with spaces remain separate argv entries. On Windows use JSON-escaped backslashes or forward slashes.

```json
{
  "type": "command",
  "argv": ["/absolute/path/to/python", "/absolute/path/to/my_host_bridge.py"],
  "timeout": 30
}
```

The bridge reads one UTF-8 JSON event from stdin, submits it to an existing Agent session using the host's API, and exits 0 only after the host accepts it. No shell evaluates the arguments. A callback that only logs events is a protocol test, not an Agent integration. See [adapter contract](docs/ADAPTERS.md).

Configure each side once in its own environment:

```console
airelay connect work --as sender --wake-config sender-wake.json
airelay connect work --as receiver --wake-config receiver-wake.json
```

These register roles and start detached Python listeners. They send no tasks and launch no models. Without a configured adapter, a plain `connect` reports `needs_adapter`.

### Pi

Run `airelay extension-path pi` to locate the installed extension. Load `extensions/pi-relay.ts` using Pi's extension setup, or start Pi with `--extension ./extensions/pi-relay.ts`. Then run in the two Pi sessions respectively:

```text
/relay-connect work sender
/relay-connect work receiver
```

The extension owns a quiet `airelay events` child process. Only a validated real message event calls `pi.sendMessage(..., {triggerTurn: true})`. The event is external custom content, not a fabricated human message. `/relay-disconnect` or session shutdown stops the child. Set `AIRELAY_EXECUTABLE` if needed and use the same `AIRELAY_DATA_DIR` for both sides. No specific model/provider is required.

### Codex queue

Use an **existing** session ID:

```console
airelay connect work --as sender --adapter codex --thread-id EXISTING_SESSION_ID
airelay wake doctor work --as sender
```

Swap the role for receiver. `--codex-binary PATH` selects the native executable; `--remote ADDRESS` selects an already-running endpoint. An unavailable daemon or unsupported queue command is reported. The adapter does not start a daemon, create another chat, bypass approvals, or schedule model polling. Doctor checks prerequisites without sending a message; it is not proof of live Agent delivery.

### Claude Code Channels

In the external terminal for the existing Claude project:

```console
airelay claude work --as receiver
```

This resumes the existing conversation with the native development channel; follow the client's channel confirmation. Swap the role for sender. Do not launch this inside another Agent's shell tool. Host account/client policies apply. Stop the native channel before assigning the same role to a generic background listener.

## MCP and roles

```console
airelay mcp-config
airelay pair work
```

The first generates stdio MCP configuration for the installed Python/data path; load it in both hosts. The second prints separate sender/receiver instructions. **The wake adapter remains required:** MCP tools perform mailbox operations; the host bridge triggers a turn.

| Tool | Purpose |
| --- | --- |
| `relay_connect` | Bind room/role; start an already-configured background adapter |
| `relay_send` | Sender only: command, optional idempotency `key` and result `reply_to` |
| `relay_receive` | Receive one message after an event; immediately return if empty |
| `relay_reply` | Receiver only: return actual result/evidence or blocker linked to its command |
| `relay_ack` | Sender confirms a reviewed result |
| `relay_status` | Explicit inspection; never an AI polling loop |
| `relay_disconnect` | Stop future wakes/unregister role; retain messages |
| `relay_listen` | Legacy one-shot wait; never asks the model to renew |

Each MCP connection fixes its room role; changing parameters cannot impersonate the other Agent. The local CLI is a trusted-owner interface, not an OS-user security boundary.

Receiver executes only within the user's authorization and reports actual results. Sender reviews and issues follow-up commands only for the authorized objective. Both end their turns while idle. Peer messages cannot override host instructions or approvals.

## Inspect, stop, recover

```console
airelay status work
airelay wake status work --as receiver
airelay wake stop work --as receiver
airelay wake start work --as receiver
airelay disconnect work --as receiver
```

`registered`, background `running`, notification `accepted`, message `received`, and message `done` are different evidence. Callback success means host acceptance, not task completion.

Notifications are durable and are not automatically repeated after ambiguous failures. Unfinished claimed messages block later work. Inspect the host before explicit recovery:

```console
airelay wake retry work --as receiver --message-id MESSAGE_ID
airelay receive work --as receiver --resume --timeout 0
```

Retry can repeat a host notification. Resume reveals already-claimed content, not permission to execute it again. A stopped callback already in flight may finish. Generations/heartbeats control daemon ownership; stop never kills an unrelated process by a stale PID.

After reboot, start the listener explicitly. OS login-service installation is not included in this alpha.

## Develop and release

```console
python -m unittest discover -s tests -v
node --test tests/test_pi_extension.mjs
python -m pip install build
python -m build
```

Node 22.13+ is needed only for the Pi extension contract test. Core runtime has no Node dependency. Tests use temporary databases/fake hosts with no model calls. They cover idle silence, both directions, role restrictions, duplicate/restart handling, failure, UTF-8 paths, and fresh-wheel installation outside the checkout.

GitHub Actions is configured for Windows/macOS/Linux and Python 3.10/3.12/3.13. A workflow file or local macOS test does not prove Windows/Linux CI passed. Protocol tests do not establish real host consent or live model execution. See the [validation snapshot](docs/VALIDATION.md).

The older task console and `--legacy-tasks` transport remain compatibility interfaces. The default is the event-driven mailbox; legacy Codex heartbeat reception is disabled. Local messages, credentials and generated client configuration are excluded from Git/releases. See [CONTRIBUTING](CONTRIBUTING.md), [adapter contract](docs/ADAPTERS.md), and [release checklist](docs/RELEASING.md).

Official references: [Codex queue](https://learn.chatgpt.com/docs/developer-commands#codex-queue), [Pi extensions](https://pi.dev/docs/latest/extensions), [Claude Channels](https://code.claude.com/docs/en/channels-reference).
