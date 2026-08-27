"""Starting an Autopilot run — the button, and the states around it.

Until this existed the AI loop was a management command: a student could
APPLY a finished run and undo it, but could not start one. These tests
hold the four promises the control has to keep — one run per tap however
many taps arrive, a request that never waits on the model, a state the
student can always read (including "it died"), and a price disclosed
before the tap rather than discovered after it — plus the one it must
never break: starting a run applies nothing, and the separate tap that
already existed is still the only thing that writes to the CRM.

The counter-evidence rule those verdicts stand on lives next door, in
test_autopilot_evidence.py.

`transaction=True` for the same reason test_autopilot.py gives.
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
from crm.models import Contact
from directory.models import Firm

User = get_user_model()

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture(autouse=True)
def configured(settings):
    """The button is offered only where the AI can actually run — `preview`
    checks `is_configured()` before it prices anything, so the whole start
    surface needs a key present to be under test at all. The one test that
    cares about the dark deploy clears it back."""
    settings.ANTHROPIC_API_KEY = "test-key-not-used"


@pytest.fixture
def student():
    return User.objects.create_user(email="ap-start@example.com", password="x")


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


def dying(text, *, model=""):
    raise autopilot.AutopilotError(RuntimeError("api down"))


def poor(settings):
    """One credit a day — ten rows at the default rate."""
    settings.CREDIT_PLANS = {
        "free": {"monthly_grant": 1, "message_cost": 1, "daily_burst": 1}
    }


def broke(settings):
    settings.CREDIT_PLANS = {
        "free": {"monthly_grant": 0, "message_cost": 1, "daily_burst": 0}
    }


# --------------------------------------------------------------------------- #
# One tap, one run
# --------------------------------------------------------------------------- #
def test_the_button_starts_exactly_one_run(client, student, firm):
    make_proposal(student, firm)
    client.force_login(student)
    resp = client.post(reverse("crm:autopilot_start"))
    assert resp.status_code == 200

    runs = list(AutopilotRun.all_objects.filter(user=student))
    assert len(runs) == 1
    run = runs[0]
    # QUEUED, not decided: the request did not wait on the model, and no
    # model call has happened yet.
    assert run.status == AutopilotRun.STATUS_QUEUED
    assert run.llm_calls == 0 and run.credits_spent == 0
    assert not AutopilotDecision.all_objects.filter(user=student).exists()
    assert not CreditLedger.all_objects.filter(
        user=student, kind=CreditLedger.KIND_SPEND_AUTOPILOT
    ).exists()


def test_a_second_tap_while_running_is_refused(client, student, firm):
    make_proposal(student, firm)
    client.force_login(student)
    client.post(reverse("crm:autopilot_start"))
    client.post(reverse("crm:autopilot_start"))
    client.post(reverse("crm:autopilot_start"))
    assert AutopilotRun.all_objects.filter(user=student).count() == 1

    # And once it is claimed and RUNNING, still refused — "active" is both
    # states, not just the queued one.
    run = AutopilotRun.all_objects.get(user=student)
    assert autopilot.claim_run(run) is True
    outcome, existing = autopilot.start_run(student)
    assert outcome == autopilot.ALREADY_RUNNING and existing.pk == run.pk
    assert AutopilotRun.all_objects.filter(user=student).count() == 1


def test_the_database_refuses_a_second_active_row(student, firm):
    """The guard is a constraint, not a read-then-write — so it holds even
    when two requests both see an empty table."""
    from django.db import IntegrityError

    make_proposal(student, firm)
    AutopilotRun.all_objects.create(
        user=student, status=AutopilotRun.STATUS_QUEUED
    )
    with pytest.raises(IntegrityError):
        AutopilotRun.all_objects.create(
            user=student, status=AutopilotRun.STATUS_RUNNING
        )


def test_only_one_worker_can_claim_a_queued_run(student, firm):
    make_proposal(student, firm)
    _, run = autopilot.start_run(student)
    twin = AutopilotRun.all_objects.get(pk=run.pk)
    assert autopilot.claim_run(run) is True
    assert autopilot.claim_run(twin) is False


def test_a_finished_run_frees_the_slot(student, firm):
    make_proposal(student, firm)
    _, run = autopilot.start_run(student)
    autopilot.claim_run(run)
    autopilot.execute_run(run, decide=accepting)
    make_proposal(student, firm, 7)
    outcome, second = autopilot.start_run(student)
    assert outcome == autopilot.STARTED and second.pk != run.pk


# --------------------------------------------------------------------------- #
# Cost, disclosed before the tap and refused honestly
# --------------------------------------------------------------------------- #
def test_the_price_is_known_before_the_tap(student, firm):
    for i in range(25):
        make_proposal(student, firm, i)
    look = autopilot.preview(student)
    assert look.candidates == 25 and look.decidable == 25
    assert look.credits == 3  # 25 rows at 10/credit, rounded up
    assert not look.blocked


def test_the_strip_shows_the_price_on_today(client, student, firm):
    for i in range(25):
        make_proposal(student, firm, i)
    client.force_login(student)
    body = client.get(reverse("crm:week")).content.decode()
    assert "Run Autopilot" in body
    assert "Autopilot can read 25 cards" in body
    assert "About 3 credits" in body


def test_an_unaffordable_run_is_refused_before_any_model_call(
    client, student, firm, settings
):
    broke(settings)
    for i in range(4):
        make_proposal(student, firm, i)

    look = autopilot.preview(student)
    assert look.blocked == autopilot.INSUFFICIENT_CREDITS
    assert look.decidable == 0

    calls = []

    def spy(text, *, model=""):
        calls.append(text)
        return accepting(text)

    outcome, run = autopilot.start_run(student)
    assert outcome == autopilot.INSUFFICIENT_CREDITS and run is None
    assert AutopilotRun.all_objects.count() == 0
    assert calls == []

    client.force_login(student)
    body = client.get(reverse("crm:week")).content.decode()
    assert "Run Autopilot" not in body


def test_a_dark_deploy_offers_no_button(client, student, firm, settings):
    """No key, no control — a button that cannot work is worse than none."""
    settings.ANTHROPIC_API_KEY = ""
    make_proposal(student, firm)
    assert autopilot.preview(student).blocked == autopilot.UNCONFIGURED
    outcome, run = autopilot.start_run(student)
    assert outcome == autopilot.UNCONFIGURED and run is None
    assert AutopilotRun.all_objects.count() == 0
    client.force_login(student)
    assert "Run Autopilot" not in client.get(reverse("crm:week")).content.decode()


def test_a_partial_budget_says_how_many_of_how_many(client, student, firm, settings):
    poor(settings)
    for i in range(12):
        make_proposal(student, firm, i)
    look = autopilot.preview(student)
    assert look.candidates == 12 and look.decidable == 10 and look.clamped
    client.force_login(student)
    body = client.get(reverse("crm:week")).content.decode()
    assert "Autopilot can read 10 of 12 cards" in body


# --------------------------------------------------------------------------- #
# Nothing to do is a state, not an error
# --------------------------------------------------------------------------- #
def test_zero_proposals_starts_no_run_and_spends_nothing(client, student):
    look = autopilot.preview(student)
    assert look.blocked == autopilot.NOTHING_TO_DECIDE
    assert autopilot.today_state(student).phase == autopilot.TodayState.NOTHING

    outcome, run = autopilot.start_run(student)
    assert outcome == autopilot.NOTHING_TO_DECIDE and run is None
    assert AutopilotRun.all_objects.count() == 0
    assert not CreditLedger.all_objects.filter(
        user=student, kind=CreditLedger.KIND_SPEND_AUTOPILOT
    ).exists()

    client.force_login(student)
    body = client.get(reverse("crm:week")).content.decode()
    assert "Run Autopilot" not in body
    # And no error, no spinner, no pitch — the ordinary day reads as the
    # ordinary day.
    assert "Autopilot is reading" not in body


def test_rows_already_decided_are_not_a_second_invitation_to_spend(
    client, student, firm
):
    p = make_proposal(student, firm)
    autopilot.run_autopilot(student, decide=lambda t, model="": (
        "needs_review", 0.9, t.splitlines()[0], "your call"
    ))
    p.refresh_from_db()
    assert p.status == ContactProposal.STATUS_PENDING  # escalated, still a card

    look = autopilot.preview(student)
    assert look.pending == 1 and look.candidates == 0
    assert autopilot.today_state(student).phase == autopilot.TodayState.REVIEWED_EMPTY

    client.force_login(student)
    body = client.get(reverse("crm:week")).content.decode()
    assert "Run Autopilot" not in body
    assert "left it for you" in body


# --------------------------------------------------------------------------- #
# Failure is visible; it never looks like thinking
# --------------------------------------------------------------------------- #
def test_a_failed_run_surfaces_as_failed(client, student, firm):
    make_proposal(student, firm)
    _, run = autopilot.start_run(student)
    autopilot.claim_run(run)
    autopilot.execute_run(run, decide=dying)

    run.refresh_from_db()
    assert run.status == AutopilotRun.STATUS_FAILED
    assert run.failure_reason
    assert Contact.all_objects.filter(user=student).count() == 0

    state = autopilot.today_state(student)
    assert state.phase == autopilot.TodayState.FAILED

    client.force_login(student)
    body = client.get(reverse("crm:week")).content.decode()
    assert "stopped before it finished" in body or run.failure_reason in body
    assert "Autopilot is reading" not in body
    assert "Try again" in body

    # And the slot is free — a failure is not a lock.
    outcome, again = autopilot.start_run(student)
    assert outcome == autopilot.STARTED and again.pk != run.pk


def test_an_abandoned_run_is_reclaimed_not_left_thinking(student, firm):
    from datetime import timedelta

    make_proposal(student, firm)
    _, run = autopilot.start_run(student)
    autopilot.claim_run(run)
    # The worker's process dies here. Nothing will ever finish this row.
    AutopilotRun.all_objects.filter(pk=run.pk).update(
        started_at=timezone.now() - timedelta(seconds=autopilot.STALE_RUN_AFTER + 60)
    )
    assert autopilot.reap_stale_runs() == 1
    run.refresh_from_db()
    assert run.status == AutopilotRun.STATUS_FAILED and run.failure_reason
    # The student is not locked out behind a corpse.
    outcome, _ = autopilot.start_run(student)
    assert outcome == autopilot.STARTED


def test_a_queued_run_is_never_reaped(student, firm):
    """`queued` is waiting, not abandoned — the next tick is what it waits
    FOR, and reaping it would fail runs on a deploy that hasn't ticked."""
    from datetime import timedelta

    make_proposal(student, firm)
    _, run = autopilot.start_run(student)
    AutopilotRun.all_objects.filter(pk=run.pk).update(
        created=timezone.now() - timedelta(days=1)
    )
    assert autopilot.reap_stale_runs() == 0
    run.refresh_from_db()
    assert run.status == AutopilotRun.STATUS_QUEUED


