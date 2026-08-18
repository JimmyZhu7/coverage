"""Gmail-findings provider tests.

The centre of gravity here is the **caller-side thread ratchet**
(`capture/gmail.py`), because `coverage_domain.pipeline` deliberately does not
guard `thread_state` against regression — its own docstring says so, and pins
that boundary with `test_thread_state_is_not_rank_guarded_outside_advocate`.
The ratchet is what makes a *daily* run safe rather than a one-shot import, so
these tests exist to keep it honest: same finding twice must be a no-op, and a
later stage must never be walked backward by an earlier one resurfacing.

``transaction=True`` for the same reason `test_pipeline.py` and crm's service
tests need it: applying a finding calls `crm.services.log_touch`, which opens
its own psycopg connection. That connection cannot see rows written inside
pytest's wrapping transaction, so the contact would not exist from its side.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone

from capture.gmail import apply_findings
from crm.models import Contact, Touch

pytestmark = pytest.mark.django_db(transaction=True)

User = get_user_model()


@pytest.fixture
def student(db):
    return User.objects.create_user(
        email="gmail-student@example.com", password="x"
    )


@pytest.fixture
def contact(student):
    return Contact.all_objects.create(
        user=student, name="Jane Banker", email="jane@bank.example", source="manual"
    )


def finding(**over):
    base = {
        "name": "Jane Banker",
        "email": "jane@bank.example",
        "found": True,
        "bounced": False,
        "outreach_sent": False,
        "replied": False,
        "chat_status": "none",
        "evidence": "thread summary",
        "thread_id": "t-1",
    }
    base.update(over)
    return base


def kinds(user, contact):
    return sorted(
        Touch.objects.for_user(user).filter(contact=contact).values_list("kind", flat=True)
    )


# --------------------------------------------------------------------------- #
# Matching
# --------------------------------------------------------------------------- #

def test_matches_by_email(student, contact):
    apply_findings(student, [finding(replied=True)])
    assert kinds(student, contact) == ["reply_received"]


def test_matches_by_name_when_email_differs(student, contact):
    """A finding that discovered a second address for a known person still
    lands on that person — and NOTES the address rather than overwriting the
    one on file or forking a contact.

    This test used to assert the overwrite (`contact.email ==
    "j.banker@bank.example"`, `emails_backfilled == 1`). That behaviour was
    the bug: the counter is called `emails_backfilled`, the docstring says
    "backfill", and matching here can succeed on NAME ALONE — so a finding
    for a common name from someone's personal Gmail could replace the work
    address the student actually needs to write to, and detach the person
    from their firm's domain in the process. A second address is information,
    not a correction.
    """
    result = apply_findings(
        student, [finding(email="j.banker@bank.example", replied=True)]
    )
    contact.refresh_from_db()
    assert contact.email == "jane@bank.example", "the primary address stands"
    assert "j.banker@bank.example" in contact.notes, "the alternate is recorded"
    assert result.emails_backfilled == 0
    assert result.alternate_emails_noted == 1
    assert Contact.objects.for_user(student).count() == 1


def test_blank_email_is_still_filled_in(student):
    """The behaviour the counter was always NAMED for, and the one case where
    writing the column is unambiguous: there is nothing there to destroy."""
    contact = Contact.all_objects.create(
        user=student, name="Jane Banker", email="", source="manual"
    )
    result = apply_findings(student, [finding(email="jane@bank.example", replied=True)])
    contact.refresh_from_db()
    assert contact.email == "jane@bank.example"
    assert result.emails_backfilled == 1
    assert result.alternate_emails_noted == 0


def test_the_same_alternate_email_is_noted_only_once(student, contact):
    """This sync runs DAILY and the same thread resurfaces every day it stays
    in the search window, so an unguarded note append would grow the contact's
    notes without bound."""
    batch = [finding(email="j.banker@bank.example", replied=True)]
    apply_findings(student, batch)
    result = apply_findings(student, batch)
    contact.refresh_from_db()
    assert result.alternate_emails_noted == 0
    assert contact.notes.lower().count("j.banker@bank.example") == 1


def test_unmatched_finding_never_invents_a_contact(student):
    """Unlike the BCC path (which auto-creates a pending contact), a Gmail
    finding names someone already being tracked. An unmatched one means the two
    systems have drifted — report it, don't paper over it."""
    result = apply_findings(student, [finding(name="Nobody", email="no@one.example",
                                              replied=True)])
    assert result.skipped_unmatched == 1
    assert Contact.objects.for_user(student).count() == 0


