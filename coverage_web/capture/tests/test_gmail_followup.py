"""Follow-up capture (capture/gmail.py's `_follow_up_action`).

The defect these tests pin: kind `follow_up` was never logged from any
capture path — `apply_findings` dedup'd ALL outbound per contact ("have I
ever sent a first note"), so the cadence engine's branch 6 saw `outbound`
stuck at 1 forever, its "follow up" prompt re-rendered indefinitely, and
`max_cold_touches` (2) was unreachable: a cold contact could never earn the
park suggestion through capture. The founder's real follow-ups are
same-thread "just following up" replies sent from Gmail (measured on his
live mailbox, 2026-08-27), so capture is exactly where they must land.

Both directions matter as much as each other: a real second send must log
`follow_up` once, and everything that is NOT a second send — the same
message re-seen by a rescan, the daily sync's thread summary re-emitted
tomorrow, a hand-logged duplicate, a mail-merge wave — must keep logging
nothing.

``transaction=True`` for the same reason test_gmail.py needs it: applying a
finding calls `crm.services.log_touch`, which opens its own psycopg
connection.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone

from capture.gmail import apply_findings
from coverage_domain.cadence import due_actions
from crm.models import Campaign, Contact, Touch

pytestmark = pytest.mark.django_db(transaction=True)

User = get_user_model()


@pytest.fixture
def student(db):
    return User.objects.create_user(
        email="followup-student@example.com", password="x"
    )


@pytest.fixture
def contact(student):
    return Contact.all_objects.create(
        user=student, name="Jane Banker", email="jane@bank.example",
        source="manual",
    )


def finding(**over):
    base = {
        "name": "Jane Banker",
        "email": "jane@bank.example",
        "found": True,
        "bounced": False,
        "outreach_sent": True,
        "replied": False,
        "chat_status": "none",
        "evidence": "Sent: checking in",
        "thread_id": "t-1",
    }
    base.update(over)
    return base


def outbound_kinds(user, contact):
    return list(
        Touch.objects.for_user(user)
        .filter(contact=contact, kind__in=("outreach", "follow_up"))
        .order_by("ts")
        .values_list("kind", flat=True)
    )


def iso(dt):
    return dt.isoformat()


def _first_note(student, contact, *, days_ago=30, thread="t-1"):
    """Log the first outreach the way the live path does: a dated finding."""
    when = timezone.now() - timedelta(days=days_ago)
    apply_findings(student, [finding(thread_id=thread, occurred_at=iso(when))])
    assert outbound_kinds(student, contact) == ["outreach"]
    return when


# --------------------------------------------------------------------------- #
# Real second sends log follow_up
# --------------------------------------------------------------------------- #

def test_same_thread_bump_logs_follow_up(student, contact):
    """The founder's actual follow-up shape: a dated 'just following up'
    reply on the SAME thread, well after the first note."""
    _first_note(student, contact, days_ago=30, thread="t-1")
    result = apply_findings(
        student,
        [finding(thread_id="t-1", occurred_at=iso(timezone.now()),
                 evidence="Sent: just following up")],
    )
    assert result.follow_ups_logged == 1
    assert outbound_kinds(student, contact) == ["outreach", "follow_up"]


def test_new_thread_follow_up_logs_follow_up(student, contact):
    _first_note(student, contact, days_ago=30, thread="t-1")
    result = apply_findings(
        student,
        [finding(thread_id="t-2", occurred_at=iso(timezone.now()))],
    )
    assert result.follow_ups_logged == 1
    assert outbound_kinds(student, contact) == ["outreach", "follow_up"]


def test_follow_up_is_outbound_and_moves_nothing(student, contact):
    """`follow_up`'s TOUCH_TRANSITIONS entry is (None, None): the contact
    stays cold/no_reply — the send is counted, not rewarded."""
    _first_note(student, contact, days_ago=30)
    apply_findings(
        student, [finding(thread_id="t-1", occurred_at=iso(timezone.now()))]
    )
    contact.refresh_from_db()
    assert contact.warmth == "cold"
    assert contact.thread_state == "no_reply"


def test_follow_up_makes_park_reachable(student, contact):
    """The point of the whole fix: with the follow-up on record, branch 6
    counts outbound=2 and the park suggestion becomes reachable once the
    silence clears the park window — instead of 'follow up' forever."""
    _first_note(student, contact, days_ago=60, thread="t-1")
    bump = timezone.now() - timedelta(days=30)
    apply_findings(student, [finding(thread_id="t-1", occurred_at=iso(bump))])

    touches = [
        {"contact_id": contact.id, "ts": t.ts, "kind": t.kind}
        for t in Touch.objects.for_user(student).filter(contact=contact)
    ]
    contacts = [{
        "id": contact.id, "firm_id": None, "warmth": "cold",
        "thread_state": "no_reply",
    }]
    actions = due_actions(contacts, touches, as_of=timezone.now())
    assert [a["action"] for a in actions] == ["park"]
    assert actions[0]["ctx"]["outbound"] == 2


# --------------------------------------------------------------------------- #
# Everything that is NOT a second send stays silent
# --------------------------------------------------------------------------- #

def test_rescan_of_the_original_send_does_not_double_log(student, contact):
    """A rescan re-classifies the SAME sent message: same thread, same
    occurred_at. The recorded outreach touch sits at that exact instant, so
    the window guard recognises the send as already on record."""
    when = _first_note(student, contact, days_ago=30, thread="t-1")
    result = apply_findings(
        student, [finding(thread_id="t-1", occurred_at=iso(when))]
    )
    assert result.follow_ups_logged == 0
    assert result.skipped_already_logged == 1
    assert outbound_kinds(student, contact) == ["outreach"]


def test_rescan_of_a_logged_follow_up_does_not_double_log(student, contact):
    _first_note(student, contact, days_ago=30, thread="t-1")
    bump = timezone.now() - timedelta(days=10)
    apply_findings(student, [finding(thread_id="t-1", occurred_at=iso(bump))])
    result = apply_findings(
        student, [finding(thread_id="t-1", occurred_at=iso(bump))]
    )
    assert result.follow_ups_logged == 0
    assert outbound_kinds(student, contact) == ["outreach", "follow_up"]


def test_daily_sync_thread_summary_never_relogs(student, contact):
    """The daily agent-run sync re-emits an undated, thread-level
    outreach finding every day the thread stays in the search window. The
    thread's marker is already on the outreach touch, and an undated
    summary can never prove a NEW send — so it must stay a skip forever,
    however old the first note is."""
    Touch.objects.for_user(student)  # touch the manager for scoping
    apply_findings(student, [finding(thread_id="t-1")])  # undated first note
    Touch.objects.for_user(student).filter(contact=contact).update(
        ts=timezone.now() - timedelta(days=45)
    )
    for _ in range(3):
        result = apply_findings(student, [finding(thread_id="t-1")])
        assert result.follow_ups_logged == 0
    assert outbound_kinds(student, contact) == ["outreach"]


def test_threadless_undated_finding_never_logs_follow_up(student, contact):
    """No thread to marker-guard and no message time to window-anchor:
    nothing could stop a weekly re-log, so this shape keeps the old skip."""
    apply_findings(student, [finding(thread_id="")])
    Touch.objects.for_user(student).filter(contact=contact).update(
        ts=timezone.now() - timedelta(days=45)
    )
    result = apply_findings(student, [finding(thread_id="")])
    assert result.follow_ups_logged == 0
    assert outbound_kinds(student, contact) == ["outreach"]


def test_hand_logged_send_near_capture_is_not_double_counted(student, contact):
    """The user logs the send by hand (no thread marker), and the live
    path then captures the actual sent mail minutes later: one event, one
    touch. The ±window guard is what recognises them as the same send."""
    from crm import services as crm_services

    crm_services.log_touch(student.id, contact.id, "outreach", "email", None)
    result = apply_findings(
        student, [finding(thread_id="t-9", occurred_at=iso(timezone.now()))]
    )
    assert result.follow_ups_logged == 0
    assert outbound_kinds(student, contact) == ["outreach"]


def test_quick_second_note_inside_the_window_is_suppressed(student, contact):
    """Accepted cost, pinned so it stays a decision rather than drifting:
    a second note inside NO_THREAD_DEDUP_DAYS of the first is treated as
    the same send. The cadence's own follow-up window is 6 business days,
    so a real follow-up almost always clears this."""
    _first_note(student, contact, days_ago=3, thread="t-1")
    result = apply_findings(
        student, [finding(thread_id="t-2", occurred_at=iso(timezone.now()))]
    )
    assert result.follow_ups_logged == 0


def test_replied_thread_summary_never_logs_follow_up(student, contact):
    """A daily-sync summary carrying outreach_sent AND replied means the
    thread got answered — there is nothing to follow up."""
    _first_note(student, contact, days_ago=30, thread="t-1")
    result = apply_findings(
        student,
        [finding(thread_id="t-1", replied=True,
                 occurred_at=iso(timezone.now()))],
    )
    assert result.follow_ups_logged == 0
    assert "follow_up" not in outbound_kinds(student, contact)


# --------------------------------------------------------------------------- #
# The merge guard: a blast's second wave is not N follow-ups
# --------------------------------------------------------------------------- #

def _merge_contacts(student, n=5):
    return [
        Contact.all_objects.create(
            user=student, name=f"Alum {i}", email=f"alum{i}@firm.example",
            source="manual",
        )
        for i in range(n)
    ]


def test_batch_fanout_wave_logs_no_follow_ups(student):
    """Five recipients sharing one normalized subject in one batch is a
    mail merge (discovery.MERGE_RECIPIENT_LIMIT), and a merge wave must
    not march five contacts toward the park threshold as 'follow-ups'."""
    people = _merge_contacts(student, 5)
    first = timezone.now() - timedelta(days=40)
    for i, c in enumerate(people):
        apply_findings(student, [finding(
            name=c.name, email=c.email, thread_id=f"w1-{i}",
            subject=f"Fall Panel Invitation {i}", occurred_at=iso(first),
        )])
    wave2 = [
        finding(
            name=c.name, email=c.email, thread_id=f"w2-{i}",
            subject="Fall Panel Follow Up", occurred_at=iso(timezone.now()),
        )
        for i, c in enumerate(people)
    ]
    result = apply_findings(student, wave2)
    assert result.follow_ups_logged == 0
    for c in people:
        assert outbound_kinds(student, c) == ["outreach"]


def test_detected_nonrecruiting_campaign_wave_is_refused(student, contact):
    """A single wave-2 send whose subject signature matches a DETECTED
    campaign the user has not called their recruiting — refused even
    without in-batch fan-out (the slow-dripped merge case)."""
    from crm.campaigns import normalize_subject

    _first_note(student, contact, days_ago=40, thread="t-1")
    now = timezone.now()
    Campaign.all_objects.create(
        user=student,
        signature=normalize_subject("Fall Panel Follow Up"),
        label="Fall Panel Follow Up",
        kind=Campaign.KIND_OTHER,
        first_sent=now, last_sent=now,
    )
    result = apply_findings(student, [finding(
        thread_id="t-2", subject="Fall Panel Follow Up",
        occurred_at=iso(timezone.now()),
    )])
    assert result.follow_ups_logged == 0
    assert outbound_kinds(student, contact) == ["outreach"]


def test_recruiting_classified_campaign_wave_counts(student, contact):
    """The user's explicit word cuts the other way too: a campaign they
    classified as their own recruiting IS their outreach, so its second
    wave logs."""
    from crm.campaigns import normalize_subject

    _first_note(student, contact, days_ago=40, thread="t-1")
    now = timezone.now()
    Campaign.all_objects.create(
        user=student,
        signature=normalize_subject("USC Coffee Chats Follow Up"),
        label="USC Coffee Chats Follow Up",
        kind=Campaign.KIND_RECRUITING,
        first_sent=now, last_sent=now,
    )
    result = apply_findings(student, [finding(
        thread_id="t-2", subject="USC Coffee Chats Follow Up",
        occurred_at=iso(timezone.now()),
    )])
    assert result.follow_ups_logged == 1


# --------------------------------------------------------------------------- #
# First notes are untouched
# --------------------------------------------------------------------------- #

def test_first_note_still_logs_outreach(student, contact):
    result = apply_findings(
        student, [finding(occurred_at=iso(timezone.now()))]
    )
    assert result.outreach_logged == 1
    assert result.follow_ups_logged == 0
    assert outbound_kinds(student, contact) == ["outreach"]
