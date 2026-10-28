"""Effective reception state includes both background and native listeners."""
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from background import Background
from message_bus import Mailbox


class ReceptionStatusTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

    def test_background_receiver_is_active_even_when_legacy_auto_receive_is_off(self):
        with mock.patch.dict(os.environ, {"CODEX_THREAD_ID": "", "AIRELAY_SESSION_ID": ""}):
            mailbox = Mailbox(self.temp.name, "status-check")
            mailbox.connection.join("receiver")

            def state(role):
                active = role == "receiver"
                return {"configured": active, "running": active, "worker_active": active,
                        "enabled": active, "needs_attention": [], "error": None,
                        "adapter": {"type": "codex-desktop", "thread_id": "exact-codex"} if active else None}

            with mock.patch.object(Background, "status", side_effect=state):
                result = mailbox.connect("sender")
        receiver = result["peers"]["receiver"]
        self.assertFalse(receiver["auto_receive"]["enabled"])
        self.assertEqual(receiver["reception"], {
            "automatic": True, "mode": "background", "target_session": "exact-codex"})

    def test_native_channel_is_active_without_a_generic_adapter(self):
        with mock.patch.dict(os.environ, {"CODEX_THREAD_ID": "", "AIRELAY_SESSION_ID": ""}):
            mailbox = Mailbox(self.temp.name, "channel-check")
            inactive = {"configured": False, "running": False, "worker_active": False,
                        "enabled": False, "needs_attention": [], "error": None, "adapter": None}
            with mock.patch.object(Background, "status", return_value=inactive), \
                    mock.patch("auto_receive.describe", return_value={
                        "enabled": True, "client": "claude", "status": "listening"}):
                result = mailbox.connect("sender")
        self.assertEqual(result["mode"], "channel")
        self.assertTrue(result["listen_enabled"])
        self.assertTrue(result["peers"]["sender"]["reception"]["automatic"])
