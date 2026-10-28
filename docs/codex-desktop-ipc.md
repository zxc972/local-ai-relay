# Experimental Codex desktop adapter (macOS)

A real idle-chat echo has been verified on macOS using the desktop's session-owner IPC. This adapter does not use the CLI daemon socket. It starts no new app-server or model process and makes no model calls while idle.

This is an internal, version-sensitive protocol. Opt in with a wake config:

```json
{"type":"codex-desktop","thread_id":"YOUR_EXISTING_CHAT_ID","timeout":10}
```

Then use the existing command:

```sh
airelay connect work --as receiver --wake-config receiver-wake.json
```

Use `--as sender` for the sender side. Keep the selected chat loaded in the desktop app. The default socket is resolved from `CODEX_HOME`, or `~/.codex`; no personal path is required. A non-default `socket_path` may be configured explicitly. Unix socket and parent ownership/write permissions are checked without changing them.

`doctor` only discovers the owner and external-input support; it does not send a message. Delivery adds relay metadata as external tool output. An accepted turn ID proves host acceptance, not task completion. An error after sending is uncertain and is never automatically retried, except the exact start-turn rejection `App context must wait until the current turn finishes`. The installed host checks this before starting the turn. The adapter marks only that rejection as `busy` / `retry_safe: true`; chatroom workers retain the notification and defer another attempt for 3 seconds without calling a model. Socket timeouts and all unknown outcomes still require review.

This does not yet provide a Claude desktop adapter or verified two-app communication. Windows named pipes are not implemented by this adapter. Do not present the Mac result as Windows support.
