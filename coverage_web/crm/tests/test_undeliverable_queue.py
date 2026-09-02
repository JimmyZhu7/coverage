"""The queue stops asking for email to an address the mail system rejected.

THE DEFECT, in the founder's own words: "some emails bounce back but Coverage
still asks me to follow up."

Measured read-only on his live account, 2026-09-02. Five live contacts carry
no email address at all: four were blanked by `capture.gmail.apply_findings`'
hard-bounce block on Aug 30, one by a `departed` auto-reply. Feeding the
departed one to `cadence.due_actions` on its own returns, that day,
`follow_up` — "no reply 7 business days after touch 1 — follow up" — for a
person with no address to follow up AT. The engine is not wrong about the
timing; the card is unworkable, and the click it offers goes nowhere.

WHAT THESE TESTS PIN

  1. A hard-bounced contact is never asked for another email. The card is
     MARKED, not dropped (P4): it stays in the queue, says the address is
     dead, and offers the two things that help — find a new address, or park
     them. `touch_kind` is withheld so the "Log it" button, which would
     record an outreach that cannot have happened, is not drawn.
  2. A SOFT bounce is not this and never becomes this. A full mailbox is a
     deferral, the address works, and the follow-up it earns is a real
     follow-up. The whole value of `capture.gmail_live`'s soft/hard split is
     that it stops working addresses being cleared; a deliverability rule
     that ignored it would undo the split from the other end.
  3. A `departed` auto-reply is the same fact by a different route, and is
     treated the same.
  4. The rule reads the ADDRESS, not just the person. A student who types a
     new address has a reachable contact again, with no undo needed.
  5. Undo puts the address back in the queue's eyes; Dismiss does not.
     Dismissing a card is "I have seen this", not "the mail started
     arriving" — the same asymmetry `mailfacts.address_is_departed` makes.

Both bounce shapes are driven through the real chain — a Gmail-API-shaped
message dict through `gmail_live._classify_message`, its finding through
`capture.gmail.apply_findings` — for the same reason `test_mailfacts.py` does
it: the soft/hard classification is the thing under test, and a hand-built
`MailFact` would assert the fixture rather than the pipeline.
"""

from __future__ import annotations

import base64
from datetime import timedelta

import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse
from django.utils import timezone

from capture import mailfacts
from capture.gmail import apply_findings
from capture.gmail_live import _classify_message
from capture.models import MailFact
from crm.models import Contact, Touch
from crm.today import UNDELIVERABLE_LABEL, _build_actions, _cockpit_context

pytestmark = pytest.mark.django_db(transaction=True)

OWN = "queue-student@example.com"


def _user():
    return get_user_model().objects.create_user(email=OWN, password="pw12345!")


def _due_contact(user, name, email):
    """A cold contact with one unanswered outreach: cadence branch 6's
    `follow_up`, the exact card the founder was complaining about.

    `school_affiliation=True` for the reason `test_today.py`'s own `_contact`
    gives — it is the cheapest way past `crm.relevance`'s gate (one boolean,
    no Firm and no UserFirm row), and nothing here is about that gate.
    10 days is the same weekday-proof "a follow-up is due" offset that file
    uses: comfortably past the window, comfortably inside the follow-up's
    15-business-day shelf life, so the fixture cannot drift into a `park`
    depending on which day the suite runs."""
    contact = Contact.all_objects.create(
        user=user, name=name, email=email, warmth="cold", thread_state="no_reply",
        school_affiliation=True,
    )
    Touch.all_objects.create(
        user=user, contact=contact, kind="outreach", channel="email",
        ts=timezone.now() - timedelta(days=10),
    )
    return contact


def _b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode()


def _message(*, from_header, subject, body, thread_id, headers=()):
    return {
        "threadId": thread_id,
        "internalDate": "1787584244000",
        "snippet": body[:180],
        "payload": {
            "headers": [
                {"name": "From", "value": from_header},
                {"name": "To", "value": f"Student <{OWN}>"},
                {"name": "Subject", "value": subject},
                *[{"name": n, "value": v} for n, v in headers],
            ],
            "mimeType": "text/plain",
            "body": {"data": _b64(body)},
        },
    }


def hard_bounce(address, thread_id="t-hard"):
    """A permanent failure: sendmail's transcript shape, 5.1.1 user unknown."""
    return _message(
        from_header="mailerdaemon@mx.example (Mail Delivery Subsystem)",
        subject="Returned mail: see transcript for details",
        body=(
            "The following addresses had permanent fatal errors:\n"
            f"<{address}>\n550 5.1.1 User unknown\n"
        ),
        thread_id=thread_id,
    )


