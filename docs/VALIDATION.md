# Validation snapshot — 2026-10-22

## Cross-platform CI

All **nine jobs passed** in [GitHub Actions run 39779976068](https://github.com/zxc972/local-ai-relay/actions/runs/39779976068) at commit `eccaf3814a4295e3182e4a1b8d5d2e7ff2c604af`:

| Platform | Python 3.10 | Python 3.12 | Python 3.13 |
| --- | --- | --- | --- |
| Windows | Passed | Passed | Passed |
| macOS | Passed | Passed | Passed |
| Linux | Passed | Passed | Passed |

Every job installs the project, passes **67 Python tests and eight Pi extension contract tests**, and builds the wheel/source distributions. Installed-wheel checks exercise both roles and detached callbacks outside the source checkout. The Windows run exposed timing assumptions in the timeout/cleanup tests; the corrected tests accept startup-time timeout while still requiring exactly one durable attempt, and only retry transient Windows file-sharing conflicts during cleanup.

## Local checks

Local macOS validation before the CI portability corrections:

- **66 Python tests passed**, with no skips in the final run. Includes legacy compatibility, event listeners, host adapters, MCP role checks, localhost HTTP checks and isolated installed-wheel tests.
- **8 Pi extension contract tests passed**, using a fake Pi host. Connect/idle/shutdown trigger no model messages; validated events trigger the current session callback.
- Fresh wheels installed into isolated virtual environments work outside the source checkout. Installed background workers notify both sender and receiver, and stop cleanly.
- Source and wheel archive inspections exclude local databases, credentials, client configuration, logs, screenshots and personal filesystem paths. Extension and documentation resources are included.

Commands:

```console
python -m unittest discover -s tests -v
node --test tests/test_pi_extension.mjs
```

The Python build tests require setuptools/wheel/pip; the Node contract test requires Node 22.13+. No tests invoke paid model APIs or submit work to a real user chat.

## Evidence limits

- The CI matrix verifies the listed Python/runner combinations, not every OS version or third-party Agent client.
- Fake host acceptance and MCP protocol tests are not evidence of real model execution or native client consent.
- The locally inspected Codex CLI supports `queue`, but its default existing-session daemon endpoint was unavailable. The adapter reports this through preflight and does not start another model/session.
- Pi's event API and Claude's native channel contract are integrated, with model-free tests. A user must load/authorize the host extension/channel before live delivery can be established.
- Background workers make no model calls while idle. A host may perform unrelated model calls independently; this project does not control those.

Treat this as an alpha validation snapshot. Live host integration needs separate evidence before claiming a specific Agent client is connected.
