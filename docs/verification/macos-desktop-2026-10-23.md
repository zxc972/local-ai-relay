# macOS desktop integration check — 2026-10-23

Status: BLOCKED, not an end-to-end pass.

Scope: existing Codex desktop and Claude Code desktop; idle turns must be woken by the relay, without AI polling. The user authorized a new Claude Code echo-only test session. No business tasks were sent.

## Observations

- Installed Codex native executable: `/Applications/ChatGPT.app/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex`.
- Running `app-server daemon version` exited 1: failed to connect to `$CODEX_HOME/app-server-control/app-server-control.sock`, No such file or directory (os error 2).
- This blocks the current default queue adapter before message delivery. It does not establish that every desktop integration is impossible.
- Claude desktop Code mode opened successfully and displayed no existing sessions.
- Prepared an echo-only prompt requesting `RELAY_MAC_READY_20261023`; no tools, file edits, or background tasks were requested.
- UI input was visible in the draft. Submission was not confirmed: native automation returned repeated `noWindowsAvailable`, `elementHasNoFrame`, and invalid element ID errors around the folder menu/send control.
- Read-only installed-code inspection found internal Claude session send methods and SDK channel options. Internal methods are not a verified external API. No application binaries were modified or private IPC protections bypassed.

## Acceptance evidence

| Stage | Result |
| --- | --- |
| Relay background waiting without model calls | Existing implementation, not retested in this desktop check |
| Codex default host endpoint available | Failed |
| Claude echo response | Not observed |
| Relay wakes an idle Claude desktop chat | Not verified |
| Relay wakes an idle Codex desktop chat | Not verified |
| Automatic bidirectional acknowledgement | Not verified |
| Windows desktop compatibility | Not tested here |

No claim of automatic desktop connection or two-command readiness follows from this check. Next work requires an actual externally callable host wake entry for each desktop application, followed by an isolated nonce roundtrip. Starting another CLI/model session or manually clicking Send would not satisfy the existing-desktop requirement.

## Follow-up: real Codex desktop wake verified

The user explicitly approved testing the local internal IPC. The app's actual socket is under `$CODEX_HOME/ipc/ipc.sock` (default `~/.codex/ipc/ipc.sock`), distinct from the CLI daemon control socket above.

- Initialized with client type `local-ai-relay`.
- Discovery initially timed out while the test chat was not loaded. After opening the existing relay test chat, discovery succeeded and advertised `supportsUntrustedAppInput: true`.
- A standalone Python process sent one echo via `thread-follower-start-turn`.
- The first echo turn completed with `RELAY_CODEX_IPC_ECHO_20261023`; the chat returned to idle. Local turn IDs are omitted from this public report.
- The new `codex-desktop` adapter then sent a second event as `function_call_output`, preserving the external-content trust boundary instead of making it a user/developer message.
- The external-input echo turn completed with `RELAY_CODEX_EXTERNAL_ECHO_20261023`; the chat returned to idle again.
- Five protocol/safety unit tests and nine existing adapter tests passed. These tests are separate from the actual desktop observations above.
- Claude's first echo is now visibly confirmed in the existing test session: `RELAY_MAC_READY_20261023`, with completion state. This is only the ordinary input baseline, not a relay wake; the local session ID is omitted.

Remaining: Claude background wake and full automatic two-app roundtrip. Windows remains untested. The internal Codex protocol may change with app updates and currently requires the target chat to be loaded. No desktop binaries, authentication checks, permission policies, or app startup configuration were changed.

Full regression follow-up: `python3 -m unittest discover -s tests` completed successfully outside the filesystem/network sandbox: 72 tests ran, 5 skipped. The first sandboxed run failed to bind an ephemeral loopback HTTP port; it was not a product regression. No real-chat sends occur in this test suite.
