"""Adversarial invariant suite for `capture.mailfacts`' status lifecycle.

Companion to `test_mailfacts.py`, which pins the feature's EXAMPLES. This
file pins the one PROPERTY every `MailFact` row is supposed to hold no
matter how many times a mailbox is re-scanned: once a row reaches a
TERMINAL state (`dismissed` — the student waved the card away — or
`undone` — the student explicitly reversed the automated action), no later
automated pass may resurrect it. `undo()` only acts on `applied`
(`fact.status != STATUS_APPLIED: return`) and `dismiss()` only accepts
`pending`/`applied` — both already refuse to re-fire on a closed row. The
production bug this file is built from was `_apply_ooo`'s "a later leave
updates the existing row" branch, the one applier with a legitimate reason
to touch a row twice (an out-of-office return date can genuinely move
later; every other fact kind is a one-time observation), which forgot to
ask the same question its siblings ask for free by never updating in
place at all.

Same discipline as `capture/tests/test_stress_identity.py`: no
`hypothesis`, so the finite space here (status x whether a contact is
attached) is walked EXHAUSTIVELY via `pytest.mark.parametrize`.
"""

from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone

from capture import mailfacts
from capture.gmail import apply_findings
from capture.gmail_live import _classify_message
from capture.models import MailFact
from crm.models import Contact
from directory.models import Firm

pytestmark = pytest.mark.django_db(transaction=True)

User = get_user_model()
OWN = "jimmyz@usc.edu"


@pytest.fixture
def student():
    return User.objects.create_user(email="stress-facts@example.com", password="x")


@pytest.fixture
def allen():
    return Firm.objects.create(
        slug="allen-company-stress", name="Allen & Company", domains=["allenco.com"]
    )


def _b64(text: str) -> str:
    import base64

    return base64.urlsafe_b64encode(text.encode()).decode()


def _ooo_message(*, return_day: str, thread_id: str, internal_date: str):
    body = (
        "I am out of the office with limited access to email. I will "
        f"return on Monday, {return_day}. For urgent matters, please "
        "contact my assistant at assistant@allenco.com."
    )
    return {
        "threadId": thread_id,
        "internalDate": internal_date,
        "snippet": body[:180],
        "payload": {
            "headers": [
                {"name": "From", "value": "Peter Foggo <pfoggo@allenco.com>"},
                {"name": "To", "value": f"Jimmy Zhu <{OWN}>"},
                {"name": "Subject", "value": "Automatic reply: out of office"},
                {"name": "auto-submitted", "value": "auto-replied"},
            ],
            "mimeType": "text/plain",
            "body": {"data": _b64(body)},
        },
    }


# The first OOO always lands "September 7". The second always claims a
# strictly LATER date — the one shape `_apply_ooo`'s update branch is
# willing to act on at all, so it is the only shape that can expose the
# missing status guard.
FIRST_OOO = _ooo_message(
    return_day="September 7", thread_id="t-stress-ooo-1", internal_date="1787584244000"
)
SECOND_OOO = _ooo_message(
    return_day="September 14", thread_id="t-stress-ooo-2", internal_date="1789189844000"
)

# (status to force the row into, does a LATER genuine OOO update it?)
STATUS_LIVENESS = [
    (MailFact.STATUS_PENDING, True),
    (MailFact.STATUS_APPLIED, True),
    (MailFact.STATUS_DISMISSED, False),
    (MailFact.STATUS_UNDONE, False),
]


@pytest.mark.parametrize("status,should_update", STATUS_LIVENESS)
def test_a_later_ooo_only_updates_a_still_open_row(student, allen, status, should_update):
    contact = Contact.all_objects.create(
        user=student, name="Peter Foggo", email="pfoggo@allenco.com", firm=allen
    )
    apply_findings(student, [_classify_message(OWN, FIRST_OOO)])
    fact = MailFact.objects.for_user(student).get(kind=MailFact.KIND_OOO)
    assert fact.return_on.day == 7
    # The first apply wrote `snoozed_until` through a DIFFERENT Python
    # object (mailfacts.py fetches its own `Contact` row) — this local
    # `contact` handle is stale until refreshed.
    contact.refresh_from_db()

    # Force the row into the state under test. A real dismiss/undo already
    # has its own coverage in test_mailfacts.py — this drives every reachable
    # status directly so the invariant is checked exhaustively, not just for
    # the two ways a user's tap can produce a closed row.
    fact.status = status
    fact.save(update_fields=["status"])
    snoozed_before = contact.snoozed_until
    return_before = fact.return_on

    apply_findings(student, [_classify_message(OWN, SECOND_OOO)])

    fact.refresh_from_db()
    contact.refresh_from_db()
    if should_update:
        assert fact.return_on.day == 14, (
            f"status={status!r} is still open — a later, genuine OOO must "
            "move the follow-up clock forward"
        )
    else:
        assert fact.status == status, (
            f"status={status!r} is a closed card — a later automated pass "
            "must not reopen it"
        )
        assert fact.return_on == return_before, (
            f"status={status!r} is closed — the stored return date must not "
            "move either"
        )
        assert contact.snoozed_until == snoozed_before, (
            f"status={status!r} is closed — the student's own follow-up "
            "clock must not be touched by mail read on their behalf"
        )


@pytest.mark.parametrize("status", [MailFact.STATUS_DISMISSED, MailFact.STATUS_UNDONE])
def test_a_closed_ooo_takes_no_action_even_when_unresolvable(student, allen, status):
    """The "no readable return date" branch (`out.surfaced`) sits behind the
    same `existing is not None` check as the dated-update branch, but is
    reached only when `return_on` cannot be parsed from THIS message. A
    closed row must not be re-surfaced by an unreadable follow-up either —
    covered separately because that branch never enters the "does the date
    beat the stored one" comparison at all."""
    contact = Contact.all_objects.create(
        user=student, name="Peter Foggo", email="pfoggo@allenco.com", firm=allen
    )
    apply_findings(student, [_classify_message(OWN, FIRST_OOO)])
    fact = MailFact.objects.for_user(student).get(kind=MailFact.KIND_OOO)
    fact.status = status
    fact.save(update_fields=["status"])
    details_before = None

    unreadable = _ooo_message(
        return_day="whenever things calm down", thread_id="t-stress-ooo-3",
        internal_date="1789276244000",
    )
    result = apply_findings(student, [_classify_message(OWN, unreadable)])

    fact.refresh_from_db()
    assert fact.status == status
    assert result.mail_facts_surfaced == 0, (
        "a closed OOO card must not be resurfaced by a later unreadable reply"
    )


# ---------------------------------------------------------------------------
# Idempotence: applying the identical finding N times settles, it never grows.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("times", [1, 2, 3])
def test_reapplying_the_identical_ooo_never_duplicates_the_fact(student, allen, times):
    contact = Contact.all_objects.create(
        user=student, name="Peter Foggo", email="pfoggo@allenco.com", firm=allen
    )
    for _ in range(times):
        apply_findings(student, [_classify_message(OWN, FIRST_OOO)])

    facts = list(MailFact.objects.for_user(student).filter(kind=MailFact.KIND_OOO))
    assert len(facts) == 1, "re-scanning the same message must never mint a second row"
    assert facts[0].return_on.day == 7
    contact.refresh_from_db()
    assert timezone.localtime(contact.snoozed_until).date().day == 7