def test_ambiguous_name_is_skipped_not_guessed(student, contact):
    """Two "Jane Banker"s at different firms, a finding whose email matches
    neither -- _match_contact used to silently return the first row the
    queryset yielded. Now it must refuse and report the finding as
    ambiguous rather than log a touch on either homonym."""
    other_jane = Contact.all_objects.create(
        user=student, name="Jane Banker", email="jane@other-bank.example", source="manual",
    )
    result = apply_findings(
        student, [finding(email="jane@nowhere.example", replied=True)]
    )
    assert result.skipped_ambiguous == 1
    assert kinds(student, contact) == []
    other_jane.refresh_from_db()
    assert other_jane.warmth == "cold"


def test_not_found_is_left_completely_alone(student, contact):
    result = apply_findings(student, [finding(found=False, replied=True)])
    assert result.skipped_not_found == 1
    assert kinds(student, contact) == []


# --------------------------------------------------------------------------- #
# The thread ratchet — the reason this module exists
# --------------------------------------------------------------------------- #

def test_same_finding_twice_is_a_no_op(student, contact):
    """The daily-run guarantee. Yesterday's thread is still inside today's
    search window and gets re-found; it must not re-log."""
    apply_findings(student, [finding(replied=True)])
    result = apply_findings(student, [finding(replied=True)])
    assert result.touches_logged == 0
    assert result.skipped_already_logged == 1
    assert kinds(student, contact) == ["reply_received"]


def test_thread_climbs_the_ladder(student, contact):
    """reply -> scheduled -> chat over three days, all on one thread: each
    genuinely new stage logs."""
    apply_findings(student, [finding(replied=True)])
    apply_findings(student, [finding(chat_status="scheduled")])
    apply_findings(student, [finding(chat_status="completed")])
    assert kinds(student, contact) == ["chat", "chat_scheduled", "reply_received"]


def test_ladder_refuses_to_regress(student, contact):
    """The failure this ratchet exists to prevent. `apply_touch` does NOT
    rank-guard thread_state, so a 'scheduled' finding resurfacing after the
    chat already happened would walk a chat_done contact backward. Refuse it
    at this layer."""
    apply_findings(student, [finding(chat_status="completed")])
    contact.refresh_from_db()
    assert contact.thread_state == "chat_done"

    result = apply_findings(student, [finding(chat_status="scheduled")])
    contact.refresh_from_db()
    assert result.touches_logged == 0
    assert result.skipped_already_logged == 1
    assert contact.thread_state == "chat_done", "a stale 'scheduled' must not regress a done chat"


def test_separate_threads_each_log(student, contact):
    """The ratchet is per thread, not per contact: a second conversation with
    the same person is a real, distinct event."""
    apply_findings(student, [finding(replied=True, thread_id="t-1")])
    apply_findings(student, [finding(replied=True, thread_id="t-2")])
    assert kinds(student, contact) == ["reply_received", "reply_received"]


# --------------------------------------------------------------------------- #
# The no-thread_id fallback
# --------------------------------------------------------------------------- #

def test_no_thread_id_dedups_on_a_time_window(student, contact):
    """Without a thread marker there is no stable key, so an open thread would
    re-log daily — silently resetting last_touch so the cadence engine's
    follow-up nudges never come due."""
    apply_findings(student, [finding(thread_id=None, replied=True)])
    result = apply_findings(student, [finding(thread_id=None, replied=True)])
    assert result.touches_logged == 0
    assert kinds(student, contact) == ["reply_received"]


def test_no_thread_id_logs_again_after_the_window(student, contact):
    """A genuinely new event later still lands."""
    apply_findings(student, [finding(thread_id=None, replied=True)])
    old = Touch.objects.for_user(student).get(contact=contact)
    Touch.all_objects.filter(pk=old.pk).update(ts=timezone.now() - timedelta(days=30))

    result = apply_findings(student, [finding(thread_id=None, replied=True)])
    assert result.touches_logged == 1


# --------------------------------------------------------------------------- #
# Outreach + bounces
# --------------------------------------------------------------------------- #

def test_outreach_dedups_per_contact_not_per_thread(student, contact):
    """'Have I ever sent a first note' is a per-contact fact — the same way the
    cadence engine decides whether first outreach is still due."""
    apply_findings(student, [finding(outreach_sent=True, thread_id="t-1")])
    result = apply_findings(student, [finding(outreach_sent=True, thread_id="t-99")])
    assert result.outreach_logged == 0
    assert kinds(student, contact) == ["outreach"]


