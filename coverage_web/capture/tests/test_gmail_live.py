"""Gmail Live's deterministic extractors — the only classification this
pipeline does (see gmail_live.py's module docstring on what it deliberately
does NOT try to infer). No network, no Google client, no database: every
function under test here is a pure function of a message dict.
"""

from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone as dt_timezone

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

    def test_genuine_reply_carries_discovery_facts(self):
        """`threaded_reply` and `subject` ride the finding for the discovery
        hook (capture.discovery): the reply pointer is the "user emailed them
        first" evidence, and the subject is the most a proposal card may
        show."""
        message = _message(
            {"From": "Alice <alice@firm.com>", "To": "jimmy@example.com",
             "Subject": "Re: Following up",
             "In-Reply-To": "<sent-by-me@mail.example>"},
            snippet="Sure, happy to chat!",
        )
        finding = gmail_live._classify_message(OWN_EMAIL, message)
        assert finding["threaded_reply"] is True
        assert finding["subject"] == "Re: Following up"

        fresh = _message(
            {"From": "Alice <alice@firm.com>", "To": "jimmy@example.com",
             "Subject": "Hello"},
        )
        assert gmail_live._classify_message(OWN_EMAIL, fresh)["threaded_reply"] is False

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

    def test_a_genuine_reply_quoting_bounce_text_is_not_mistaken_for_a_bounce(self):
        """A real, live contact replying normally — but whose message
        happens to QUOTE the exact technical wording of a bounce they saw
        elsewhere ("I tried your old address and got this back...") — must
        not be reclassified as a bounce of their own, working address. The
        FROM is a real person, not mailer-daemon/postmaster, and the
        Subject is an ordinary reply subject; only the body text matches
        the bounce vocabulary, which used to be enough on its own to flip
        `_looks_like_bounce` to True and then have `_bounce_recipient` pick
        Alice's own (correctly working) email out of her message as the
        "failed" address."""
        message = _message(
            {"From": "Alice <alice@firm.com>", "To": OWN_EMAIL,
             "Subject": "Re: reaching you"},
            snippet=(
                "Hey, I tried your old address last week and got "
                "'recipient address rejected: 550 5.1.1' - here's my "
                "current one, alice@firm.com, going forward."
            ),
        )
        finding = gmail_live._classify_message(OWN_EMAIL, message)
        assert finding["bounced"] is False
        assert finding["replied"] is True
        assert finding["email"] == "alice@firm.com"


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
        assert gmail_live._extract_ics_schedule({"payload": {}}) == (None, None, None)

    def test_the_invite_uid_rides_along_with_the_time(self):
        """DTSTART says when, UID says WHICH EVENT — and only the second one
        survives a reschedule onto a different Gmail thread."""
        message = _message(
            {"From": "Alice <alice@firm.com>", "To": OWN_EMAIL, "Subject": "Invite"},
            parts=[{"mimeType": "text/calendar", "body": {"data": _b64(
                "BEGIN:VEVENT\n"
                "UID:abc123@google.com\n"
                "DTSTART:20260901T140000Z\n"
                "SUMMARY:Coffee chat with Alice\n"
                "END:VEVENT\n"
            )}}],
        )
        assert gmail_live._classify_message(OWN_EMAIL, message)["ics_uid"] == (
            "abc123@google.com"
        )

    def test_a_folded_uid_is_unfolded_not_truncated(self):
        """RFC 5545 splits any property past 75 octets across lines with a
        leading space. Google's UIDs are long enough to arrive folded, and
        half a UID still LOOKS like a key — it would quietly key two
        different events onto the same row."""
        uid = "0" * 60 + "@google.com"
        message = _message(
            {"From": "Alice <alice@firm.com>", "To": OWN_EMAIL, "Subject": "Invite"},
            parts=[{"mimeType": "text/calendar", "body": {"data": _b64(
                "BEGIN:VEVENT\r\n"
                f"UID:{uid[:50]}\r\n {uid[50:]}\r\n"
                "DTSTART:20260901T140000Z\r\n"
                "END:VEVENT\r\n"
            )}}],
        )
        assert gmail_live._classify_message(OWN_EMAIL, message)["ics_uid"] == uid

    def test_an_invite_with_no_uid_still_yields_its_time(self):
        """Not every sender's .ics survives the parse. A missing UID costs
        the reschedule key, not the event."""
        message = _message(
            {"From": "Alice <alice@firm.com>", "To": OWN_EMAIL, "Subject": "Invite"},
            parts=[{"mimeType": "text/calendar", "body": {"data": _b64(self.ICS_TEXT)}}],
        )
        finding = gmail_live._classify_message(OWN_EMAIL, message)
        assert finding["ics_uid"] is None
        assert finding["chat_scheduled_at"] == "2026-09-01T14:00:00+00:00"


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
    def test_false_when_any_of_the_three_is_missing(self, settings):
        settings.GMAIL_LIVE_CLIENT_ID = ""
        settings.GMAIL_LIVE_CLIENT_SECRET = "x"
        settings.GMAIL_LIVE_TOKEN_KEY = "x"
        assert gmail_live.is_configured() is False

    def test_true_when_client_id_secret_and_token_key_are_set(self, settings):
        settings.GMAIL_LIVE_CLIENT_ID = "id"
        settings.GMAIL_LIVE_CLIENT_SECRET = "secret"
        settings.GMAIL_LIVE_TOKEN_KEY = "key"
        assert gmail_live.is_configured() is True

    def test_true_with_no_pubsub_topic_at_all(self, settings):
        """The fix this pins (2026-08-27): connecting a mailbox and syncing
        mail never needed Pub/Sub — `connect_gmail` and `sync_connection`
        build their Gmail client straight from the stored OAuth refresh
        token. `is_configured()` used to demand `GMAIL_LIVE_PUBSUB_TOPIC`
        anyway, so a topicless deployment (real-time push never set up)
        could not even show a connect button or run `gmail_poll`, though
        neither touches Pub/Sub. See `TestIsPushConfigured` for the gate
        that DOES need the topic."""
        settings.GMAIL_LIVE_CLIENT_ID = "id"
        settings.GMAIL_LIVE_CLIENT_SECRET = "secret"
        settings.GMAIL_LIVE_TOKEN_KEY = "key"
        settings.GMAIL_LIVE_PUBSUB_TOPIC = ""
        assert gmail_live.is_configured() is True


