# Wake adapter contract

The Python background worker watches the local mailbox every 250 ms. Empty waiting performs no model/API calls. Any host wake occurs only after a real queued message.

## Command callback

Trusted configuration:

```json
{"type":"command","argv":["python","/path/to/host_bridge.py"],"timeout":30}
```

Optional `cwd` selects the working directory. The executable runs with `shell=False`; Windows batch wrappers are rejected. Use a native executable or interpreter plus script. Keep credentials in the host environment/credential store, not messages.

The program receives one UTF-8 JSON object on stdin:

```json
{
  "schema": "airelay.wake.v1",
  "event": "message_available",
  "room": "work",
  "role": "receiver",
  "kind": "command",
  "message_id": "message-uuid",
  "sender": "sender",
  "created_at": 0,
  "receive_argv": ["python", "/path/to/relay.py", "--data-dir", "/data", "receive", "work", "--as", "receiver", "--timeout", "0"],
  "receive_tool": {"name":"relay_receive","arguments":{"room":"work","role":"receiver"}}
}
```

Metadata has no peer message text. Prefer `receive_argv` over parsing a shell command. Validate schema, room, role and target session. Use the host's supported event/input API to wake that existing session, with external-content attribution. Never simulate human authorization or skip permission checks.

Exit 0 only after host acceptance. Timeout/nonzero exits are ambiguous: the host may have accepted before failing. The relay persists `needs_attention`, without automatic retry. Explicit retry may duplicate a notification; hosts should deduplicate `message_id` too.

The Agent atomically receives one queued message, performs authorized work, and replies/acknowledges. A notification never claims a message or completes a task. A received, unfinished message blocks later notifications.

## Extension event stream

```console
airelay events work --as receiver
```

Run this as a long-lived child of the host extension. It prints JSONL only for real new messages: no banners, empty events or recurring model instructions. Both roles use the same contract. Successful pipe write is not proof of host/Agent consumption; reconnect never automatically replays ambiguous notifications.

Clean up on session shutdown. Do not silently attach the subscription to a replacement chat. Use stable `AIRELAY_SESSION_ID` when available. Failed hosts retain queued work and require explicit recovery.

## Built-in integrations

Built-in adapters implement `normalize`, `doctor`, `deliver` in `adapters.py`. Doctor checks prerequisites without submitting work. Registry entries must be available to detached processes; registering only in an ephemeral parent is insufficient. Most third-party integrations can use the command/stream contract without modifying the package.

Include model-free tests for idle silence, one event, offline/timeout, role selection, session shutdown, and ambiguous delivery. Report actual host acceptance and live Agent execution separately from protocol tests.
