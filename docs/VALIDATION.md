# Validation snapshot — 2026-10-22

Local macOS validation of version 0.3.0:

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

- Windows/Linux have a configured GitHub Actions matrix; they have not yet been run on those OSes in this snapshot.
- Fake host acceptance and MCP protocol tests are not evidence of real model execution or native client consent.
- The locally inspected Codex CLI supports `queue`, but its default existing-session daemon endpoint was unavailable. The adapter reports this through preflight and does not start another model/session.
- Pi's event API and Claude's native channel contract are integrated, with model-free tests. A user must load/authorize the host extension/channel before live delivery can be established.
- Background workers make no model calls while idle. A host may perform unrelated model calls independently; this project does not control those.

Treat this as an alpha validation snapshot. Update it with actual remote CI and live host evidence before broad compatibility claims.