class TestIsPushConfigured:
    """Real-time push's own, stricter gate — `register_watch`,
    `renew_watches`, and `gmail_pubsub_listen` hold themselves to this
    instead of the base `is_configured()`."""

    def test_false_without_a_topic(self, settings):
        settings.GMAIL_LIVE_CLIENT_ID = "id"
        settings.GMAIL_LIVE_CLIENT_SECRET = "secret"
        settings.GMAIL_LIVE_TOKEN_KEY = "key"
        settings.GMAIL_LIVE_PUBSUB_TOPIC = ""
        assert gmail_live.is_push_configured() is False

    def test_false_when_the_base_config_is_missing_even_with_a_topic(self, settings):
        settings.GMAIL_LIVE_CLIENT_ID = ""
        settings.GMAIL_LIVE_CLIENT_SECRET = "secret"
        settings.GMAIL_LIVE_TOKEN_KEY = "key"
        settings.GMAIL_LIVE_PUBSUB_TOPIC = "projects/p/topics/t"
        assert gmail_live.is_push_configured() is False

    def test_true_when_all_four_are_set(self, settings):
        settings.GMAIL_LIVE_CLIENT_ID = "id"
        settings.GMAIL_LIVE_CLIENT_SECRET = "secret"
        settings.GMAIL_LIVE_TOKEN_KEY = "key"
        settings.GMAIL_LIVE_PUBSUB_TOPIC = "projects/p/topics/t"
        assert gmail_live.is_push_configured() is True


class TestFlowHasPkceDisabled:
    """Regression test for a real production outage (2026-08-19): Google
    rejected every single token exchange with "invalid_grant: Missing code
    verifier". `build_auth_url` and `connect_gmail` each build a fresh
    `Flow` from a separate HTTP request — nothing on one instance survives
    to the other — so if `Flow` auto-generates its own PKCE code_verifier
    per instance (google-auth-oauthlib's default), the verifier the
    callback's exchange sends never matches the code_challenge the connect
    step already gave Google. See `_flow`'s docstring for the full story."""

    def test_flow_does_not_autogenerate_a_pkce_verifier(self, settings):
        settings.GMAIL_LIVE_CLIENT_ID = "id"
        settings.GMAIL_LIVE_CLIENT_SECRET = "secret"
        flow = gmail_live._flow("https://example.com/capture/gmail/callback/")
        assert flow.autogenerate_code_verifier is False
        assert flow.code_verifier is None

    def test_build_auth_url_sends_no_code_challenge(self, settings):
        settings.GMAIL_LIVE_CLIENT_ID = "id"
        settings.GMAIL_LIVE_CLIENT_SECRET = "secret"
        auth_url = gmail_live.build_auth_url(
            "https://example.com/capture/gmail/callback/", state="s"
        )
        assert "code_challenge" not in auth_url


