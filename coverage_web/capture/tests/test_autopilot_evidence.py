"""Counter-evidence: the run gathers its own, and says what it could not read.

THE BUG. `capture_autopilot` run WITHOUT `--findings` showed the model
"OTHER MAIL ABOUT THIS ADDRESS OR THREAD: none found" and accepted all 53
rows — including a man whose firm's auto-reply says he has left, and one
whose mailbox was full. The same code WITH the findings file had correctly
escalated exactly those two. Same data, opposite answer, and nothing
warned the caller that the difference was a forgotten flag.

THE FIX UNDER TEST. `run_autopilot` reads the stored `MailFact` rows for
itself, on every run, with no argument to forget — and states on the run
what it could and could not weigh. Every test below passes NOTHING to
`run_autopilot`: no findings, no context notes. If the run stops reading
its own counter-evidence, this file fails.

`transaction=True` for the same reason test_autopilot.py gives.
"""

from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone

from capture import autopilot
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
    return User.objects.create_user(email="ap-evidence@example.com", password="x")


@pytest.fixture
def firm():
    return Firm.objects.create(
        slug="allen-co", name="Allen & Company", domains=["allenco.example"]
    )


def make_proposal(student, firm, i=0, **over):
    fields = dict(
        user=student,
        name=f"Alex Banker{i}",
        email=f"alex.banker{i}@allenco.example",
        firm=firm,
        evidence=f"You wrote to them: USC | Coffee Chat Request {i}",
        evidence_kind="outreach",
        thread_subject=f"USC | Coffee Chat Request {i}",
        thread_id=f"t{i}",
        occurred_at=timezone.now(),
    )
    fields.update(over)
    return ContactProposal.all_objects.create(**fields)


def accepting(text, *, model=""):
    return "accept", 0.95, text.splitlines()[0], "clean outreach"


def reading_the_evidence(text, *, model=""):
    """A stand-in for the real model that behaves the way it did: it
    escalates when the OTHER MAIL section carries a line, and accepts when
    it does not. That difference — a populated counter-evidence section or
    an empty one — IS the whole bug, so the fake decider must be sensitive
    to exactly it and to nothing else."""
    for line in text.splitlines():
        if line.startswith("- "):
            return "needs_review", 0.9, line[2:], "their mailbox says otherwise"
    return "accept", 0.95, text.splitlines()[0], "clean outreach"


# --------------------------------------------------------------------------- #
# THE BUG: counter-evidence nobody passed in
# --------------------------------------------------------------------------- #
def test_departed_person_escalates_with_no_caller_evidence(student, firm):
    """The Somil case. A stored departure fact about this address must
    reach the verdict even though the caller passed no findings and no
    context — which is precisely how the bad run was invoked."""
    departed = make_proposal(
        student, firm, 1, name="Somil Agarwal", email="sagarwal@allenco.example"
    )
    quote = (
        "Somil Agarwal is no longer with Allen & Company. For matters with "
        "which Somil was involved, please contact Salima Vahabzadeh at "
        "salima@allenco.example."
    )
    MailFact.all_objects.create(
        user=student, kind=MailFact.KIND_DEPARTED,
        about_email="sagarwal@allenco.example", about_name="Somil Agarwal",
        quote=quote, subject="Automatic reply: USC | Coffee Chat Request",
        status=MailFact.STATUS_APPLIED,
    )
    clean = make_proposal(student, firm, 2)

    # No findings. No context_notes. Exactly the invocation that went wrong.
    report = autopilot.run_autopilot(student, decide=reading_the_evidence)

    verdicts = {line.row_id: line for line in report.lines if line.kind == "proposal"}
    assert verdicts[departed.pk].decision == AutopilotDecision.DECIDE_ESCALATE
    # And it escalated ON the mailbox's own sentence, not on a hunch.
    assert "no longer with Allen & Company" in verdicts[departed.pk].quote
    # The row with nothing against it is still accepted — a pass that
    # escalates everything has automated nothing.
    assert verdicts[clean.pk].decision == AutopilotDecision.DECIDE_ACCEPT


def test_full_mailbox_escalates_with_no_caller_evidence(student, firm):
    """The Noah case: a soft bounce leaves a `routing_address` fact whose
    quote is the DSN's own sentence."""
    bounced = make_proposal(
        student, firm, 3, name="Noah Bauld", email="nbauld@allenco.example"
    )
    MailFact.all_objects.create(
        user=student, kind=MailFact.KIND_ROUTING,
        about_email="nbauld@allenco.example", about_name="Noah Bauld",
        quote=(
            "The recipient's mailbox is full and can't accept messages now. "
            "Please try resending your message later"
        ),
        subject="Delivery Status Notification (Failure)",
        status=MailFact.STATUS_APPLIED,
    )
    report = autopilot.run_autopilot(student, decide=reading_the_evidence)
    line = next(x for x in report.lines if x.row_id == bounced.pk)
    assert line.decision == AutopilotDecision.DECIDE_ESCALATE
    assert "mailbox is full" in line.quote


def test_a_fact_the_user_dismissed_is_not_counter_evidence(student, firm):
    """The user's word outranks the reader's, the same way an overridden
    decision outranks every future run."""
    p = make_proposal(student, firm, 4)
    MailFact.all_objects.create(
        user=student, kind=MailFact.KIND_DEPARTED, about_email=p.email,
        quote="Alex Banker4 has left the firm.",
        status=MailFact.STATUS_DISMISSED,
    )
    report = autopilot.run_autopilot(student, decide=reading_the_evidence)
    line = next(x for x in report.lines if x.row_id == p.pk)
    assert line.decision == AutopilotDecision.DECIDE_ACCEPT


def test_referral_quote_never_lands_on_the_person_it_named(student, firm):
    """A referral fact is evidence AGAINST the person who redirected you
    and FOR the person named. Keyed on `about_email` only, so the referral
    card the fact created is not escalated by its own justification."""
    salima = make_proposal(
        student, firm, 5, name="Salima Vahabzadeh",
        email="salima@allenco.example", evidence_kind="referral",
    )
    MailFact.all_objects.create(
        user=student, kind=MailFact.KIND_REFERRAL,
        about_email="sagarwal@allenco.example",
        new_email="salima@allenco.example",
        quote="please contact Salima Vahabzadeh at salima@allenco.example",
        status=MailFact.STATUS_APPLIED,
    )
    report = autopilot.run_autopilot(student, decide=reading_the_evidence)
    line = next(x for x in report.lines if x.row_id == salima.pk)
    assert line.decision == AutopilotDecision.DECIDE_ACCEPT


def test_every_run_states_what_it_could_and_could_not_read(student, firm):
    """Partial counter-evidence is allowed; SILENT partial counter-evidence
    is the bug. The run says so on its own row, in both directions."""
    make_proposal(student, firm)
    report = autopilot.run_autopilot(student, decide=accepting)
    run = AutopilotRun.all_objects.get(user=student)
    assert run.evidence_note == report.evidence_note
    assert "No stored mail facts" in run.evidence_note
    assert "hard bounces and mass-send flags" in run.evidence_note

    MailFact.all_objects.create(
        user=student, kind=MailFact.KIND_OOO, about_email="x@allenco.example",
        quote="I am out of the office until 3 September.",
    )
    make_proposal(student, firm, 9)
    autopilot.run_autopilot(student, decide=accepting)
    later = AutopilotRun.all_objects.filter(user=student).order_by("-pk").first()
    assert "Read 1 stored mail fact" in later.evidence_note
