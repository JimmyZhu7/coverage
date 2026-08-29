"""Enrichment failing must never mean the sync failed.

WHAT THIS PINS, AND THE MEASURED GAP THAT MOTIVATED IT
------------------------------------------------------
`apply_findings` (capture/gmail.py) runs two ENRICHMENT hooks over every
finding before contact matching even starts: the application-mail hook
(`appmail.consider_finding`) and the mail-facts hook
(`mailfacts.consider_finding`). Both were called bare — no try/except — so
a single malformed auto-reply that raised inside mailfacts propagated out of
`apply_findings` and killed the ENTIRE sync for that mailbox. Not just the
enrichment: the touches, the outreach notes, the discovery proposals, the
bounce clears, the campaign detection pass at the end. All of it, for every
finding in the batch, including the ones the pipeline had already read fine.

This is not hypothetical severity on a hypothetical feature. MailFact's
first real firing in the product's history was 2026-08-28 20:19 UTC, on the
founder's own mailbox, during a `gmail_rescan` over 109 findings — five
rows, all `detected_by="rules"`: a `departed` and a `referral` off Allen &
Company's auto-reply, an `out_of_office`, and two `review` rows the gate let
in but no pattern could type. Five rows of history is a data point, not a
track record, and the layer with five rows of history sat unguarded in front
of the layer with the whole product behind it. Today an unguarded raise
there would not merely FAIL to report a mailfacts bug — it would convert one
into silent, mailbox-wide data loss.

The posture these tests hold the file to is not a new invention. It is the
one `gmail_live.backfill_new_contacts` already states in its own except
clause — "an import must never fail because enrichment did" — and the one
`apply_findings` itself already holds for `crm_campaigns.detect` at the
bottom of the same function ("never allowed to fail the sync ... losing a
whole night's capture because a grouping pass raised would be a far worse
trade than one late campaign card"). The two hooks in the middle of the
function were simply missed when they were added.

NOT SWALLOWED SILENTLY, EITHER. `backfill_new_contacts` returns `None` and
says nothing, which is right for an inline enrichment scan whose caller is
an HTTP request. It is wrong here: this runs unattended every two minutes,
so a hook that fails forever and reports nothing is exactly the failure mode
the capture-run observability work exists to end. Each catch increments a
counter that rides out in `SyncResult.as_stats()` — and therefore into the
`Import` ledger rows and `/ops/health/capture/` — and appends a detail line
naming the finding and the exception.

``transaction=True`` for the reason test_gmail.py's docstring gives: applying
a finding calls `crm.services.log_touch`, which opens its own psycopg
connection that cannot see rows written inside pytest's wrapping transaction.
"""

from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model

from capture import appmail, mailfacts
from capture.gmail import apply_findings
from crm.models import Contact, Touch

pytestmark = pytest.mark.django_db(transaction=True)

User = get_user_model()


@pytest.fixture
def student(db):
    return User.objects.create_user(email="isolation@example.com", password="x")


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
        Touch.objects.for_user(user)
        .filter(contact=contact)
        .values_list("kind", flat=True)
    )


class _Boom(RuntimeError):
    """Stands in for whatever a malformed auto-reply actually does inside the
    extractor — an IndexError off a zero-length sentence split, a TypeError
    off a header the parser assumed was a string, a DataError off a quote
    longer than the column. The class of failure is what matters; the
    specific exception is not, which is why this is a plain raise rather
    than a fixture payload that happens to break today's parser (and would
    stop breaking it the moment the parser is fixed).
    """


# --------------------------------------------------------------------------- #
# The mail-facts hook (capture/gmail.py's `mailfacts.consider_finding` call)
# --------------------------------------------------------------------------- #

def test_a_raising_mailfacts_hook_does_not_lose_the_touch(
    student, contact, monkeypatch
):
    """THE CORE PROPERTY. Before the fix this test failed by RAISING out of
    `apply_findings` — no touch row, no SyncResult, nothing for the poller to
    report but a stderr line.
    """
    def _raise(*args, **kwargs):
        raise _Boom("malformed auto-reply")

    monkeypatch.setattr(mailfacts, "consider_finding", _raise)

    result = apply_findings(student, [finding(replied=True)])

    # The primary sync completed: the reply is on the board.
    assert kinds(student, contact) == ["reply_received"]
    assert result.touches_logged == 1