def soft_bounce(address, thread_id="t-soft"):
    """A DEFERRAL, verbatim in shape from Goldman's postmaster answering the
    founder's note to a real banker: 5.2.2, mailbox full, try later. The
    address works."""
    return _message(
        from_header="postmaster@mx.example",
        subject="Undeliverable: coffee chat",
        body=(
            "Delivery has failed to these recipients or groups:\n"
            f"{address}\n"
            "Status: 5.2.2\n"
            "The recipient's mailbox is full and can't accept messages now. "
            "Please try resending your message later.\n"
        ),
        thread_id=thread_id,
    )


def departed_reply(address, thread_id="t-gone"):
    """The Allen & Company auto-reply that started this whole module. Gated
    in as an auto-reply by the RFC 3834 header, then typed as `departed` by
    the phrase layer, which clears the address."""
    return _message(
        from_header=f"Somil Agarwal <{address}>",
        subject="Automatic reply: coffee chat",
        body=(
            "Somil Agarwal is no longer with Allen & Company. For matters "
            "with which Somil was involved, please contact Salima "
            "Vahabzadeh at salima@allenco.example."
        ),
        thread_id=thread_id,
        headers=(("Auto-Submitted", "auto-replied"),),
    )


def _sync(user, message):
    apply_findings(user, [_classify_message(OWN, message)])


def _action_for(actions, contact):
    return next((a for a in actions if a["contact"]["id"] == contact.id), None)


def _card(user, contact):
    """The queue card for one contact, straight off `_build_actions`."""
    return _action_for(_build_actions(user)[0], contact)


def _rendered_card(user, contact):
    """The same card as the cockpit hands the template, which is where
    `touch_kind` and `snoozable` are set. Flattened out of the lanes the same
    way `test_today.py` does it."""
    ctx = _cockpit_context(user)
    return _action_for(
        [a for lane in ctx["lanes"] for a in lane["items"]], contact
    )


# ---------------------------------------------------------------------------
# 1. The defect itself
# ---------------------------------------------------------------------------
def test_hard_bounced_contact_is_not_asked_to_follow_up():
    user = _user()
    contact = _due_contact(user, "Lidia M", "lidia@wellsfargo.example")

    # Before: the engine wants a follow-up, and the queue renders one.
    before = _card(user, contact)
    assert before is not None and before["action"] == "follow_up"
    assert before["label"] == "Follow up"
    assert not before.get("undeliverable")

    _sync(user, hard_bounce("lidia@wellsfargo.example"))
    contact.refresh_from_db()
    assert contact.email == ""

    after = _card(user, contact)
    # MARKED, NEVER DROPPED (P4). The card is still here.
    assert after is not None
    assert after["undeliverable"] is True
    assert after["label"] == UNDELIVERABLE_LABEL
    assert "lidia@wellsfargo.example" in after["reason"]
    assert "bounced" in after["reason"]
    # And it offers the two things that actually help.
    assert "Find a new address" in after["reason"]
    assert "park them" in after["reason"]
    # Nothing to compose to, and no draft badge claiming otherwise.
    assert after["mailto"] == ""
    assert after["has_draft"] is False


def test_the_card_draws_no_send_button():
    """`touch_kind` is what `_act_card.html` gates "Log it" on. Logging an
    outreach for this card would record a send that cannot have happened."""
    user = _user()
    contact = _due_contact(user, "Lidia M", "lidia@wellsfargo.example")
    _sync(user, hard_bounce("lidia@wellsfargo.example"))

    card = _rendered_card(user, contact)
    assert card is not None
    assert card["touch_kind"] is None
    # Park stays available: it is the other honest answer to a dead address.
    assert card["snoozable"] is True


# ---------------------------------------------------------------------------
# 2. THE DISTINCTION. A soft bounce is not a dead address.
# ---------------------------------------------------------------------------
def test_soft_bounced_contact_still_gets_an_ordinary_follow_up():
    """A full mailbox means the message did not land TODAY, not that the
    address is wrong. `capture.gmail_live` refuses to clear it; the queue
    must go on asking for the follow-up it earns. If this test ever passes
    for the wrong reason, the soft/hard split has collapsed and working
    addresses are being thrown away."""
    user = _user()
    contact = _due_contact(user, "Noah Bauld", "noah.bauld@gs.example")

    _sync(user, soft_bounce("noah.bauld@gs.example"))

    contact.refresh_from_db()
    assert contact.email == "noah.bauld@gs.example"      # kept
    assert not MailFact.objects.for_user(user).filter(
        kind=MailFact.KIND_BOUNCED
    ).exists()
    assert contact.id not in mailfacts.dead_addresses(user)

    card = _card(user, contact)
    assert card is not None
    assert card["action"] == "follow_up"
    assert card["label"] == "Follow up"
    assert not card.get("undeliverable")
    assert card["mailto"]                                 # still composable
    # And the send button is still drawn: this really is a follow-up to make.
    assert _rendered_card(user, contact)["touch_kind"] == "follow_up"


