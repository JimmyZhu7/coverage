"""Today's SECTION order, pinned.

The order of the cockpit's sections is a deliberate severity ranking, written
out in full in the leading comment of `templates/crm/_cockpit.html`. It was
not always: the page accumulated in the order its features were built, which
put the day's actual plan sixth — behind the board, a debrief lane, a
proposals queue and a pitch for a paid feature.

That is exactly the kind of thing that regresses silently. A section added at
the bottom of the file, or moved to sit next to a related one, breaks no test
and no render; it just quietly buries the work again. These tests fail when it
happens.

Every assertion here is about ORDER ONLY. Whether a section renders at all is
each section's own business and is pinned by test_today.py /
test_today_seeds.py; nothing here asserts a render condition, and nothing here
should be relaxed to make a section appear or disappear.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse
from django.utils import timezone

from capture.models import ContactProposal
from crm.models import CalendarEvent, Contact, Touch, UserFirm
from directory.models import Firm, FirmDate

pytestmark = pytest.mark.django_db(transaction=True)

COCKPIT = Path(__file__).resolve().parents[2] / "templates" / "crm" / "_cockpit.html"


def _user(email):
    return get_user_model().objects.create_user(email=email, password="pw12345!")


def _contact(user, name, **kw):
    kw.setdefault("school_affiliation", True)
    return Contact.all_objects.create(user=user, name=name, **kw)


def _touch(user, contact, kind, *, days_ago=0):
    return Touch.all_objects.create(
        user=user, contact=contact, kind=kind, channel="email",
        ts=timezone.now() - timedelta(days=days_ago),
    )


def _body(client, user):
    client.force_login(user)
    return client.get(reverse("crm:week")).content.decode()


def _at(body, marker):
    """Where `marker` starts, asserting it is actually on the page — an
    ordering test that silently passes because neither side rendered is
    worse than no test."""
    assert marker in body, f"{marker!r} did not render; the fixture is wrong"
    return body.index(marker)


# Markers: a substring unique to one section of the cockpit.
PREP = 'class="lane lane-prep"'
DEBRIEF = 'class="lane lane-debrief"'
PROPOSALS = 'class="lane lane-proposals"'
CRITICAL = 'class="lane lane-critical"'
COLD = 'class="lane lane-cold"'
HELD = 'class="upnext"'
STALE = 'class="lane lane-stale"'


@pytest.fixture
def loud(client):
    """A page with something in every section that competes for the top.

    This is the shape the reorder was for. It is deliberately not a shape any
    real account hits often — on the founder's own live account (2026-08-28,
    before the board was removed) only the plan, the board and "waiting on
    reply" render at all, which is why the bad order survived so long
    unnoticed.
    """
    user = _user("order-loud@example.com")
    today = timezone.localdate()

    firm = Firm.objects.create(name="Order Capital", slug="order-capital")
    UserFirm.all_objects.create(user=user, firm=firm, tier=1)
    FirmDate.objects.create(
        firm=firm, cycle="sa2028", region="us", event_kind="app_close",
        date=today + timedelta(days=3), confidence=1.0,
    )

    # A CLASS_CRITICAL action: they replied, nothing since.
    _touch(user, _contact(user, "Critical Person", firm=firm, warmth="replied"),
           "reply_received", days_ago=30)

    # Enough cold bodies that the cap bites and "Up next" renders.
    for i in range(12):
        _touch(user, _contact(user, f"Cold {i}", firm=firm, warmth="cold"),
               "email", days_ago=40 + i)

    # A chat nobody wrote up.
    _touch(user, _contact(user, "Chatted Person", firm=firm, warmth="chatted"),
           "chat", days_ago=3)

    # A chat on the calendar today. Anchored to local NOON today rather than
    # "now + 3h": `_schedule`'s window starts at local midnight, so any time
    # today satisfies it, but `now + 3h` crosses into tomorrow (in whatever
    # zone is active for this timezone-less test user, i.e. UTC) for roughly
    # the last three hours of every UTC day — which turned "is_today" false
    # and silently dropped the whole prep lane, exactly the kind of
    # local-date bug this suite exists to catch elsewhere.
    CalendarEvent.all_objects.create(
        user=user, contact=_contact(user, "Prep Person", firm=firm, warmth="chatted"),
        title="Chat with Prep Person", kind="chat", thread_id="t-order",
        starts_at=timezone.localtime(timezone.now()).replace(
            hour=12, minute=0, second=0, microsecond=0
        ),
    )

    # An inbox match waiting on a yes or no.
    ContactProposal.all_objects.create(
        user=user, name="Proposed Person", email="proposed@order.example",
        firm=firm, status=ContactProposal.STATUS_PENDING,
        evidence_kind="reply", role_hint="Analyst",
    )
    return _body(client, user)


def test_the_plan_leads_everything_except_a_chat_happening_today(loud):
    """THE BUG THIS FILE EXISTS FOR. Measured before the fix, on this exact
    fixture, the rendered order was: board, chat prep, debriefs, proposals,
    THEN the critical lane. A student opening Today to find out what was
    urgent scrolled past four sections — one of them a review queue, one of
    them an ad — to reach the day's overdue work.

    Only chat prep may outrank it, and only because a conversation at 2pm is
    the one clock on this page that cannot be re-run."""
    plan = _at(loud, CRITICAL)
    assert _at(loud, PREP) < plan, "a chat today still leads; that is rung 1"
    for later in (DEBRIEF, PROPOSALS):
        assert plan < _at(loud, later), f"{later} must not outrank the plan"


def test_the_inbox_family_is_reachable_without_a_scroll(loud):
    """"Found in your inbox" above the debrief lane, 2026-08-29.

    This inverts what rung order alone would give (a decaying debrief is
    rung 5, a proposal waiting on nobody's clock is rung 6) and the
    inversion is the point. Ranking purely by "whose clock is running" had
    put the two AI-fed lanes — what the mailbox proposed, and what an ATS
    said your applications did — ninth on the page, under the debrief lane
    and the still-open lane. On an ordinary day that is far enough down
    that the founder reported both as missing from Today outright.

    A section with no deadline still has to be FINDABLE. A review queue
    nobody scrolls to is a review queue that does not exist, so the
    severity ladder yields here to reachability. The autopilot OFFER did
    not move with the cards: it stays at the foot of the family it is an
    offer about (pinned by
    `test_the_autopilot_pitch_sits_below_the_cards_it_is_an_offer_about`)."""
    assert _at(loud, PROPOSALS) < _at(loud, DEBRIEF)


def test_a_fired_clock_outranks_one_that_has_not_fired(client):
    """"Still open" above "Up next". Both are work the plan is not doing
    today, and that is where the resemblance stops: still-open cards are
    criticals that already blew their window and that the plan has stopped
    budgeting for, while held cards have a morning coming and are waiting on
    the cap alone. Two three-week-old unanswered questions must not sit
    underneath a collapsed accordion."""
    user = _user("order-stale@example.com")
    firm = Firm.objects.create(name="Order Stale", slug="order-stale")
    UserFirm.all_objects.create(user=user, firm=firm, tier=1)
    for i in range(2):
        _touch(user, _contact(user, f"Stuck {i}", firm=firm, warmth="chatted",
                              thread_state="chat_scheduled"),
               "email", days_ago=60)
    for i in range(12):
        _touch(user, _contact(user, f"Cold {i}", firm=firm, warmth="cold"),
               "email", days_ago=40 + i)

    body = _body(client, user)
    assert _at(body, COLD) < _at(body, STALE) < _at(body, HELD)


def test_the_done_for_today_summary_lists_its_sections_in_render_order(client):
    """The "Done for today" panel names what is left — still open, then up
    next, then gone quiet — and those three sections render in that order
    below it. A summary that lists its own subjects in a different order than
    it renders them makes the reader hunt."""
    src = COCKPIT.read_text()
    panel = src.index('class="empty empty--done"')
    tail = src[panel:panel + 1400]
    assert (tail.index("still open below")
            < tail.index("pacing out over the coming days")
            < tail.index("gone quiet")), (
        "the summary's clauses drifted out of the order the sections render in"
    )


def test_the_autopilot_pitch_sits_below_the_cards_it_is_an_offer_about():
    """Rung 8, the bottom. The `idle` strip is the one thing on Today that
    reports nothing about the student's own pipeline — it says a paid feature
    exists and prices it. It rendered SIXTH from the top, above the day's
    overdue work.

    A SOURCE assertion rather than a rendered one: reaching `phase == "idle"`
    needs a configured AI backend and a funded credit ledger, and the thing
    worth pinning is structural — which side of the proposals lane the block
    is written on. The four OTHER phases stay above those cards on purpose;
    they are narration about the cards, and a status line reads correctly
    before its subject while an offer does not."""
    src = COCKPIT.read_text()
    pitch = src.index('{% if autopilot_state.phase == "idle" %}')
    status = src.index('{% if autopilot_state.phase == "active" %}')
    proposals = src.index(PROPOSALS)
    assert status < proposals < pitch, (
        "the Run Autopilot pitch is back above the cards it is an offer about"
    )


def test_the_pitch_is_still_exactly_one_strip_however_the_page_renders():
    """The idle branch was lifted out of the phase chain so it could move.
    That is only safe because every branch tests the same single value, so at
    most one strip anywhere on the page can fire. If a future branch tests
    something else as well, this stops being true and the page can render two
    autopilot strips at once."""
    src = COCKPIT.read_text()
    branches = [
        line.strip() for line in src.splitlines()
        if "autopilot_state.phase ==" in line
    ]
    assert branches, "the phase chain vanished"
    for line in branches:
        assert line.startswith(("{% if autopilot_state.phase ==",
                               "{% elif autopilot_state.phase ==")), (
            f"a phase branch grew a second condition and can now co-render: {line}"
        )


# NOT PINNED HERE: the foot of the page — waiting on reply, the bench, the
# gone-quiet strip, new at your firms. The reorder did not move any of them
# relative to each other, and they were already correctly last: none of it is
# work, and nothing that is not work outranks something that is. A test there
# would pin a decision this change never made.