def test_a_run_whose_cards_vanished_ends_reviewed_not_running(student, firm):
    """The student worked the cards himself while the run sat in the
    queue. There is nothing to decide — but the row must still stop."""
    p = make_proposal(student, firm)
    _, run = autopilot.start_run(student)
    autopilot.claim_run(run)
    p.status = ContactProposal.STATUS_DISMISSED
    p.save(update_fields=["status"])
    autopilot.execute_run(run, decide=accepting)
    run.refresh_from_db()
    assert run.status == AutopilotRun.STATUS_REVIEWED
    assert run.accepts == 0 and run.credits_spent == 0


# --------------------------------------------------------------------------- #
# Starting is not applying — the line the whole feature stands on
# --------------------------------------------------------------------------- #
def test_starting_a_run_applies_nothing(client, student, firm):
    make_proposal(student, firm)
    client.force_login(student)
    client.post(reverse("crm:autopilot_start"))
    run = AutopilotRun.all_objects.get(user=student)
    autopilot.claim_run(run)
    autopilot.execute_run(run, decide=accepting)

    run.refresh_from_db()
    assert run.status == AutopilotRun.STATUS_REVIEWED
    assert run.accepts == 1
    # Decided, and not one thing in the CRM has moved.
    assert Contact.all_objects.filter(user=student).count() == 0
    assert ContactProposal.all_objects.get(user=student).status == (
        ContactProposal.STATUS_PENDING
    )

    # It takes the OTHER tap, the one that already existed, to write anything.
    resp = client.post(reverse("crm:autopilot_apply", args=[run.pk]))
    assert resp.status_code == 200
    assert Contact.all_objects.filter(user=student).count() == 1
    run.refresh_from_db()
    assert run.status == AutopilotRun.STATUS_APPLIED


