"""Build a source deployment/client ZIP without data, credentials or build deps."""
from __future__ import annotations

import argparse
import hashlib
from pathlib import Path
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]
FILES = (
    "LOCAL_DEVELOPMENT.md", "启动 AirRelay.command", "启动 AirRelay.bat",
    "README.md", "LICENSE", "CONTRIBUTING.md", "pyproject.toml", "MANIFEST.in",
    "Dockerfile", ".dockerignore", ".gitignore", "bin/airelay",
    "deploy/compose.yaml", "deploy/Caddyfile", "deploy/.env.example",
    "docs/local-collaboration.md", "docs/deployment.md", "docs/chatrooms.md", "docs/network-chatrooms.md",
    "docs/context-management.md", "docs/claude-clients.md", "docs/legacy-mailbox.md",
    "docs/codex-desktop-ipc.md", "docs/chat-safety.md", "docs/controlled-executor.md", "scripts/build_release.py", "scripts/verify_chat_safety.py", "scripts/verify_executor.py",
)
PATTERNS = ("*.py", "static/*", "extensions/*.ts", "examples/*.py", "tests/*.py", "tests/*.mjs")


def build(output):
    files = {ROOT / name for name in FILES}
    for pattern in PATTERNS:
        files.update(p for p in ROOT.glob(pattern) if p.is_file())
    output = Path(output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="airelay-release-", dir=output.parent) as tmp:
        archive = Path(tmp) / "release.zip"
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as bundle:
            for source in sorted(files):
                if source.is_symlink() or not source.is_file():
                    raise ValueError("Missing or symlinked release file: " + str(source.relative_to(ROOT)))
                bundle.write(source, "local-ai-relay/" + source.relative_to(ROOT).as_posix())
        archive.replace(output)
    return {"path": str(output), "files": len(files), "bytes": output.stat().st_size,
            "sha256": hashlib.sha256(output.read_bytes()).hexdigest()}


if __name__ == "__main__":
    import json
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "dist/local-ai-relay-web.zip")
    print(json.dumps(build(parser.parse_args().output), ensure_ascii=False, indent=2))
