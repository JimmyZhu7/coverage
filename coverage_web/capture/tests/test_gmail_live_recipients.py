"""Two classification defects in capture/gmail_live.py, pinned together
because both are about WHO a message is really about:

- `_bounce_recipient` used to take the first non-own, non-mailer address
  found anywhere in snippet + body. A DSN that quotes the original message
  (a Cc, an address in a signature) BEFORE naming the failed recipient
  would hard-bounce-clear a working address off the wrong person — and
  clearing an address is destructive. The fix reads the DSN's own RFC 3464
  fields first, then failure-phrase-anchored addresses, and REFUSES an
  unanchored multi-address text instead of guessing. Both real DSN shapes
  on the founder's live mailbox (Proofpoint/sendmail "Returned mail" and
  Exchange "Undeliverable:", 2026-08-27 read-only) carry Final-Recipient
  and are pinned below in their real shapes.

- the outbound branch used `parseaddr` on To:, which silently keeps only
  the FIRST mailbox of a multi-recipient header — one email sent to three
  contacts logged outreach for one and lost the other two sends.
  `classify_message_findings` now yields one finding per distinct To:
  recipient.

Pure classification tests — no DB.
"""

from __future__ import annotations

import base64

from capture import gmail_live

OWN = "student@usc.example"


def _headers(**headers) -> list[dict]:
    return [{"name": k.replace("_", "-"), "value": v} for k, v in headers.items()]


def _b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode()


def _message(headers: dict, snippet: str = "", parts: list[dict] | None = None) -> dict:
    payload = {"headers": _headers(**headers)}
    if parts:
        payload["parts"] = parts
    return {"threadId": "t1", "snippet": snippet, "payload": payload,
            "internalDate": "1787584097000"}


def _text_part(text: str, mime: str = "text/plain") -> dict:
    return {"mimeType": mime, "body": {"data": _b64(text)}}


def _bounce_headers(subject: str = "Undeliverable: hello") -> dict:
    return {"From": "postmaster@firm.example", "Subject": subject}


# --------------------------------------------------------------------------- #
# _bounce_recipient: structured fields win
# --------------------------------------------------------------------------- #

def test_final_recipient_field_wins_over_an_earlier_quoted_address():
    """The founder's Exchange-shaped DSN quotes headers full of other
    addresses; the RFC 3464 block names the one that failed. The old
    first-address scan returned whatever came first."""
    body = (
        "Your message could not be processed.\n"
        "Quoted from the original: Cc: teammate@usc-club.example\n"
        "Final-Recipient: rfc822;jane.banker@firm.example\n"
        "Action: failed\nStatus: 5.1.1\n"
    )
    message = _message(_bounce_headers(), parts=[_text_part(body)])
    assert gmail_live._bounce_recipient(message, OWN) == "jane.banker@firm.example"


def test_sendmail_returned_mail_shape_is_parsed():
    """The real Proofpoint/sendmail shape, verbatim structure from the
    founder's Wells Fargo DSN (2026-08-24)."""
    body = (
        "The original message was received at Mon, 24 Aug 2026 08:08:32 -0700\n"
        "   ----- The following addresses had permanent fatal errors -----\n"
        "<lidia.motorozesku@wellsfargo.example>\n"
        "    (reason: 550 5.1.1 User Unknown)\n"
        "Final-Recipient: RFC822; lidia.motorozesku@wellsfargo.example\n"
        "Action: failed\nStatus: 5.1.1\n"
    )
    message = _message(
        {"From": "MAILER-DAEMON@mx.pphosted.example",
         "Subject": "Returned mail: see transcript for details"},
        parts=[_text_part(body)],
    )
    assert (
        gmail_live._bounce_recipient(message, OWN)
        == "lidia.motorozesku@wellsfargo.example"
    )