class TestMessageOccurredAt:
    """`internalDate` -> `occurred_at` on every finding — the backfill
    command's whole reason to exist is applying findings whose real time
    isn't "now", so a message with no parseable date must not crash the
    classifier, just omit the field."""

    def test_parses_internal_date_to_iso(self):
        # 2026-09-01T14:00:00Z in epoch milliseconds.
        message = {"internalDate": "1788271200000"}
        assert gmail_live._message_occurred_at(message) == "2026-09-01T14:00:00+00:00"

    def test_missing_internal_date_is_none(self):
        assert gmail_live._message_occurred_at({}) is None

    def test_garbled_internal_date_is_none(self):
        assert gmail_live._message_occurred_at({"internalDate": "not-a-number"}) is None

    def test_classify_message_carries_occurred_at_through(self):
        message = _message(
            {"From": "Alice <alice@firm.com>", "To": OWN_EMAIL, "Subject": "Re: hi"},
            snippet="a reply",
        )
        message["internalDate"] = "1788271200000"
        finding = gmail_live._classify_message(OWN_EMAIL, message)
        assert finding["occurred_at"] == "2026-09-01T14:00:00+00:00"


NOW = datetime(2026, 9, 1, tzinfo=dt_timezone.utc)


class TestBackfillWindowStart:
    def test_zero_touch_contact_gets_365_days(self):
        start = gmail_live._backfill_window_start(None, now=NOW)
        assert start == NOW - timedelta(days=365)

    def test_recently_touched_contact_gets_last_touch_minus_overlap(self):
        last_touch = NOW - timedelta(days=10)
        start = gmail_live._backfill_window_start(last_touch, now=NOW)
        assert start == last_touch - timedelta(days=7)

    def test_a_very_old_last_touch_is_capped_at_90_days(self):
        last_touch = NOW - timedelta(days=400)
        start = gmail_live._backfill_window_start(last_touch, now=NOW)
        assert start == NOW - timedelta(days=90)


class TestSuppressStaleBounces:
    def test_a_bounce_before_a_later_reply_is_dropped(self):
        findings = [
            {"email": "a@b.com", "bounced": True, "occurred_at": "2026-03-01T00:00:00+00:00"},
            {"email": "a@b.com", "replied": True, "occurred_at": "2026-06-01T00:00:00+00:00"},
        ]
        kept = gmail_live._suppress_stale_bounces(findings)
        assert all(not f.get("bounced") for f in kept)
        assert len(kept) == 1

    def test_a_bounce_with_no_later_reply_is_kept(self):
        findings = [
            {"email": "a@b.com", "bounced": True, "occurred_at": "2026-06-01T00:00:00+00:00"},
        ]
        kept = gmail_live._suppress_stale_bounces(findings)
        assert len(kept) == 1
        assert kept[0]["bounced"] is True

    def test_a_bounce_after_the_reply_is_kept_not_the_reply_thats_stale(self):
        """A later bounce (address stopped working after they'd replied
        once) is real evidence too — only a bounce that PRECEDES proof of
        delivery is the stale one."""
        findings = [
            {"email": "a@b.com", "replied": True, "occurred_at": "2026-03-01T00:00:00+00:00"},
            {"email": "a@b.com", "bounced": True, "occurred_at": "2026-06-01T00:00:00+00:00"},
        ]
        kept = gmail_live._suppress_stale_bounces(findings)
        assert any(f.get("bounced") for f in kept)

    def test_different_contacts_do_not_cross_suppress(self):
        findings = [
            {"email": "a@b.com", "bounced": True, "occurred_at": "2026-03-01T00:00:00+00:00"},
            {"email": "c@d.com", "replied": True, "occurred_at": "2026-06-01T00:00:00+00:00"},
        ]
        kept = gmail_live._suppress_stale_bounces(findings)
        assert any(f.get("bounced") for f in kept)