def test_outreach_does_not_block_a_reply_on_the_same_thread(student, contact):
    """`outreach` carries the thread marker but is off the ladder, so it must
    not suppress a later reply from that thread."""
    apply_findings(student, [finding(outreach_sent=True, replied=True)])
    assert kinds(student, contact) == ["outreach", "reply_received"]


def test_bounce_clears_the_address_and_keeps_the_person(student, contact):
    """A bounce is a fact about one string in one column, so it may only
    change that column.

    This replaces `test_bounce_archives_rather_than_deletes`, which asserted
    `contact.archived is True` on the reasoning that archiving is a soft
    delete and "trivially recoverable". It wasn't: `archived` had no UI at
    all, and both capture resolvers filter it out — so a later genuine reply
    from this person forked a SECOND contact instead of resurrecting the
    first, splitting the relationship's history in half. Archiving is now a
    user action with a control and an undo (crm.views.contact_archive); no
    automated path takes it.
    """
    result = apply_findings(student, [finding(bounced=True, replied=True)])
    contact.refresh_from_db()
    assert contact.archived is False, "no automated path may archive"
    assert contact.email == "", "the address that bounced is gone"
    assert "jane@bank.example" in contact.notes, "but recorded, not destroyed"
    assert result.bounced_cleared == 1
    assert kinds(student, contact) == [], "a bounced address has nothing to log"


def test_a_bounced_contact_still_resolves_instead_of_forking(student, contact):
    """The concrete cost of the old archive-on-bounce, pinned. `_match_contact`
    filters `archived=False`, so after a bounce the person was invisible to
    every later finding about them."""
    apply_findings(student, [finding(bounced=True)])
    apply_findings(student, [finding(email="", replied=True)])
    contact.refresh_from_db()
    assert Contact.objects.for_user(student).count() == 1, "no fork"
    assert kinds(student, contact) == ["reply_received"]


def test_bounce_is_idempotent(student, contact):
    apply_findings(student, [finding(bounced=True)])
    result = apply_findings(student, [finding(bounced=True)])
    contact.refresh_from_db()
    assert result.bounced_cleared == 0
    assert contact.notes.count("bounced") == 1, "the daily run can't stack notes"


# --------------------------------------------------------------------------- #
# Privacy + provenance
# --------------------------------------------------------------------------- #

def test_touch_note_carries_the_thread_marker_and_no_body(student, contact):
    apply_findings(student, [finding(replied=True, evidence="asked about deal flow")])
    note = Touch.objects.for_user(student).get(contact=contact).note
    assert note.startswith("[gmail:t-1]"), note
    assert "asked about deal flow" in note


def test_dry_run_writes_nothing(student, contact):
    """Regression: the first version of `capture_gmail --dry-run` wrapped the
    call in `transaction.atomic()` and rolled back, which does NOT cover
    `log_touch` — that opens its own psycopg connection and commits there. A
    "dry" run wrote real touches. The guarantee now lives in apply_findings."""
    result = apply_findings(
        student,
        [finding(replied=True, email="new@bank.example", outreach_sent=True)],
        dry_run=True,
    )
    contact.refresh_from_db()
    assert result.touches_logged == 1, "must still REPORT what it would do"
    assert result.outreach_logged == 1
    # A differing address is now an alternate, not a backfill (the contact
    # already has one) — so this reports under the other counter.
    assert result.alternate_emails_noted == 1
    assert Touch.objects.for_user(student).count() == 0, "but write no touches"
    assert contact.email == "jane@bank.example", "and not touch the address"
    assert "new@bank.example" not in contact.notes, "and write no note"


def test_dry_run_bounce_does_not_clear_the_address(student, contact):
    result = apply_findings(student, [finding(bounced=True)], dry_run=True)
    contact.refresh_from_db()
    assert result.bounced_cleared == 1
    assert contact.email == "jane@bank.example"
    assert contact.archived is False


def test_dry_run_then_real_run_agree(student, contact):
    """The report has to predict the real thing, which is the entire point of
    running every decision on one shared code path."""
    batch = [finding(replied=True)]
    dry = apply_findings(student, batch, dry_run=True)
    real = apply_findings(student, batch)
    assert dry.as_stats() == real.as_stats()


def test_touches_are_tenant_scoped(student, contact, db):
    """A finding must never reach another user's identically-named contact."""
    other = User.objects.create_user(
        email="other@example.com", password="x"
    )
    Contact.all_objects.create(
        user=other, name="Jane Banker", email="jane@bank.example", source="manual"
    )
    apply_findings(student, [finding(replied=True)])
    assert Touch.objects.for_user(other).count() == 0
    assert Touch.objects.for_user(student).count() == 1
