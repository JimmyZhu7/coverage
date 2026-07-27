"""Shared fixtures + a Postmark-style inbound-email payload builder.

No live Postmark: every test feeds a fixture JSON dict shaped like Postmark's
inbound webhook (``FromFull``/``ToFull``/``BccFull``, ``Headers`` with a
Message-ID, ``Attachments`` with base64 ``Content``, ``OriginalRecipient`` for
the delivered-to capture address).
"""

from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

import pytest
from django.contrib.auth import get_user_model

from capture import services
from crm.models import Contact

User = get_user_model()


@pytest.fixture
def user(db):
    # capture_slug is auto-assigned in User.save(); pin it for stable routing.
    return User.objects.create_user(
        email="student@example.com", password="x", capture_slug="testslug123"
    )


@pytest.fixture
def capture_addr(user):
    return services.capture_address(user)  # u-testslug123@in.coverage.app


@pytest.fixture
def known_contact(user):
    return Contact.all_objects.create(
        user=user, name="Jane Banker", email="jane@bank.example", source="manual"
    )


def _ics(dtstart: datetime) -> str:
    stamp = dtstart.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return (
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VEVENT\r\n"
        f"DTSTART:{stamp}\r\nSUMMARY:Coffee chat\r\n"
        "STATUS:CONFIRMED\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
    )


@pytest.fixture
def ics_builder():
    return _ics


def _build_payload(
    *,
    from_email: str,
    from_name: str = "",
    to: list[tuple[str, str]] | None = None,
    bcc: list[tuple[str, str]] | None = None,
    capture_addr: str = "",
    subject: str = "Following up",
    text: str = "Hi there, following up on our chat.",
    message_id: str = "<msg-001@mail.example>",
    # Relative to now, NOT a literal. This header used to read
    # "Wed, 23 Jul 2026 10:00:00 -0400" while the `past_dt` / `future_dt`
    # fixtures are computed from the real clock. The two agreed only while the
    # wall clock sat near that date: once it passed 2026-07-26, `past_dt`
    # (now - 3d) landed AFTER the frozen header, so an ICS meeting the test
    # calls "past" read as future and two capture tests began failing on a
    # clean tree for reasons unrelated to any change. A fixture that decays
    # with the calendar is worse than no fixture — it fails later, in someone
    # else's diff. Anchoring both sides to the same clock makes the relation
    # the test asserts (meeting before/after the email) permanently true.
    date: str | None = None,
    headers: list[tuple[str, str]] | None = None,
    ics_text: str | None = None,
) -> dict:
    to = to or []
    bcc = bcc or []
    if date is None:
        date = format_datetime(datetime.now(timezone.utc))

    # pairs come in as (email, name)
    to_full = [{"Email": e, "Name": n, "MailboxHash": ""} for e, n in to]
    bcc_full = [{"Email": e, "Name": n, "MailboxHash": ""} for e, n in bcc]

    hdrs = [{"Name": "Message-ID", "Value": message_id}, {"Name": "Date", "Value": date}]
    for name, value in headers or []:
        hdrs.append({"Name": name, "Value": value})

    attachments = []
    if ics_text is not None:
        attachments.append({
            "Name": "invite.ics",
            "ContentType": "text/calendar; method=REQUEST",
            "Content": base64.b64encode(ics_text.encode("utf-8")).decode("ascii"),
            "ContentLength": len(ics_text),
        })

    return {
        "FromFull": {"Email": from_email, "Name": from_name},
        "From": f"{from_name} <{from_email}>" if from_name else from_email,
        "ToFull": to_full,
        "To": ", ".join(e for e, _ in to),
        "CcFull": [],
        "Cc": "",
        "BccFull": bcc_full,
        "Bcc": ", ".join(e for e, _ in bcc),
        "OriginalRecipient": capture_addr,
        "Subject": subject,
        "MessageID": "postmark-inbound-uuid",
        "Date": date,
        "TextBody": text,
        "HtmlBody": f"<p>{text}</p>",
        "Headers": hdrs,
        "Attachments": attachments,
    }


@pytest.fixture
def make_payload():
    return _build_payload


@pytest.fixture
def now_utc():
    return datetime.now(timezone.utc)


@pytest.fixture
def future_dt():
    return datetime.now(timezone.utc) + timedelta(days=3)


@pytest.fixture
def past_dt():
    return datetime.now(timezone.utc) - timedelta(days=3)
