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


# A stated time is what a chat claim is corroborated by — see
# `capture.providers.corroborated_chat_status`.
CHAT_AT = "2026-09-10T12:00:00+00:00"


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


def test_matches_inverted_last_first_display_name(student):
    """A corporate address book emits "Last, First[ Middle]" — the exact form
    the founder's real mailbox shows for contacts like "Nunley, Vanessa N".
    `_match_contact` used to name-match with plain `normalize_name` (lowercase
    + whitespace collapse only), which cannot see this inversion, so a
    finding for a known contact drifted straight into `skipped_unmatched`.
    Now it shares `discovery.names_equivalent` with `_match_existing`, which
    already handles the inversion (and a dropped middle initial) for the
    discovery pipeline."""
    contact = Contact.all_objects.create(
        user=student, name="Vanessa Nunley", email="", source="manual",
    )
    result = apply_findings(
        student,
        [finding(name="Nunley, Vanessa N", email="vanessa.n@buyside.example", replied=True)],
    )
    assert result.skipped_unmatched == 0
    assert kinds(student, contact) == ["reply_received"]


def test_matches_a_routing_variant_address(student):
    """Goldman's DSN machinery rewrites `noah.bauld@gs.com` as
    `noah.bauld@ny.ibd.email.gs.com` in transit — same mailbox, same person.
    `_match_contact` used to only try an exact (case-insensitive) address
    match before falling through to name matching; a finding whose sender
    the firm's own routing rewrote, for a contact whose stored address and
    display name are different (so the name rung can't rescue it either),
    used to be reported as unmatched. It now shares
    `discovery.routing_variant` with `_match_existing`."""
    contact = Contact.all_objects.create(
        user=student, name="Noah Bauld", email="noah.bauld@gs.com", source="manual",
    )
    result = apply_findings(
        student,
        [finding(
            name="Someone Else Entirely",
            email="noah.bauld@ny.ibd.email.gs.com",
            replied=True,
        )],
    )
    assert result.skipped_unmatched == 0
    assert kinds(student, contact) == ["reply_received"]


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
    genuinely new stage logs.

    Every chat finding here carries a `chat_scheduled_at`, because that is
    what the ladder now requires of one — see
    `test_a_chat_claim_with_no_time_is_only_a_reply`."""
    apply_findings(student, [finding(replied=True)])
    apply_findings(student, [finding(chat_status="scheduled", chat_scheduled_at=CHAT_AT)])
    apply_findings(student, [finding(chat_status="completed", chat_scheduled_at=CHAT_AT)])
    assert kinds(student, contact) == ["chat", "chat_scheduled", "reply_received"]


def test_ladder_refuses_to_regress(student, contact):
    """The failure this ratchet exists to prevent. `apply_touch` does NOT
    rank-guard thread_state, so a 'scheduled' finding resurfacing after the
    chat already happened would walk a chat_done contact backward. Refuse it
    at this layer."""
    apply_findings(student, [finding(chat_status="completed", chat_scheduled_at=CHAT_AT)])
    contact.refresh_from_db()
    assert contact.thread_state == "chat_done"

    result = apply_findings(student, [finding(chat_status="scheduled", chat_scheduled_at=CHAT_AT)])
    contact.refresh_from_db()
    assert result.touches_logged == 0
    assert result.skipped_already_logged == 1
    assert contact.thread_state == "chat_done", "a stale 'scheduled' must not regress a done chat"


# --------------------------------------------------------------------------- #
# A chat claim has to bring a time
# --------------------------------------------------------------------------- #

class TestChatClaimsNeedATime:
    """`chat_status` is produced by a classifier OUTSIDE this repo for every
    finding the live path did not build (`gmail_live` says "scheduled" only
    when it parsed an .ics DTSTART, and never says "completed" at all). Both
    live failures on these rungs were chat claims with nothing behind them:
    Ellen Chung 2026-08-12 ("completed" off "filled out the form!") and Youqi
    Chen 2026-08-31 ("scheduled" off an offer nobody had accepted).

    `capture_discover` was tightened for "scheduled" on 2026-08-31; this
    module was not tightened at all, and it is the door the daily agent-run
    sync comes through. These pin both rungs, on both doors' shared rule.
    """

    def test_a_chat_claim_with_no_time_is_only_a_reply(self, student, contact):
        """The Youqi Chen shape, on the daily sync's door."""
        result = apply_findings(student, [finding(replied=True, chat_status="scheduled")])
        contact.refresh_from_db()
        assert result.touches_logged == 1
        assert kinds(student, contact) == ["reply_received"]
        assert contact.warmth == "replied"
        assert contact.thread_state == "replied", (
            "an offer nobody accepted must not park her at chat_scheduled"
        )

    def test_a_completed_chat_with_no_time_is_only_a_reply(self, student, contact):
        """The Ellen Chung shape, and the more expensive of the two: `chat`
        sets warmth `chatted`, which `capture_worklist.RECHECK_WARMTH` drops
        from every later re-check, so no automated run can ever revisit it."""
        apply_findings(student, [finding(replied=True, chat_status="completed")])
        contact.refresh_from_db()
        assert kinds(student, contact) == ["reply_received"]
        assert contact.warmth == "replied"
        assert contact.thread_state == "replied"

    def test_an_uncorroborated_claim_on_the_users_own_send_logs_no_ladder_touch(
        self, student, contact
    ):
        """Outbound-only, so the floor is what the message proves: the send.
        The outreach branch has already recorded that; the ladder adds
        nothing, and must not gift warmth `replied` to somebody who has not
        typed a word."""
        result = apply_findings(
            student, [finding(outreach_sent=True, chat_status="scheduled")]
        )
        contact.refresh_from_db()
        assert result.outreach_logged == 1
        assert result.touches_logged == 0
        assert kinds(student, contact) == ["outreach"]
        assert contact.warmth == "cold"

    def test_a_stated_time_is_what_lets_the_claim_through(self, student, contact):
        """The corroborated case is untouched — this is the shape every
        `gmail_live` finding carries, and it still books the chat."""
        apply_findings(
            student, [finding(replied=True, chat_status="scheduled", chat_scheduled_at=CHAT_AT)]
        )
        contact.refresh_from_db()
        assert kinds(student, contact) == ["chat_scheduled"]
        assert contact.thread_state == "chat_scheduled"

    def test_no_pattern_evidence_is_banked_off_an_uncorroborated_chat(
        self, student, contact
    ):
        """`email_pattern_recorded` is a one-shot per-contact flag, so a
        "delivered" banked on a guess also spends the contact's one chance to
        bank the real evidence later."""
        result = apply_findings(student, [finding(chat_status="completed")])
        contact.refresh_from_db()
        assert result.pattern_delivered == 0
        assert contact.email_pattern_recorded is False


