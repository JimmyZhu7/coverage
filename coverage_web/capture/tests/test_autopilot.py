"""Autopilot tests (capture/autopilot.py and its Today/ledger surfaces).

The centre of gravity is the GUARDS, in both directions: the confidence
floor accepts above and escalates below; an ungrounded answer takes no
action whatever it claimed; the model never sees a row the deterministic
layer resolved; the user's override survives every later run; a pass that
dies anywhere leaves no partial CRM state; and the spend ceiling stops a
runaway before the first unaffordable call.

`transaction=True` for the same reason as test_discovery.py: apply goes
through `crm.services.log_touch`, which opens its own psycopg connection
and can only see committed rows.
"""

from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse
from django.utils import timezone

from billing.models import CreditLedger
from capture import autopilot
from capture.models import (
    ApplicationEvent,
    AutopilotDecision,
    AutopilotRun,
    ContactProposal,
)
from crm.models import Contact, Touch
from directory.models import Firm, Opportunity

User = get_user_model()

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def student():
    return User.objects.create_user(email="ap-student@example.com", password="x")


@pytest.fixture
def firm():
    return Firm.objects.create(
        slug="north-bank", name="North Bank", domains=["northbank.example"]
    )


def make_proposal(student, firm, i=0, **over):
    fields = dict(
        user=student,
        name=f"Alex Banker{i}",
        email=f"alex.banker{i}@northbank.example",
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
    """A decider that grounds its accept in the evidence's own first line."""
    return "accept", 0.95, text.splitlines()[0], "clean outreach"


def escalating(text, *, model=""):
    return "needs_review", 0.9, text.splitlines()[0], "something is off"


# --------------------------------------------------------------------------- #
# The gate: floor and grounding, both directions
# --------------------------------------------------------------------------- #
def test_grounded_accept_above_floor_is_accepted(student, firm):
    make_proposal(student, firm)
    report = autopilot.run_autopilot(student, decide=accepting)
    assert report.count("accept") == 1
    d = AutopilotDecision.all_objects.get(user=student)
    assert d.decision == AutopilotDecision.DECIDE_ACCEPT
    assert d.quote.startswith("PERSON: Alex Banker0")
    # Decided is not applied: nothing in the CRM moved.
    assert Contact.all_objects.filter(user=student).count() == 0


def test_accept_below_floor_escalates(student, firm):
    make_proposal(student, firm)

    def timid(text, *, model=""):
        return "accept", autopilot.ACCEPT_FLOOR - 0.01, text.splitlines()[0], ""

    report = autopilot.run_autopilot(student, decide=timid)
    assert report.count("accept") == 0
    d = AutopilotDecision.all_objects.get(user=student)
    assert d.decision == AutopilotDecision.DECIDE_ESCALATE
    assert "below the confidence floor" in d.reason


def test_model_escalation_is_never_blocked(student, firm):
    """The floor binds accepts only — a grounded needs_review at ANY
    confidence stays an escalation."""
    make_proposal(student, firm)
    report = autopilot.run_autopilot(student, decide=escalating)
    assert report.count("escalate") == 1
    assert AutopilotDecision.all_objects.get(user=student).quote


def test_ungrounded_answer_takes_no_action(student, firm):
    """A fabricated quote loses the accept, whatever the confidence — and
    the stored decision carries no quote rather than a fake one."""
    make_proposal(student, firm)

    def fabricating(text, *, model=""):
        return "accept", 0.99, "a sentence that is not in the evidence", "looks fine"

    report = autopilot.run_autopilot(student, decide=fabricating)
    assert report.count("accept") == 0
    d = AutopilotDecision.all_objects.get(user=student)
    assert d.decision == AutopilotDecision.DECIDE_ESCALATE
    assert d.quote == ""
    assert "did not match" in d.reason
    run = AutopilotRun.all_objects.get(user=student)
    outcome, applied = autopilot.apply_run(run)
    assert applied == 0
    assert Contact.all_objects.filter(user=student).count() == 0


def test_counter_evidence_reaches_the_verdict(student, firm):
    """A refused bounce finding about a proposed address lands in that
    row's evidence text — the seam the escalation quote comes through."""
    p = make_proposal(student, firm)
    findings = [{
        "email": p.email, "bounced": True, "bulk": False,
        "thread_id": p.thread_id,
        "subject": "Undeliverable: USC | Coffee Chat Request 0",
        "evidence": "The recipient's mailbox is full and can't accept messages now.",
    }]
    seen = {}

    def spy(text, *, model=""):
        seen["text"] = text
        return "needs_review", 0.95, "mailbox is full", "delivery deferred"

    autopilot.run_autopilot(student, findings=findings, decide=spy)
    assert "mailbox is full" in seen["text"]
    d = AutopilotDecision.all_objects.get(user=student)
    assert d.decision == AutopilotDecision.DECIDE_ESCALATE
    assert d.quote == "mailbox is full"


# --------------------------------------------------------------------------- #
# Deterministic first
# --------------------------------------------------------------------------- #
def test_resolved_rows_never_reach_the_model(student, firm):
    make_proposal(student, firm, 0, status=ContactProposal.STATUS_DISMISSED)
    make_proposal(student, firm, 1, status=ContactProposal.STATUS_ACCEPTED)
    calls = []

    def spy(text, *, model=""):
        calls.append(text)
        return accepting(text)

    report = autopilot.run_autopilot(student, decide=spy)
    assert calls == []
    assert report.lines == []


def test_dry_run_writes_nothing(student, firm):
    make_proposal(student, firm)
    report = autopilot.run_autopilot(student, dry_run=True, decide=accepting)
    assert report.dry_run and report.count("accept") == 1
    assert AutopilotRun.all_objects.count() == 0
    assert AutopilotDecision.all_objects.count() == 0
    assert CreditLedger.all_objects.filter(
        user=student, kind=CreditLedger.KIND_SPEND_AUTOPILOT
    ).count() == 0


# --------------------------------------------------------------------------- #
# Apply: one tap, one transaction, same doors as a thumb
# --------------------------------------------------------------------------- #
def test_apply_creates_contact_through_the_ratchet(student, firm):
    p = make_proposal(student, firm)
    autopilot.run_autopilot(student, decide=accepting)
    run = AutopilotRun.all_objects.get(user=student)
    assert run.status == AutopilotRun.STATUS_REVIEWED

    outcome, applied = autopilot.apply_run(run)
    assert outcome == autopilot.APPLIED and applied == 1
    contact = Contact.all_objects.get(user=student, email=p.email)
    touches = list(Touch.all_objects.filter(contact=contact))
    assert len(touches) == 1 and touches[0].kind == "outreach"
    p.refresh_from_db()
    assert p.status == ContactProposal.STATUS_ACCEPTED
    d = AutopilotDecision.all_objects.get(user=student)
    assert d.status == AutopilotDecision.STATUS_APPLIED
    assert d.created_contact and d.contact_id == contact.id
    # Idempotent: a second tap is a no-op.
    outcome, applied = autopilot.apply_run(run)
    assert outcome == autopilot.NOT_REVIEWED and applied == 0
    assert Contact.all_objects.filter(user=student).count() == 1


def test_apply_refuses_a_run_that_never_finished(student, firm):
    make_proposal(student, firm)
    run = AutopilotRun.all_objects.create(
        user=student, status=AutopilotRun.STATUS_FAILED
    )
    assert autopilot.apply_run(run) == (autopilot.NOT_REVIEWED, 0)


def test_apply_that_dies_midway_leaves_no_half_state_and_resumes(
    student, firm, monkeypatch
):
    """The fail-closed contract at the granularity the ratchet's own
    connection design allows (see apply_run's docstring): every decision is
    either fully applied and marked, or untouched; the run stays `reviewed`
    so the strip survives; and the next tap resumes without double-creating
    anything."""
    make_proposal(student, firm, 0)
    make_proposal(student, firm, 1)
    autopilot.run_autopilot(student, decide=accepting)
    run = AutopilotRun.all_objects.get(user=student)

    from capture import discovery as discovery_mod

    real_accept = discovery_mod.accept
    calls = {"n": 0}

    def exploding(proposal):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("boom mid-batch")
        return real_accept(proposal)

    monkeypatch.setattr(autopilot.discovery, "accept", exploding)
    with pytest.raises(RuntimeError):
        autopilot.apply_run(run)

    # Decision 1 completed whole (contact + touch + marked applied);
    # decision 2 is untouched — no half-recorded row anywhere.
    assert Contact.all_objects.filter(user=student).count() == 1
    assert Touch.all_objects.filter(user=student).count() == 1
    statuses = sorted(
        d.status for d in AutopilotDecision.all_objects.filter(user=student)
    )
    assert statuses == [
        AutopilotDecision.STATUS_APPLIED, AutopilotDecision.STATUS_PROPOSED,
    ]
    run.refresh_from_db()
    assert run.status == AutopilotRun.STATUS_REVIEWED

    # The next tap resumes: only the unapplied decision runs, nothing
    # double-creates, and the run completes.
    monkeypatch.setattr(autopilot.discovery, "accept", real_accept)
    outcome, applied = autopilot.apply_run(run)
    assert outcome == autopilot.APPLIED and applied == 1
    assert Contact.all_objects.filter(user=student).count() == 2
    assert Touch.all_objects.filter(user=student).count() == 2


def test_user_action_between_decide_and_tap_wins(student, firm):
    p = make_proposal(student, firm)
    autopilot.run_autopilot(student, decide=accepting)
    run = AutopilotRun.all_objects.get(user=student)
    # The user dismisses the card himself before tapping the batch.
    from capture import discovery

    discovery.dismiss(p)
    outcome, applied = autopilot.apply_run(run)
    assert applied == 0
    p.refresh_from_db()
    assert p.status == ContactProposal.STATUS_DISMISSED
    d = AutopilotDecision.all_objects.get(user=student)
    assert d.overridden and d.status == AutopilotDecision.STATUS_PROPOSED


# --------------------------------------------------------------------------- #
# Undo, and the permanence of the override
# --------------------------------------------------------------------------- #
def test_undo_reverses_exactly_what_apply_did(student, firm):
    p = make_proposal(student, firm)
    autopilot.run_autopilot(student, decide=accepting)
    run = AutopilotRun.all_objects.get(user=student)
    autopilot.apply_run(run)
    d = AutopilotDecision.all_objects.get(user=student)

    assert autopilot.undo_decision(d) == autopilot.UNDONE
    assert Contact.all_objects.filter(user=student).count() == 0
    assert Touch.all_objects.filter(user=student).count() == 0
    p.refresh_from_db()
    assert p.status == ContactProposal.STATUS_PENDING and p.contact_id is None
    d.refresh_from_db()
    assert d.status == AutopilotDecision.STATUS_UNDONE and d.overridden


def test_undo_keeps_a_contact_the_user_built_on(student, firm):
    """If the user logged his own touch on the created contact before the
    undo, the contact is HIS work now — only Autopilot's touch goes."""
    p = make_proposal(student, firm)
    autopilot.run_autopilot(student, decide=accepting)
    run = AutopilotRun.all_objects.get(user=student)
    autopilot.apply_run(run)
    d = AutopilotDecision.all_objects.get(user=student)
    from crm import services as crm_services

    crm_services.log_touch(student.id, d.contact_id, "chat", "coffee_chat")

    assert autopilot.undo_decision(d) == autopilot.UNDONE
    contact = Contact.all_objects.get(pk=d.contact_id)
    kinds = [t.kind for t in Touch.all_objects.filter(contact=contact)]
    assert kinds == ["chat"]  # Autopilot's outreach touch gone, his kept


def test_override_survives_a_rerun(student, firm):
    p = make_proposal(student, firm)
    autopilot.run_autopilot(student, decide=accepting)
    run = AutopilotRun.all_objects.get(user=student)
    autopilot.apply_run(run)
    d = AutopilotDecision.all_objects.get(user=student)
    autopilot.undo_decision(d)

    calls = []

    def spy(text, *, model=""):
        calls.append(text)
        return accepting(text)

    report = autopilot.run_autopilot(student, decide=spy)
    assert calls == []
    assert report.count("skip") == 1
    assert "overrode" in report.lines[0].reason
    p.refresh_from_db()
    assert p.status == ContactProposal.STATUS_PENDING


def test_rerun_never_redecides_a_reviewed_row(student, firm):
    make_proposal(student, firm)
    autopilot.run_autopilot(student, decide=accepting)
    report = autopilot.run_autopilot(student, decide=accepting)
    assert report.llm_calls == 0
    assert report.count("skip") == 1
    assert AutopilotDecision.all_objects.count() == 1


def test_failed_run_rows_are_recoverable(student, firm):
    """A run that died deciding must not lock its rows out forever — the
    next run decides them again; only REVIEWED/APPLIED decisions block."""
    make_proposal(student, firm)

    def dying(text, *, model=""):
        raise autopilot.AutopilotError(RuntimeError("api down"))

    report = autopilot.run_autopilot(student, decide=dying)
    assert not report.ok and report.reason == "failed"
    run = AutopilotRun.all_objects.get(user=student)
    assert run.status == AutopilotRun.STATUS_FAILED
    assert Contact.all_objects.filter(user=student).count() == 0

    report = autopilot.run_autopilot(student, decide=accepting)
    assert report.count("accept") == 1


def test_a_non_transport_bug_in_the_decide_loop_still_fails_the_run(student, firm):
    """Regression: `execute_run`'s docstring promises a claimed run is NEVER
    left at `running` — every path out is REVIEWED or FAILED. That only
    held for `AutopilotError` (a transport failure); any OTHER exception —
    a bug in `_gate`, a malformed decider return, anything not wrapped as
    AutopilotError — used to propagate straight out of `run_autopilot`
    uncaught, which on the worker path (`capture_autopilot_worker`) would
    also have stopped every OTHER user's queued run in that tick from ever
    being decided. One bad row must cost that run, never the whole pass."""
    make_proposal(student, firm)

    def buggy(text, *, model=""):
        raise ValueError("not the AutopilotError transport wrapper")

    report = autopilot.run_autopilot(student, decide=buggy)

    assert not report.ok and report.reason == "failed"
    run = AutopilotRun.all_objects.get(user=student)
    assert run.status == AutopilotRun.STATUS_FAILED
    assert run.failure_reason
    assert Contact.all_objects.filter(user=student).count() == 0

    # Same recovery contract as the AutopilotError case: the next run
    # decides the row again rather than being locked out forever.
    report = autopilot.run_autopilot(student, decide=accepting)
    assert report.count("accept") == 1


# --------------------------------------------------------------------------- #
# The ceilings
# --------------------------------------------------------------------------- #
def test_spend_ceiling_stops_a_runaway(student, firm, settings):
    """With one credit affordable (10 rows at the default rate), a 12-row
    queue decides 10 and defers 2 — no call is ever made past the clamp,
    and the debit matches what ran."""
    settings.CREDIT_PLANS = {
        "free": {"monthly_grant": 1, "message_cost": 1, "daily_burst": 1}
    }
    for i in range(12):
        make_proposal(student, firm, i)
    calls = []

    def spy(text, *, model=""):
        calls.append(text)
        return accepting(text)

    report = autopilot.run_autopilot(student, decide=spy)
    assert len(calls) == 10
    assert report.count("defer") == 2
    run = AutopilotRun.all_objects.get(user=student)
    assert run.deferred == 2 and run.llm_calls == 10 and run.credits_spent == 1
    ledger = CreditLedger.all_objects.filter(
        user=student, kind=CreditLedger.KIND_SPEND_AUTOPILOT
    )
    assert [row.delta for row in ledger] == [-1]
    # The deferred rows are untouched pending cards, decidable next run.
    assert ContactProposal.all_objects.filter(
        user=student, status=ContactProposal.STATUS_PENDING,
        autopilot_decisions__isnull=True,
    ).count() == 2


def test_hard_row_ceiling_binds_before_the_ledger(student, firm, monkeypatch):
    monkeypatch.setattr(autopilot, "MAX_ROWS_PER_RUN", 3)
    for i in range(5):
        make_proposal(student, firm, i)
    report = autopilot.run_autopilot(student, decide=accepting)
    assert report.llm_calls == 3 and report.count("defer") == 2


def test_unconfigured_decides_nothing(student, firm):
    """No key, no calls, no spend, no rows — the real decider path."""
    make_proposal(student, firm)
    report = autopilot.run_autopilot(student)  # default decide, no API key
    assert not report.ok and report.reason == "unconfigured"
    assert AutopilotRun.all_objects.count() == 0
    assert AutopilotDecision.all_objects.count() == 0


# --------------------------------------------------------------------------- #
# Application events ride the same batch
# --------------------------------------------------------------------------- #
@pytest.fixture
def app_event(student, firm):
    opp = Opportunity.objects.create(
        firm=firm, title="Summer Analyst", status="open",
        url="https://northbank.example/sa",
    )
    return ApplicationEvent.all_objects.create(
        user=student, opportunity=opp, firm=firm, firm_text=firm.name,
        event_type=ApplicationEvent.APPLIED, target_status="submitted",
        evidence="Thank you for applying to North Bank",
        detected_by="phrase", match_reason="ats",
    )


def test_app_event_applies_and_undoes(student, firm, app_event):
    from analytics.models import UserOpportunity

    report = autopilot.run_autopilot(student, decide=accepting)
    assert report.count("accept") == 1
    line = report.lines[-1]
    assert line.kind == "app_event" and line.detected_by == "deterministic"

    run = AutopilotRun.all_objects.get(user=student)
    outcome, applied = autopilot.apply_run(run)
    assert applied == 1
    row = UserOpportunity.all_objects.get(
        user=student, opportunity=app_event.opportunity
    )
    assert row.applied_status == "submitted"
    app_event.refresh_from_db()
    assert app_event.status == ApplicationEvent.STATUS_ACCEPTED

    d = AutopilotDecision.all_objects.get(user=student, app_event=app_event)
    assert autopilot.undo_decision(d) == autopilot.UNDONE
    assert not UserOpportunity.all_objects.filter(
        user=student, opportunity=app_event.opportunity
    ).exists()
    app_event.refresh_from_db()
    assert app_event.status == ApplicationEvent.STATUS_PENDING


# --------------------------------------------------------------------------- #
# The surfaces: one tap on Today, quotes on the ledger
# --------------------------------------------------------------------------- #
def test_one_tap_applies_from_the_cockpit(client, student, firm):
    make_proposal(student, firm)
    autopilot.run_autopilot(student, decide=accepting)
    run = AutopilotRun.all_objects.get(user=student)
    client.force_login(student)

    page = client.get(reverse("crm:week")).content.decode()
    assert "Autopilot read this scan" in page and "Add all 1" in page

    resp = client.post(reverse("crm:autopilot_apply", args=[run.pk]))
    assert resp.status_code == 200
    assert Contact.all_objects.filter(user=student).count() == 1
    # The strip is gone from the swap it returned — the run is applied.
    assert "Autopilot read this scan" not in resp.content.decode()


def test_escalation_quote_rides_the_card(client, student, firm):
    make_proposal(student, firm)

    def flagging(text, *, model=""):
        return "needs_review", 0.95, "PERSON: Alex Banker0", "left the firm"

    autopilot.run_autopilot(student, decide=flagging)
    client.force_login(student)
    page = client.get(reverse("crm:week")).content.decode()
    assert "Autopilot left this one for you" in page
    assert "PERSON: Alex Banker0" in page


def test_ledger_lists_quotes_and_undo(client, student, firm):
    make_proposal(student, firm)
    autopilot.run_autopilot(student, decide=accepting)
    run = AutopilotRun.all_objects.get(user=student)
    autopilot.apply_run(run)
    client.force_login(student)

    page = client.get(reverse("crm:autopilot_log")).content.decode()
    assert "PERSON: Alex Banker0" in page and "Undo" in page

    d = AutopilotDecision.all_objects.get(user=student)
    resp = client.post(reverse("crm:autopilot_undo", args=[d.pk]))
    assert resp.status_code == 302
    d.refresh_from_db()
    assert d.status == AutopilotDecision.STATUS_UNDONE and d.overridden


def test_tenancy_is_enforced(client, student, firm):
    """Another tenant can neither apply nor undo this user's run."""
    make_proposal(student, firm)
    autopilot.run_autopilot(student, decide=accepting)
    run = AutopilotRun.all_objects.get(user=student)
    other = User.objects.create_user(email="ap-other@example.com", password="x")
    client.force_login(other)
    assert client.post(
        reverse("crm:autopilot_apply", args=[run.pk])
    ).status_code == 404
    run.refresh_from_db()
    assert run.status == AutopilotRun.STATUS_REVIEWED


def test_undo_on_a_matched_contact_never_eats_prior_history(student, firm):
    """Regression: `accept` on a proposal that MATCHES an existing live
    contact resolves the proposal without logging any touch — but apply
    used to record the contact's newest capture touch as if it were its
    own, and undo then deleted a touch some earlier Gmail sync had
    legitimately written. The matched contact and every one of their
    touches must survive an apply-then-undo round trip untouched."""
    from crm import services as crm_services

    existing = Contact.all_objects.create(
        user=student, name="Alex Banker0",
        email="alex.banker0@northbank.example", firm=firm, source="capture",
    )
    prior = crm_services.log_touch(
        student.id, existing.id, "reply_received", "email",
        note="[gmail:t-old] they replied months ago", source="capture",
    )
    p = make_proposal(student, firm)  # same email — accept will match

    autopilot.run_autopilot(student, decide=accepting)
    run = AutopilotRun.all_objects.get(user=student)
    autopilot.apply_run(run)
    d = AutopilotDecision.all_objects.get(user=student)

    assert d.contact_id == existing.id
    assert d.created_contact is False
    # Apply logged nothing on a matched contact, so it may claim nothing.
    assert d.touch_id is None

    assert autopilot.undo_decision(d) == autopilot.UNDONE
    # The pre-existing history is intact; only the proposal came back.
    assert Contact.all_objects.filter(pk=existing.pk).exists()
    assert Touch.all_objects.filter(pk=prior.touch_id).exists()
    p.refresh_from_db()
    assert p.status == ContactProposal.STATUS_PENDING