def test_the_start_endpoint_rejects_a_get(client, student, firm):
    make_proposal(student, firm)
    client.force_login(student)
    assert client.get(reverse("crm:autopilot_start")).status_code == 405
    assert AutopilotRun.all_objects.count() == 0


def test_the_poll_target_shows_the_working_state_then_the_batch(
    client, student, firm
):
    make_proposal(student, firm)
    client.force_login(student)
    client.post(reverse("crm:autopilot_start"))

    body = client.get(reverse("crm:autopilot_state")).content.decode()
    assert "Autopilot is queued" in body
    assert "autopilot/state/" in body  # still polling

    run = AutopilotRun.all_objects.get(user=student)
    autopilot.claim_run(run)
    autopilot.execute_run(run, decide=accepting)

    body = client.get(reverse("crm:autopilot_state")).content.decode()
    assert "Add all 1" in body
    assert "autopilot/state/" not in body  # the poll stopped by being replaced


# --------------------------------------------------------------------------- #
# The worker
# --------------------------------------------------------------------------- #
def test_the_worker_decides_what_the_button_queued(client, student, firm, monkeypatch):
    from io import StringIO

    from django.core.management import call_command

    make_proposal(student, firm)
    monkeypatch.setattr(autopilot, "_decide_with_model", accepting)
    client.force_login(student)
    client.post(reverse("crm:autopilot_start"))

    out = StringIO()
    call_command("capture_autopilot_worker", stdout=out)
    run = AutopilotRun.all_objects.get(user=student)
    assert run.status == AutopilotRun.STATUS_REVIEWED and run.accepts == 1
    assert "1 ready to add" in out.getvalue()

    # A second tick has nothing to do and says so.
    out = StringIO()
    call_command("capture_autopilot_worker", stdout=out)
    assert "Nothing queued." in out.getvalue()


def test_an_application_event_alone_is_still_worth_a_run(student, firm):
    """Zero proposals but a pending application update: free to decide
    (deterministic), so the run is offered rather than refused."""
    from directory.models import Opportunity

    opp = Opportunity.objects.create(
        firm=firm, title="Summer Analyst", status="open",
        url="https://allenco.example/sa",
    )
    ApplicationEvent.all_objects.create(
        user=student, opportunity=opp, firm=firm, firm_text=firm.name,
        event_type=ApplicationEvent.APPLIED, target_status="submitted",
        evidence="Thank you for applying", detected_by="phrase",
        match_reason="ats",
    )
    look = autopilot.preview(student)
    assert look.candidates == 0 and look.free_rows == 1
    assert look.blocked == "" and look.credits == 0
    outcome, run = autopilot.start_run(student)
    assert outcome == autopilot.STARTED
    autopilot.claim_run(run)
    autopilot.execute_run(run, decide=accepting)
    run.refresh_from_db()
    assert run.status == AutopilotRun.STATUS_REVIEWED and run.accepts == 1
    assert run.credits_spent == 0
