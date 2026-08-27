"""Autopilot `overridden` semantics: the user's word vs the machine's.

The defect: a proposal auto-withdrawn by a mail fact between decide and
tap used to be marked `overridden` — "never re-decide, permanently" —
even though no person ever weighed in. The automated withdrawal now lands
as decision status `superseded`, which blocks nothing: if the user
restores the card, a future run decides it afresh. The user's own
resolution (and undo) keeps the permanent override exactly as before.

`transaction=True` for the same reason test_autopilot.py needs it: apply
goes through `crm.services.log_touch`'s own psycopg connection.
"""

from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone

from capture import autopilot, discovery
from capture.models import (
    AutopilotDecision,
    AutopilotRun,
    ContactProposal,
    MailFact,
)
from directory.models import Firm

User = get_user_model()

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def student():
    return User.objects.create_user(email="sup-student@example.com", password="x")


@pytest.fixture
def firm():
    return Firm.objects.create(
        slug="south-bank", name="South Bank", domains=["southbank.example"]
    )


def make_proposal(student, firm, i=0, **over):
    fields = dict(
        user=student,
        name=f"Sam Banker{i}",
        email=f"sam.banker{i}@southbank.example",
        firm=firm,
        evidence=f"You wrote to them: USC | Coffee Chat Request {i}",
        evidence_kind="outreach",
        thread_subject=f"USC | Coffee Chat Request {i}",
        thread_id=f"s{i}",
        occurred_at=timezone.now(),
    )
    fields.update(over)
    return ContactProposal.all_objects.create(**fields)


def accepting(text, *, model=""):
    return "accept", 0.95, text.splitlines()[0], "clean outreach"


def _reviewed_run(student, **kwargs):
    report = autopilot.run_autopilot(student, decide=accepting, **kwargs)
    assert report.ok and report.run is not None
    assert report.run.status == AutopilotRun.STATUS_REVIEWED
    return report.run


# --------------------------------------------------------------------------- #
# overridden is the user's word; superseded is the machine's
# --------------------------------------------------------------------------- #

def test_mailfact_withdrawal_is_superseded_not_overridden(student, firm):
    proposal = make_proposal(student, firm)
    run = _reviewed_run(student)

    # Between decide and tap, a departed-fact withdraws the pending card —
    # the exact automated path capture.mailfacts takes.
    discovery.dismiss(proposal)
    MailFact.all_objects.create(
        user=student, kind=MailFact.KIND_DEPARTED,
        about_email=proposal.email, about_name=proposal.name,
        quote="Sam Banker0 is no longer with South Bank.",
        status=MailFact.STATUS_APPLIED, proposal=proposal,
        action_note="Their pending card withdrawn",
    )

    outcome, applied = autopilot.apply_run(run)
    assert outcome == autopilot.APPLIED
    assert applied == 0

    decision = run.decisions.get()
    assert decision.status == AutopilotDecision.STATUS_SUPERSEDED
    assert decision.overridden is False
    assert "withdrawn by a mail fact" in decision.reason


def test_superseded_row_is_re_decidable_after_restore(student, firm):
    proposal = make_proposal(student, firm)
    run = _reviewed_run(student)
    discovery.dismiss(proposal)
    MailFact.all_objects.create(
        user=student, kind=MailFact.KIND_DEPARTED,
        about_email=proposal.email, quote="No longer with the firm.",
        status=MailFact.STATUS_APPLIED, proposal=proposal,
    )
    autopilot.apply_run(run)

    # The user restores the withdrawn card: pending again, and no human
    # ever decided anything about this person — a future run must be free
    # to decide it afresh.
    outcome, _ = discovery.restore(proposal)
    proposal.refresh_from_db()
    assert proposal.status == ContactProposal.STATUS_PENDING

    assert autopilot._skip_reason(proposal) is None
    report = autopilot.run_autopilot(student, decide=accepting)
    line = next(l for l in report.lines if l.row_id == proposal.pk)
    assert line.decision == AutopilotDecision.DECIDE_ACCEPT


def test_user_resolution_before_tap_stays_a_permanent_override(student, firm):
    proposal = make_proposal(student, firm)
    run = _reviewed_run(student)

    # The user dismisses the card himself — his word, permanent.
    discovery.dismiss(proposal)
    autopilot.apply_run(run)

    decision = run.decisions.get()
    assert decision.overridden is True
    assert decision.status != AutopilotDecision.STATUS_SUPERSEDED

    discovery.restore(proposal)
    proposal.refresh_from_db()
    assert proposal.status == ContactProposal.STATUS_PENDING
    assert autopilot._skip_reason(proposal) is not None


def test_undo_still_sets_the_permanent_override(student, firm):
    proposal = make_proposal(student, firm)
    run = _reviewed_run(student)
    autopilot.apply_run(run)
    decision = run.decisions.get()
    assert decision.status == AutopilotDecision.STATUS_APPLIED

    assert autopilot.undo_decision(decision) == autopilot.UNDONE
    decision.refresh_from_db()
    assert decision.overridden is True
    proposal.refresh_from_db()
    assert autopilot._skip_reason(proposal) is not None
