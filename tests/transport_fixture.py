"""Test-only access to retired automatic transport primitives.

These tests verify delivery mechanics, NOT a safe native Agent integration.
Production entry points never expose this override. Security tests do not use it.
"""
from contextlib import ExitStack, contextmanager
from functools import wraps
from unittest.mock import patch

from chat_background import _transport_client
from chat_server import ChatService


def transport_only(test):
    @wraps(test)
    def wrapped(*args, **kwargs):
        with ExitStack() as stack:
            for module in ("chat_background", "chat_remote"):
                stack.enter_context(patch(module + ".resolve_client", _transport_client))
                stack.enter_context(patch(module + ".require_automatic_host", return_value=None))
            stack.enter_context(patch.object(ChatService, "peek", ChatService._transport_peek))
            return test(*args, **kwargs)
    return wrapped