def test_the_run_reports_every_touch_it_actually_wrote(student, contact):
    """`touches_logged` counts LADDER stages only, which is right for "how
    much progress did this run find" and wrong for "what did this run
    write" — and the Settings page renders it as the headline.

    The founder's first-connect backfill (2026-08-30) stored `525 findings,
    0 touches_logged`, so Settings said the scan wrote nothing. The same run
    had inserted 12 `outreach` touches, 1 `follow_up` and 1 `bulk_received`,
    and cleared 4 dead addresses."""
    result = apply_findings(student, [finding(outreach_sent=True)])
    assert result.touches_logged == 0
    assert result.touches_written == 1
    assert result.as_stats()["touches_written"] == 1
    assert Touch.objects.for_user(student).filter(contact=contact).count() == 1


def test_an_earlier_reply_on_another_thread_cannot_regress_a_booked_chat(
    student, contact
):
    """Lily Liu, live 2026-08-25 (contact 765).

    Google opens a NEW Gmail thread for a calendar reply, so a conversation
    does not stay on one thread — `_upsert_scheduled_chat` was rewritten
    around exactly that and keyed the CALENDAR on the .ics UID. The touch
    ladder stayed keyed on the thread, so her two `reply_received` findings
    (dated 08-24 15:40 and 08-25 12:45, on two other threads) each ranked 0 on
    their own thread, were logged after the `chat_scheduled` touch dated 08-25
    15:10, and set her `thread_state` back to `replied`.

    The question is about TIME, not insertion order: something further up the
    ladder has already happened at a later moment, so this finding cannot be
    the state of the relationship.
    """
    later = timezone.now() - timedelta(hours=1)
    earlier = later - timedelta(days=1)

    apply_findings(student, [finding(
        thread_id="invite", chat_status="scheduled",
        chat_scheduled_at=CHAT_AT, occurred_at=later.isoformat(),
    )])
    contact.refresh_from_db()
    assert contact.thread_state == "chat_scheduled"

    result = apply_findings(student, [finding(
        thread_id="reply-thread", replied=True, occurred_at=earlier.isoformat(),
    )])
    contact.refresh_from_db()
    assert result.touches_logged == 0
    assert result.skipped_already_logged == 1
    assert contact.thread_state == "chat_scheduled"