def test_a_raising_mailfacts_hook_is_counted_not_swallowed(
    student, contact, monkeypatch
):
    """The other half of the property: a caught error must still be VISIBLE.
    A hook that fails on every message forever and reports zero is the exact
    silence this work exists to remove.
    """
    def _raise(*args, **kwargs):
        raise _Boom("malformed auto-reply")

    monkeypatch.setattr(mailfacts, "consider_finding", _raise)

    result = apply_findings(student, [finding(replied=True)])

    assert result.mail_facts_errors == 1
    # And it rides out in the stats dict, which is what the `Import` ledger
    # rows and /ops/health/capture/ read — a counter that exists only on the
    # dataclass is a counter nothing can query.
    assert result.as_stats()["mail_facts_errors"] == 1
    assert any("mail-facts" in line for line in result.details)


def test_one_bad_finding_does_not_take_down_the_rest_of_the_batch(
    student, contact, monkeypatch
):
    """Per-FINDING isolation, not per-batch. The hook is inside the loop, so
    the catch has to be too: a batch where finding #1 detonates must still
    apply findings #2..#N. This is the mailbox-wide-data-loss case in
    miniature — 109 findings was a real rescan size on the founder's own
    mailbox, and one poisoned message in it must cost one message.
    """
    calls = {"n": 0}

    def _raise_on_first(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise _Boom("malformed auto-reply")
        return mailfacts.Outcome()

    monkeypatch.setattr(mailfacts, "consider_finding", _raise_on_first)

    other = Contact.all_objects.create(
        user=student, name="Sam Trader", email="sam@bank.example", source="manual"
    )
    result = apply_findings(
        student,
        [
            finding(replied=True),
            finding(
                name="Sam Trader",
                email="sam@bank.example",
                replied=True,
                thread_id="t-2",
            ),
        ],
    )

    assert kinds(student, contact) == ["reply_received"]
    assert kinds(student, other) == ["reply_received"]
    assert result.touches_logged == 2
    assert result.mail_facts_errors == 1


# --------------------------------------------------------------------------- #
# The application-mail hook (capture/gmail.py's `appmail.consider_finding`)
# --------------------------------------------------------------------------- #

def test_a_raising_appmail_hook_does_not_lose_the_touch(
    student, contact, monkeypatch
):
    """The appmail hook sits one line above the mailfacts hook and was
    unguarded for the same reason. It is guarded on its own axis rather than
    sharing one try block, because collapsing them would mean an ATS-parser
    bug reported itself as a mail-facts failure — and the counter exists
    precisely so an operator knows which layer to go fix.
    """
    def _raise(*args, **kwargs):
        raise _Boom("unparseable ATS mail")

    monkeypatch.setattr(appmail, "consider_finding", _raise)

    result = apply_findings(student, [finding(replied=True)])

    assert kinds(student, contact) == ["reply_received"]
    assert result.touches_logged == 1
    assert result.app_events_errors == 1
    assert result.as_stats()["app_events_errors"] == 1
    assert any("application-mail" in line for line in result.details)


def test_both_hooks_failing_still_leaves_the_primary_sync_intact(
    student, contact, monkeypatch
):
    def _raise(*args, **kwargs):
        raise _Boom("everything is on fire")

    monkeypatch.setattr(appmail, "consider_finding", _raise)
    monkeypatch.setattr(mailfacts, "consider_finding", _raise)

    result = apply_findings(student, [finding(replied=True)])

    assert kinds(student, contact) == ["reply_received"]
    assert result.app_events_errors == 1
    assert result.mail_facts_errors == 1


# --------------------------------------------------------------------------- #
# The counters must stay honest when nothing fails
# --------------------------------------------------------------------------- #

def test_a_clean_run_reports_zero_errors(student, contact):
    """The canary is only worth watching if it reads zero in the normal case.
    /ops/health/capture/ treats `> 0` as the whole signal (at ~10 mailboxes
    anything statistical would be theatre), so a counter that drifted upward
    on healthy traffic would make the page unreadable within a day.
    """
    result = apply_findings(student, [finding(replied=True)])

    assert result.mail_facts_errors == 0
    assert result.app_events_errors == 0
    assert result.as_stats()["mail_facts_errors"] == 0
    assert result.as_stats()["app_events_errors"] == 0
