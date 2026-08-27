"""Autopilot's reviewed backlog: disclosed together, applied together.

The defect: two reviewed runs can coexist (a second decide pass over rows
the first never saw), and the Today strip only surfaced the newest — the
older run was a decision made, disclosed to nobody, and silently dropped
unless the user happened onto the log page. The strip now discloses the
sum across reviewed runs, and the tap applies every reviewed run up to
the one it names (`apply_reviewed_through`).

`transaction=True` for the same reason test_autopilot.py needs it: apply
goes through `crm.services.log_touch`'s own psycopg connection.
"""

from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone

from capture import autopilot
from capture.models import AutopilotRun, ContactProposal
from crm.models import Contact
from directory.models import Firm

User = get_user_model()

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def student():
    return User.objects.create_user(email="log-student@example.com", password="x")


@pytest.fixture
def firm():
    return Firm.objects.create(
        slug="east-bank", name="East Bank", domains=["eastbank.example"]
    )


def make_proposal(student, firm, i=0, **over):
    fields = dict(
        user=student,
        name=f"Lee Banker{i}",
        email=f"lee.banker{i}@eastbank.example",
        firm=firm,
        evidence=f"You wrote to them: USC | Coffee Chat Request {i}",
        evidence_kind="outreach",
        thread_subject=f"USC | Coffee Chat Request {i}",
        thread_id=f"e{i}",
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
# Two reviewed runs: disclosed together, applied together
# --------------------------------------------------------------------------- #

def test_apply_reviewed_through_applies_the_older_run_too(student, firm):
    p1 = make_proposal(student, firm, 1)
    run1 = _reviewed_run(student)

    p2 = make_proposal(student, firm, 2)
    run2 = _reviewed_run(student)
    # run2 decided only the new row; run1's accept still stands unapplied.
    assert run2.decisions.filter(proposal=p2).exists()
    assert not run2.decisions.filter(proposal=p1).exists()

    outcome, applied = autopilot.apply_reviewed_through(run2)
    assert outcome == autopilot.APPLIED
    assert applied == 2

    for run in (run1, run2):
        run.refresh_from_db()
        assert run.status == AutopilotRun.STATUS_APPLIED
    for p in (p1, p2):
        p.refresh_from_db()
        assert p.status == ContactProposal.STATUS_ACCEPTED
    assert Contact.objects.for_user(student).count() == 2


def test_apply_reviewed_through_leaves_newer_runs_alone(student, firm):
    p1 = make_proposal(student, firm, 1)
    run1 = _reviewed_run(student)
    p2 = make_proposal(student, firm, 2)
    run2 = _reviewed_run(student)

    # Tapping the OLDER run applies only what its strip disclosed.
    outcome, applied = autopilot.apply_reviewed_through(run1)
    assert outcome == autopilot.APPLIED
    assert applied == 1
    run2.refresh_from_db()
    assert run2.status == AutopilotRun.STATUS_REVIEWED
    p2.refresh_from_db()
    assert p2.status == ContactProposal.STATUS_PENDING


def test_today_strip_discloses_the_sum_across_reviewed_runs(student, firm):
    from crm.today import _cockpit_context

    make_proposal(student, firm, 1)
    _reviewed_run(student)
    make_proposal(student, firm, 2)
    newest = _reviewed_run(student)

    context = _cockpit_context(student)
    review = context["autopilot_review"]
    assert review is not None
    assert review["accepts"] == 2
    assert review["run"].pk == newest.pk


def test_strip_shows_older_accepts_even_when_newest_has_none(student, firm):
    """The instance the newest-only read hid outright: an older reviewed
    run with accepts behind a newer one with zero."""
    from crm.today import _cockpit_context

    make_proposal(student, firm, 1)
    _reviewed_run(student)

    def escalating(text, *, model=""):
        return "needs_review", 0.9, text.splitlines()[0], "look first"

    make_proposal(student, firm, 2)
    report = autopilot.run_autopilot(student, decide=escalating)
    assert report.run.accepts == 0

    context = _cockpit_context(student)
    review = context["autopilot_review"]
    assert review is not None
    assert review["accepts"] == 1
