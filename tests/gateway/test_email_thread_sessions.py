"""Per-thread email sessions (EMAIL_THREAD_SESSIONS / platforms.email.thread_sessions)."""

import asyncio
import os
import unittest
from unittest.mock import MagicMock, patch

from gateway.config import Platform, PlatformConfig
from gateway.session import build_session_key

_ENV = {
    "EMAIL_ADDRESS": "hermes@test.com",
    "EMAIL_PASSWORD": "secret",
    "EMAIL_IMAP_HOST": "imap.test.com",
    "EMAIL_SMTP_HOST": "smtp.test.com",
    "EMAIL_ALLOW_ALL_USERS": "true",
}


def _make_adapter(extra=None, env=None):
    with patch.dict(os.environ, {**_ENV, **(env or {})}):
        from plugins.platforms.email.adapter import EmailAdapter
        return EmailAdapter(PlatformConfig(enabled=True, extra=extra or {}))


def _msg(message_id, subject="Topic", in_reply_to="", references=""):
    return {"uid": b"1", "sender_addr": "user@test.com", "sender_name": "User", "subject": subject,
            "message_id": message_id, "in_reply_to": in_reply_to, "references": references, "date": "",
            "sender_authenticated": True, "auth_reason": "dmarc=pass", "body": "hi", "attachments": []}


class TestThreadSessions(unittest.TestCase):
    def setUp(self):
        self._env = patch.dict(os.environ, {"EMAIL_ALLOW_ALL_USERS": "true"})
        self._env.start()

    def tearDown(self):
        self._env.stop()

    def _dispatch(self, adapter, msg_data):
        events = []

        async def _capture(event):
            events.append(event)
        adapter.handle_message = _capture
        adapter._sender_accepted = lambda *_: True
        asyncio.run(adapter._dispatch_message(msg_data))
        return events[0]

    def _key(self, event):
        return build_session_key(event.source)

    def test_default_off_keeps_one_session_per_sender(self):
        adapter = _make_adapter()
        a = self._dispatch(adapter, _msg("<a@x>", "One"))
        b = self._dispatch(adapter, _msg("<b@x>", "Two"))
        self.assertIsNone(a.source.thread_id)
        self.assertEqual(self._key(a), self._key(b))

    def test_new_subjects_get_separate_sessions(self):
        adapter = _make_adapter(env={"EMAIL_THREAD_SESSIONS": "true"})
        a = self._dispatch(adapter, _msg("<a@x>", "One"))
        b = self._dispatch(adapter, _msg("<b@x>", "Two"))
        self.assertNotEqual(self._key(a), self._key(b))
        self.assertTrue(self._key(a).startswith("agent:main:email:dm:user@test.com:"))

    def test_config_extra_enables_too(self):
        adapter = _make_adapter(extra={"thread_sessions": True})
        self.assertTrue(adapter._thread_sessions)

    def test_reply_continues_thread_via_references(self):
        adapter = _make_adapter(env={"EMAIL_THREAD_SESSIONS": "true"})
        root = self._dispatch(adapter, _msg("<a@x>", "One"))
        reply = self._dispatch(adapter, _msg("<c@x>", "Re: One", in_reply_to="<hermes-1@test.com>",
                                             references="<a@x> <hermes-1@test.com>"))
        self.assertEqual(self._key(root), self._key(reply))

    def test_in_reply_to_only_maps_back_through_our_sent_ids(self):
        adapter = _make_adapter(env={"EMAIL_THREAD_SESSIONS": "true"})
        root = self._dispatch(adapter, _msg("<a@x>", "One"))
        with patch("smtplib.SMTP") as smtp:
            smtp.return_value = MagicMock()
            sent = asyncio.run(adapter.send("user@test.com", "answer", "<a@x>",
                                            metadata={"thread_id": root.source.thread_id}))
        reply = self._dispatch(adapter, _msg("<c@x>", "Re: One", in_reply_to=sent.message_id or ""))
        self.assertEqual(self._key(root), self._key(reply))

    def test_replies_use_their_threads_subject_and_references(self):
        adapter = _make_adapter(env={"EMAIL_THREAD_SESSIONS": "true"})
        a = self._dispatch(adapter, _msg("<a@x>", "Quarterly report"))
        self._dispatch(adapter, _msg("<b@x>", "Team offsite"))  # newer thread from the same sender
        with patch("smtplib.SMTP") as smtp:
            server = MagicMock()
            smtp.return_value = server
            asyncio.run(adapter.send("user@test.com", "answer", "<a@x>", metadata={"thread_id": a.source.thread_id}))
            sent = server.send_message.call_args[0][0]
        self.assertEqual(sent["Subject"], "Re: Quarterly report")
        self.assertEqual(sent["In-Reply-To"], "<a@x>")
        self.assertEqual(sent["References"], "<a@x>")

    def test_subject_change_mid_thread_does_not_split(self):
        adapter = _make_adapter(env={"EMAIL_THREAD_SESSIONS": "true"})
        a = self._dispatch(adapter, _msg("<a@x>", "One"))
        b = self._dispatch(adapter, _msg("<d@x>", "Re: something else", in_reply_to="<a@x>", references="<a@x>"))
        self.assertEqual(self._key(a), self._key(b))

    def test_send_without_thread_falls_back_to_latest_sender_context(self):
        adapter = _make_adapter(env={"EMAIL_THREAD_SESSIONS": "true"})
        self._dispatch(adapter, _msg("<a@x>", "One"))
        with patch("smtplib.SMTP") as smtp:
            server = MagicMock()
            smtp.return_value = server
            asyncio.run(adapter.send("user@test.com", "cron output"))
            sent = server.send_message.call_args[0][0]
        self.assertEqual(sent["Subject"], "Re: One")


if __name__ == "__main__":
    unittest.main()
