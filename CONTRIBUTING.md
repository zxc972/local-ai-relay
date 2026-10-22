# Contributing

Local AI Relay uses Python 3.10 or newer and the Python standard library at
runtime. Each room has a commanding `sender` and an executing `receiver`.
Agent integrations must preserve those roles, the user's authorization scope,
and durable replies before acknowledging work.

## Development

Create and activate a virtual environment with your platform's Python, then run:

```sh
python -m pip install -e .
python -m unittest discover -s tests -v
```

On Windows, `py -3 -m venv .venv` creates an environment; use
`.venv\Scripts\python.exe` for commands if activation is unavailable. On macOS
and Linux, use `python3 -m venv .venv` and `.venv/bin/python`.

All automated messaging tests use temporary data directories and fake Agent
handlers. They must not call paid models, modify personal client settings,
or send messages into an existing user's room. Set `AIRELAY_DATA_DIR` or pass
`--data-dir` for manual experiments. Never commit databases, credentials,
personal paths, or captured conversations.

## Build and installation checks

```sh
python -m pip install build setuptools wheel
python -m build
python -m unittest discover -s tests -p test_install.py -v
```

The installation smoke test builds a wheel without network access, installs it
into a temporary virtual environment, and executes the installed CLI and MCP
server from outside the checkout. It requires `setuptools` and `wheel` in the
test interpreter; without them, only that test is skipped. CI installs these
build tools and exercises Windows, macOS, and Linux.

## Adapter contributions

Keep idle waiting in a background process. Invoke an Agent only when a durable
message arrives. A successful adapter process exit does not itself establish
that the Agent completed its task. State clearly whether an integration sends
to an existing session, starts a new session, or requires a host extension.

Add tests for sender and receiver delivery, no-message idle behavior, clean
shutdown, and failure recovery. Include a real-client verification note when
available; protocol tests alone do not establish host support. Avoid automatic
retry when a message might already have caused file or network changes.

## Pull requests

Describe the concrete behavior changed, how to reproduce it, and the checks
performed. Keep generated build output out of the repository. Packaging tests
and CI passing are release preparation; publishing a GitHub release or PyPI
package is a separate maintainer action.
