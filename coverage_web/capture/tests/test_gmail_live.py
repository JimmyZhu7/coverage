"""Gmail Live's deterministic extractors — the only classification this
pipeline does (see gmail_live.py's module docstring on what it deliberately
does NOT try to infer). No network, no Google client, no database: every
function under test here is a pure function of a message dict.
"""

from __future__ import annotations

import base64

from capture import gmail_live

OWN_EMAIL = "jimmy@example.com"


def _headers(**kwargs) -> list[dict]:
    return [{"name": k, "value": v} for k, v in kwargs.items()]


def _b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode("utf-8")).decode("ascii")


def _message(headers: dict, snippet: str = "", parts: list[dict] | None = None) -> dict:
    payload = {"headers": _headers(**headers)}
    if parts is not None:
        payload["parts"] = parts
    return {"threadId": "t1", "snippet": snippet, "payload": payload}


class TestOutboundVsInbound:
    def test_outbound_message_is_outreach_sent(self):
        message = _message(
            {"From": "Jimmy <jimmy@example.com>", "To": "Alice <alice@firm.com>",
             "Subject": "Following up"},
        )
        finding = gmail_live._classify_message(OWN_EMAIL, message)
        assert finding["outreach_sent"] is True
        assert finding["replied"] is False
        assert finding["email"] == "alice@firm.com"
        assert finding["chat_status"] == "none"

    def test_inbound_message_is_a_reply(self):
        message = _message(
            {"From": "Alice <alice@firm.com>", "To": "jimmy@example.com",
             "Subject": "Re: Following up"},
            snippet="Sure, happy to chat!",
        )
        finding = gmail_live._classify_message(OWN_EMAIL, message)
        assert finding["replied"] is True
        assert finding["outreach_sent"] is False
        assert finding["email"] == "alice@firm.com"

    def test_outbound_with_no_to_header_is_skipped(self):
        message = _message({"From": OWN_EMAIL, "Subject": "no recipient"})
        assert gmail_live._classify_message(OWN_EMAIL, message) is None


class TestBounceDetection:
    def test_mailer_daemon_sender_is_a_bounce(self):
        message = _message(
            {"From": "Mail Delivery Subsystem <mailer-daemon@googlemail.com>",
             "To": OWN_EMAIL, "Subject": "Delivery Status Notification (Failure)"},
            snippet="The following address failed: rando@deadfirm.com. "
                    "The recipient's address rejected your message: 550 5.1.1.",
        )
        finding = gmail_live._classify_message(OWN_EMAIL, message)
        assert finding["bounced"] is True
        assert finding["email"] == "rando@deadfirm.com"
        assert finding["replied"] is False
        assert finding["outreach_sent"] is False

    def test_bounce_with_no_recoverable_address_is_dropped(self):
        message = _message(
            {"From": "postmaster@example.com", "To": OWN_EMAIL,
             "Subject": "Undeliverable: hello"},
            snippet="Delivery failed, no address in this snippet at all.",
        )
        assert gmail_live._classify_message(OWN_EMAIL, message) is None

    def test_ordinary_reply_is_not_mistaken_for_a_bounce(self):
        message = _message(
            {"From": "Alice <alice@firm.com>", "To": OWN_EMAIL,
             "Subject": "Re: coffee chat"},
            snippet="Great chatting with you, does not conflict with my schedule.",
        )
        # "does not exist" would false-positive here if bounce detection ever
        # matched snippet text alone without the From/Subject signals too.
        finding = gmail_live._classify_message(OWN_EMAIL, message)
        assert finding["bounced"] is False
        assert finding["replied"] is True


class TestIcsScheduling:
    ICS_TEXT = (
        "BEGIN:VEVENT\n"
        "DTSTART:20260901T140000Z\n"
        "SUMMARY:Coffee chat with Alice\n"
        "END:VEVENT\n"
    )

    def test_inbound_ics_marks_chat_scheduled(self):
        message = _message(
            {"From": "Alice <alice@firm.com>", "To": OWN_EMAIL, "Subject": "Invite"},
            parts=[{"mimeType": "text/calendar", "body": {"data": _b64(self.ICS_TEXT)}}],
        )
        finding = gmail_live._classify_message(OWN_EMAIL, message)
        assert finding["chat_status"] == "scheduled"
        assert finding["chat_scheduled_at"] == "2026-09-01T14:00:00+00:00"
        assert "Alice" in finding["evidence"]

    def test_outbound_ics_also_marks_chat_scheduled(self):
        message = _message(
            {"From": OWN_EMAIL, "To": "alice@firm.com", "Subject": "Invite"},
            parts=[{"mimeType": "text/calendar", "body": {"data": _b64(self.ICS_TEXT)}}],
        )
        finding = gmail_live._classify_message(OWN_EMAIL, message)
        assert finding["outreach_sent"] is True
        assert finding["chat_status"] == "scheduled"

    def test_no_ics_part_leaves_chat_status_none(self):
        message = _message(
            {"From": "Alice <alice@firm.com>", "To": OWN_EMAIL, "Subject": "Re: hi"},
            snippet="just a plain reply",
        )
        finding = gmail_live._classify_message(OWN_EMAIL, message)
        assert finding["chat_status"] == "none"
        assert finding["chat_scheduled_at"] is None

    def test_extract_ics_schedule_returns_none_when_absent(self):
        assert gmail_live._extract_ics_schedule({"payload": {}}) == (None, None)


class TestTokenEncryption:
    def test_round_trips(self, settings):
        settings.GMAIL_LIVE_TOKEN_KEY = gmail_live.Fernet.generate_key().decode()
        ciphertext = gmail_live.encrypt_token("1//refresh-token-value")
        assert ciphertext != "1//refresh-token-value"
        assert gmail_live.decrypt_token(ciphertext) == "1//refresh-token-value"

    def test_wrong_key_raises_gmail_live_error(self, settings):
        settings.GMAIL_LIVE_TOKEN_KEY = gmail_live.Fernet.generate_key().decode()
        ciphertext = gmail_live.encrypt_token("secret")
        settings.GMAIL_LIVE_TOKEN_KEY = gmail_live.Fernet.generate_key().decode()
        with __import__("pytest").raises(gmail_live.GmailLiveError):
            gmail_live.decrypt_token(ciphertext)


class TestIsConfigured:
    def test_false_when_any_setting_missing(self, settings):
        settings.GMAIL_LIVE_CLIENT_ID = ""
        settings.GMAIL_LIVE_CLIENT_SECRET = "x"
        settings.GMAIL_LIVE_PUBSUB_TOPIC = "x"
        settings.GMAIL_LIVE_TOKEN_KEY = "x"
        assert gmail_live.is_configured() is False

    def test_true_when_all_four_set(self, settings):
        settings.GMAIL_LIVE_CLIENT_ID = "id"
        settings.GMAIL_LIVE_CLIENT_SECRET = "secret"
        settings.GMAIL_LIVE_PUBSUB_TOPIC = "projects/p/topics/t"
        settings.GMAIL_LIVE_TOKEN_KEY = "key"
        assert gmail_live.is_configured() is True