def test_delivery_status_part_is_read():
    """An Exchange DSN keeps its Final-Recipient block in a
    message/delivery-status part, whose mimeType does not start with
    text/ — it must still be scanned."""
    status = (
        "Original-Recipient: rfc822;jane.banker@firm.example\n"
        "Final-Recipient: rfc822;jane.banker@firm.example\n"
        "Action: failed\nStatus: 5.2.2\n"
    )
    message = _message(
        _bounce_headers(),
        parts=[_text_part(status, mime="message/delivery-status")],
    )
    assert gmail_live._bounce_recipient(message, OWN) == "jane.banker@firm.example"


def test_failure_phrase_anchors_the_recipient():
    """No RFC 3464 fields, but the human sentence names the failure — the
    address right after the phrase is the one, even with another address
    earlier in the text."""
    body = (
        "Report generated for thread with sarah.helper@other.example.\n"
        "Delivery has failed to these recipients or groups:\n"
        "jane.banker@firm.example\n"
        "The email address you entered couldn't be found.\n"
    )
    message = _message(_bounce_headers(), parts=[_text_part(body)])
    assert gmail_live._bounce_recipient(message, OWN) == "jane.banker@firm.example"


def test_ambiguous_multi_address_text_refuses():
    """Two candidate addresses, no structured field, no failure phrase:
    guessing risks clearing a working address off the wrong contact, so
    the only safe answer is none. The old code returned the FIRST —
    the quoted Cc, the wrong person."""
    body = (
        "Quoted original: Cc: teammate@usc-club.example\n"
        "Some unrecognised bounce wording about jane.banker@firm.example\n"
    )
    message = _message(_bounce_headers(), parts=[_text_part(body)])
    assert gmail_live._bounce_recipient(message, OWN) is None


def test_single_address_fallback_still_detects():
    """The pre-fix happy path — one address anywhere in the text — must
    keep working, or the fix costs real detections."""
    message = _message(
        _bounce_headers(),
        snippet="Message to jane.banker@firm.example bounced badly",
    )
    assert gmail_live._bounce_recipient(message, OWN) == "jane.banker@firm.example"


def test_own_and_mailer_addresses_are_never_candidates():
    body = (
        f"Final-Recipient: rfc822;{OWN}\n"
        "mailer-daemon@firm.example says hello\n"
    )
    message = _message(_bounce_headers(), parts=[_text_part(body)])
    assert gmail_live._bounce_recipient(message, OWN) is None


# --------------------------------------------------------------------------- #
# Outbound: every To: recipient, not just the first
# --------------------------------------------------------------------------- #

def test_every_to_recipient_gets_a_finding():
    message = _message({
        "From": f"Student <{OWN}>",
        "To": (
            "Jane Banker <jane@firm.example>, "
            "Ben Trader <ben@firm.example>, amy@firm.example"
        ),
        "Subject": "Coffee chat request",
    })
    findings = gmail_live.classify_message_findings(OWN, message)
    assert [f["email"] for f in findings] == [
        "jane@firm.example", "ben@firm.example", "amy@firm.example",
    ]
    assert all(f["outreach_sent"] for f in findings)
    assert {f["thread_id"] for f in findings} == {"t1"}
    assert findings[0]["name"] == "Jane Banker"


def test_duplicate_and_own_recipients_are_dropped():
    message = _message({
        "From": OWN,
        "To": f"jane@firm.example, JANE@firm.example, {OWN}",
        "Subject": "Hi",
    })
    findings = gmail_live.classify_message_findings(OWN, message)
    assert [f["email"] for f in findings] == ["jane@firm.example"]


def test_single_recipient_classification_is_unchanged():
    message = _message({
        "From": OWN, "To": "Jane Banker <jane@firm.example>",
        "Subject": "Hi",
    })
    single = gmail_live._classify_message(OWN, message)
    [multi] = gmail_live.classify_message_findings(OWN, message)
    assert single == multi
    assert single["email"] == "jane@firm.example"


def test_inbound_messages_pass_through_unchanged():
    message = _message({
        "From": "Jane Banker <jane@firm.example>", "To": OWN,
        "Subject": "Re: Coffee chat",
    })
    findings = gmail_live.classify_message_findings(OWN, message)
    assert len(findings) == 1
    assert findings[0]["email"] == "jane@firm.example"
    assert findings[0]["replied"] is True
