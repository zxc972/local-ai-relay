"""Explicit conversation routing, independent of shared MCP connections."""
from __future__ import annotations

import hashlib
import re

if __package__:
    from .core import RelayError
else:
    from core import RelayError


def session_id(value):
    if value is not None and (not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}", value)):
        raise RelayError("session_id 须为 1–200 位字母、数字、点、冒号、下划线或连字符")
    return value


def session_owner(value):
    return "session:" + hashlib.sha256(session_id(value).encode()).hexdigest()


def check_target(value, thread_id=None, config=None):
    """A known native thread must agree even when no listener is running."""
    session_id(value)
    if value and any(target and target != value for target in (thread_id, (config or {}).get("thread_id"))):
        raise RelayError("会话绑定不符：session_id 必须与当前唤醒 thread_id 一致，不能改投另一条会话", 409)


def binding(value):
    return {"session_binding": {"status": "bound" if value else "legacy_unbound",
                                **({"session_id": value} if value else {})}}