def test_one_dead_address_does_not_silence_a_working_one():
    """Two contacts, one bounce. The bounce is a fact about one string, and
    the other person's follow-up is untouched."""
    user = _user()
    dead = _due_contact(user, "Lidia M", "lidia@wellsfargo.example")
    live = _due_contact(user, "Noah Bauld", "noah.bauld@gs.example")

    _sync(user, hard_bounce("lidia@wellsfargo.example"))

    actions = _build_actions(user)[0]
    assert _action_for(actions, dead)["undeliverable"] is True
    assert not _action_for(actions, live).get("undeliverable")
    assert _action_for(actions, live)["action"] == "follow_up"


# ---------------------------------------------------------------------------
# 3. A departure is the same fact by another route
# ---------------------------------------------------------------------------
def test_departed_contact_is_not_asked_to_follow_up():
    user = _user()
    contact = _due_contact(user, "Somil Agarwal", "sagarwal@allenco.example")

    _sync(user, departed_reply("sagarwal@allenco.example"))
    contact.refresh_from_db()
    assert contact.email == ""

    card = _card(user, contact)
    assert card is not None
    assert card["undeliverable"] is True
    assert card["undeliverable_kind"] == MailFact.KIND_DEPARTED
    assert "left the firm" in card["reason"]


# ---------------------------------------------------------------------------
# 4. The rule reads the address, not just the person
# ---------------------------------------------------------------------------
def test_a_new_address_makes_the_contact_reachable_again():
    """No undo required. The fact names the address that DIED; a different
    address is not that address, so the card goes back to being an ordinary
    follow-up on its own."""
    user = _user()
    contact = _due_contact(user, "Lidia M", "lidia@wellsfargo.example")
    _sync(user, hard_bounce("lidia@wellsfargo.example"))
    assert _card(user, contact)["undeliverable"]

    contact.email = "lidia.m@wellsfargo.example"
    contact.save(update_fields=["email"])

    card = _card(user, contact)
    assert not card.get("undeliverable")
    assert card["label"] == "Follow up"


def test_undo_restores_the_follow_up_but_dismiss_does_not():
    user = _user()
    contact = _due_contact(user, "Lidia M", "lidia@wellsfargo.example")
    _sync(user, hard_bounce("lidia@wellsfargo.example"))
    fact = MailFact.objects.for_user(user).get(kind=MailFact.KIND_BOUNCED)

    mailfacts.dismiss(fact)
    assert _card(user, contact)["undeliverable"]

    fact.status = MailFact.STATUS_UNDONE
    fact.save(update_fields=["status"])
    contact.email = "lidia@wellsfargo.example"
    contact.save(update_fields=["email"])
    assert not _card(user, contact).get("undeliverable")


# ---------------------------------------------------------------------------
# 5. The card says nothing that contradicts itself
# ---------------------------------------------------------------------------
def test_an_undeliverable_card_is_never_also_firm_paced():
    """`_pace_by_firm` skips it: it puts no email in anyone's inbox, exactly
    like a `park`. Appending "already has 2 today, so this one is better
    tomorrow" to a card whose message is that the address is dead would be
    two sentences arguing with each other."""
    user = _user()
    contact = _due_contact(user, "Lidia M", "lidia@wellsfargo.example")
    _sync(user, hard_bounce("lidia@wellsfargo.example"))

    card = _card(user, contact)
    assert card["firm_paced"] is False
    assert card["pace_note"] == ""


# ---------------------------------------------------------------------------
# 6. The contact's own page says it
# ---------------------------------------------------------------------------
def test_contact_page_says_the_address_is_dead(client):
    user = _user()
    contact = _due_contact(user, "Lidia M", "lidia@wellsfargo.example")
    _sync(user, hard_bounce("lidia@wellsfargo.example"))

    client.force_login(user)
    body = client.get(
        reverse("crm:contact_detail", args=[contact.pk])
    ).content.decode()

    assert "Mail to them bounced." in body
    assert "lidia@wellsfargo.example" in body
    # The old line, which read as the student's own omission, is gone for
    # this contact.
    assert "No email on file." not in body


def test_contact_page_still_says_no_email_when_there_never_was_one(client):
    user = _user()
    contact = Contact.all_objects.create(user=user, name="Never Had One")

    client.force_login(user)
    body = client.get(
        reverse("crm:contact_detail", args=[contact.pk])
    ).content.decode()

    assert "No email on file." in body
    assert "is dead" not in body
