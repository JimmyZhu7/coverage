"""Every Gmail sync now banks firm-level evidence about address formats.

WHY THIS EXISTS. The old single-user system kept a per-firm delivered/bounced
ledger and used it to rate how much to trust a guessed address — 18 delivered
and 0 bounced at Morgan Stanley means `first.last@` works there; 0 delivered
and 2 bounced at Mizuho means the guess is wrong. Coverage had the table
(`EmailPatternStats`) and the admin page, but nothing ever wrote to it, so the
ledger imported from that system would have frozen the moment it arrived.

The counts are deliberately on the SHARED, firm-keyed table with no user
column: build-plan §2 splits it that way so the aggregate helps every user
while the raw evidence (who was emailed, and when) stays in that user's own
private Touch rows.
"""

from __future__ import annotations

import pytest

from capture.gmail import apply_findings
from crm.models import Contact
from directory.models import EmailPatternStats, Firm

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def user(django_user_model):
    return django_user_model.objects.create_user(
        email="jimmy@example.com", password="x"
    )


@pytest.fixture
def firm():
    return Firm.objects.create(slug="gs", name="Goldman Sachs")


@pytest.fixture
def contact(user, firm):
    return Contact.all_objects.create(
        user=user, name="Ada Lovelace", firm=firm, email="ada@gs.com",
    )


def _finding(**kw):
    base = {"name": "Ada Lovelace", "found": True, "email": "ada@gs.com",
            "thread_id": "t1"}
    base.update(kw)
    return base


def test_a_reply_banks_a_delivered(user, contact, firm):
    """A reply is the strongest proof the format works — the person received
    the mail and answered it."""
    result = apply_findings(user, [_finding(replied=True)])
    stats = EmailPatternStats.objects.get(firm=firm)
    assert stats.delivered == 1
    assert stats.bounced == 0
    assert result.pattern_delivered == 1


def test_a_bounce_banks_a_bounced(user, contact, firm):
    result = apply_findings(user, [_finding(bounced=True)])
    stats = EmailPatternStats.objects.get(firm=firm)
    assert stats.bounced == 1
    assert stats.delivered == 0
    assert result.pattern_bounced == 1


def test_the_bounce_is_banked_before_the_address_is_cleared(user, contact, firm):
    """Order matters: clearing the address destroys the only record of what
    was tried, so the evidence has to be taken first."""
    apply_findings(user, [_finding(bounced=True)])
    contact.refresh_from_db()
    assert contact.email == "", "the bounce handler still clears the address"
    assert EmailPatternStats.objects.get(firm=firm).bounced == 1


def test_evidence_is_banked_once_per_contact_ever(user, contact, firm):
    """The guard that makes this safe to run twice a day. A thread stays in
    the search window for days, so without `email_pattern_recorded` one real
    send would inflate a firm's confidence every run until it aged out."""
    for _ in range(4):
        apply_findings(user, [_finding(replied=True)])
    assert EmailPatternStats.objects.get(firm=firm).delivered == 1


def test_outreach_alone_is_not_proof_of_delivery(user, contact, firm):
    """`outreach_sent` means the message left, not that it landed. Only a
    reply or a chat proves an address actually works."""
    apply_findings(user, [_finding(outreach_sent=True)])
    assert not EmailPatternStats.objects.filter(firm=firm).exists()


def test_a_scheduled_chat_counts_as_delivered(user, contact, firm):
    """A chat that names its time is a chat, and a chat proves the address
    works as surely as a reply does."""
    apply_findings(user, [_finding(
        chat_status="scheduled", chat_scheduled_at="2026-09-10T12:00:00+00:00",
    )])
    assert EmailPatternStats.objects.get(firm=firm).delivered == 1


def test_an_uncorroborated_chat_claim_banks_nothing(user, contact, firm):
    """`email_pattern_recorded` is a one-shot per-contact flag, so banking a
    "delivered" off a chat claim with no time behind it is worse than merely
    wrong: it also spends the contact's one chance to bank the real evidence
    later. Same corroboration rule the ladder uses — see
    `capture.providers.corroborated_chat_status` for the two live failures
    that set it.

    The finding is not thrown away. It still floors at what the message
    itself proves, and a reply banks the evidence on its own — which is why
    this case carries no `replied` flag: the chat claim alone must not be
    the thing that speaks."""
    result = apply_findings(user, [_finding(chat_status="completed")])
    contact.refresh_from_db()
    assert result.pattern_delivered == 0
    assert contact.email_pattern_recorded is False
    assert not EmailPatternStats.objects.filter(firm=firm).exists()


def test_a_contact_with_no_firm_says_nothing_about_any_firm(user):
    """Firm-keyed evidence needs a firm. A contact with only free-text firm
    has no row to bank against, and guessing one would corrupt shared data."""
    Contact.all_objects.create(user=user, name="Ada Lovelace", firm_text="Somewhere")
    result = apply_findings(user, [_finding(replied=True)])
    assert EmailPatternStats.objects.count() == 0
    assert result.pattern_delivered == 0


def test_dry_run_banks_nothing(user, contact, firm):
    result = apply_findings(user, [_finding(replied=True)], dry_run=True)
    assert EmailPatternStats.objects.count() == 0
    # …but still reports what it would have banked.
    assert result.pattern_delivered == 1
    contact.refresh_from_db()
    assert contact.email_pattern_recorded is False


def test_two_users_at_the_same_firm_both_contribute(django_user_model, firm):
    """The whole point of the shared table: every user's evidence improves
    the confidence everyone reads."""
    a = django_user_model.objects.create_user(email="a@x.com", password="x")
    b = django_user_model.objects.create_user(email="b@x.com", password="x")
    Contact.all_objects.create(user=a, name="Ada Lovelace", firm=firm, email="ada@gs.com")
    Contact.all_objects.create(user=b, name="Grace Hopper", firm=firm, email="grace@gs.com")

    apply_findings(a, [_finding(replied=True)])
    apply_findings(b, [_finding(name="Grace Hopper", found=True,
                                email="grace@gs.com", thread_id="t2", replied=True)])

    assert EmailPatternStats.objects.get(firm=firm).delivered == 2