def test_a_second_reply_on_a_second_thread_still_logs(student, contact):
    """The guard above is deliberately strict-greater, not at-or-above: a
    same-rank touch moves nothing backward, and a second genuine reply is a
    second real event whose whole job is to move `last_touch`."""
    first = timezone.now() - timedelta(days=2)
    second = timezone.now() - timedelta(days=1)

    apply_findings(student, [finding(
        thread_id="t-a", replied=True, occurred_at=first.isoformat())])
    result = apply_findings(student, [finding(
        thread_id="t-b", replied=True, occurred_at=second.isoformat())])
    assert result.touches_logged == 1
    assert kinds(student, contact) == ["reply_received", "reply_received"]


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


# --------------------------------------------------------------------------- #
# One malformed finding must never take the whole batch down. Each of the
# three per-finding hooks (application-mail, mail-facts, discovery) used to
# run with no guard at all: an exception raised while classifying finding #1
# propagated straight out of `apply_findings`, so findings #2..#N in the same
# batch — for other contacts entirely — never got their touch logged, and on
# the live listener path (`gmail_live.sync_connection`) the mailbox's history
# cursor never advanced either, since that write happens after the
# `apply_findings` call returns. The next poll would re-fetch the same
# messages and raise on the same finding forever.
# --------------------------------------------------------------------------- #

def test_appmail_hook_raising_does_not_lose_the_rest_of_the_batch(student, contact, monkeypatch):
    from capture import appmail

    def boom(*_a, **_k):
        raise ValueError("simulated malformed finding")

    monkeypatch.setattr(appmail, "consider_finding", boom)

    other = Contact.all_objects.create(
        user=student, name="Second Person", email="second@bank.example", source="manual",
    )
    batch = [
        finding(replied=True),
        finding(name="Second Person", email="second@bank.example", replied=True,
                thread_id="t-2"),
    ]
    result = apply_findings(student, batch)

    assert result.app_events_errors == 2  # one per finding in the batch
    assert kinds(student, contact) == ["reply_received"]
    assert kinds(student, other) == ["reply_received"]
    assert any("application-mail read failed" in line for line in result.details)


def test_mailfacts_hook_raising_does_not_lose_the_rest_of_the_batch(student, contact, monkeypatch):
    from capture import mailfacts

    def boom(*_a, **_k):
        raise ValueError("simulated malformed finding")

    monkeypatch.setattr(mailfacts, "consider_finding", boom)

    other = Contact.all_objects.create(
        user=student, name="Second Person", email="second@bank.example", source="manual",
    )
    batch = [
        finding(replied=True),
        finding(name="Second Person", email="second@bank.example", replied=True,
                thread_id="t-2"),
    ]
    result = apply_findings(student, batch)

    assert result.mail_facts_errors == 2
    assert kinds(student, contact) == ["reply_received"]
    assert kinds(student, other) == ["reply_received"]
    assert any("mail-facts read failed" in line for line in result.details)


def test_discovery_hook_raising_does_not_lose_the_rest_of_the_batch(student, contact, monkeypatch):
    """The discovery hook only ever runs on the UNMATCHED branch, so it needs
    its own unmatched finding ahead of a normal, matched one to prove the
    same thing: a blown-up discovery pass costs the unmatched proposal, not
    the matched contact's touch queued right behind it."""
    from capture import discovery

    def boom(*_a, **_k):
        raise ValueError("simulated malformed finding")

    monkeypatch.setattr(discovery, "consider_finding", boom)

    batch = [
        finding(name="Nobody", email="no@one.example", replied=True, thread_id="t-x"),
        finding(replied=True),
    ]
    result = apply_findings(student, batch)

    assert result.discovery_errors == 1
    assert result.skipped_unmatched == 1
    assert Contact.objects.for_user(student).filter(name="Nobody").count() == 0
    assert kinds(student, contact) == ["reply_received"]
    assert any("contact-discovery check failed" in line for line in result.details)
