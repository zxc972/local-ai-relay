#!/usr/bin/env python3
"""Isolated launcher for local-edition development, independent of live rooms."""
import argparse
import json
import os
from pathlib import Path
import sys
import urllib.request
import webbrowser

ROOT = Path(__file__).resolve().parent
os.environ["AIRELAY_DATA_DIR"] = str(ROOT / ".local-dev")

from chat_server import DEFAULT_PORT
from core import RelayError
from relay import dump, main


def running_url(arguments):
    """A chatroom already serving this port: reuse it, so starting twice just reopens the page."""
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--host", default="127.0.0.1")
    known, _ = parser.parse_known_args(arguments[1:])
    if known.host not in ("127.0.0.1", "localhost"):
        return None
    url = f"http://127.0.0.1:{known.port}/"
    try:
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(url + "health", timeout=1) as response:
            return url if json.load(response).get("service") == "ai-chatroom" else None
    except (OSError, ValueError):
        return None


if __name__ == "__main__":
    arguments = sys.argv[1:] or ["serve", "--open"]
    try:
        if any(value == "--data-dir" or value.startswith("--data-dir=") for value in arguments):
            raise RelayError("本地开发入口固定使用当前副本的 .local-dev，无需指定 --data-dir")
        if arguments == ["--help"] or arguments == ["-h"]:
            parser = argparse.ArgumentParser(description="AirRelay · 最多 8 人", epilog="高级功能仍可通过 python3 relay.py --help 查看。")
            parser.add_argument("command", nargs="?", choices=["serve", "room", "mcp-config", "mcp"],
                                help="默认打开网页；room 管理聊天；mcp-config 生成简洁接入配置；mcp 运行接入服务")
            parser.print_help()
            sys.exit(0)
        if arguments[0] not in ("serve", "room", "mcp-config", "mcp"):
            raise RelayError("本地入口使用 serve、room、mcp-config 或 mcp；高级功能请用 python3 relay.py")
        if arguments[0] == "serve" and (url := running_url(arguments)):
            opened = "--open" in arguments
            if opened:
                webbrowser.open(url)
            dump({"status": "already_running", "url": url, "note": "聊天室已在运行" + ("，已打开网页。" if opened else "。")})
            sys.exit(0)
        if arguments[0] in ("mcp", "mcp-config") and "--simple" not in arguments:
            arguments.append("--simple")
        main(["--data-dir", str(ROOT / ".local-dev"), *arguments])
    except (RelayError, OSError, ValueError) as error:
        dump({"error": str(error)})
        sys.exit(2)
