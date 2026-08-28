"""My Applications' deadline lenses, and the shared Closing Soon window.

The reason this file exists: two surfaces now claim to count the same thing.
The Today dashboard has a "closing in 10 days" stat card; My Applications has a
Closing Soon section. If those definitions ever drift, the product has told one
student two different things about the same set of dates.

`directory/deadlines.py` holds the single definition. `crm/views.py` still
inlines its own copy of the arithmetic (it is outside this change's ownership),
so the first test below pins the two against each other on identical fixtures —
the day someone edits one and not the other, this fails instead of shipping.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.urls import reverse
from django.utils import timezone

from analytics.models import UserOpportunity
from crm.views import _dashboard_context
from directory.classify import TARGET_BUCKETS
from directory.deadlines import (
    CLOSING_SOON_DAYS, closing_soon_filter, closing_soon_window, is_closing_soon,
)
from directory.models import Firm, Opportunity

from .test_tracking import _user

TODAY = timezone.localdate()


def _opp(firm, n, *, days=None, bucket="internship"):
    """One open campus role, `days` from today (None = rolling/no deadline)."""
    return Opportunity.objects.create(
        firm=firm, url=f"https://x/{n}", title=f"Summer Analyst {n}",
        bucket=bucket, status="open",
        deadline=None if days is None else TODAY + timedelta(days=days),
    )


@pytest.fixture
def board(db):
    """Deadlines placed on every edge of the window that could be got wrong:
    yesterday (out), today (in), the last day in, the first day out, far
    future (out), and a rolling role (never in)."""
    firm = Firm.objects.create(name="Evercore", slug="evercore")
    return {
        "past": _opp(firm, 1, days=-1),
        "today": _opp(firm, 2, days=0),
        "last_in": _opp(firm, 3, days=CLOSING_SOON_DAYS - 1),   # day 9
        "first_out": _opp(firm, 4, days=CLOSING_SOON_DAYS),     # day 10
        "far": _opp(firm, 5, days=90),
        "rolling": _opp(firm, 6, days=None),
    }


# ---------------------------------------------------------------------------
# The window itself.
# ---------------------------------------------------------------------------

def test_window_is_ten_days_inclusive():
    first, last = closing_soon_window(TODAY)
    assert first == TODAY
    assert (last - first).days == CLOSING_SOON_DAYS - 1 == 9


@pytest.mark.parametrize("days,expected", [
    (-1, False), (0, True), (1, True), (9, True), (10, False), (90, False),
])
def test_is_closing_soon_edges(days, expected):
    assert is_closing_soon(TODAY + timedelta(days=days), today=TODAY) is expected


def test_a_rolling_role_is_never_closing_soon():
    """A null deadline has no urgency signal at all. It must not slip into the
    window, and it must not blow up on the comparison either."""
    assert is_closing_soon(None, today=TODAY) is False


@pytest.mark.django_db
def test_shared_window_agrees_with_the_today_widget(board):
    """The load-bearing test. `crm/views._dashboard_context` computes
    `closing_10` with its own inline arithmetic; `closing_soon_filter` is the
    shared definition. On identical rows they must produce the same number."""
    user = _user()
    campus = Opportunity.objects.filter(status="open", bucket__in=TARGET_BUCKETS)

    shared_ids = set(closing_soon_filter(campus, today=TODAY).values_list("id", flat=True))
    today_widget = _dashboard_context(user)["dash"]["closing_10"]

    # Named rather than counted, so a failure says WHICH edge moved.
    assert shared_ids == {board["today"].id, board["last_in"].id}
    assert len(shared_ids) == today_widget == 2


# ---------------------------------------------------------------------------
# The My Applications lenses.
# ---------------------------------------------------------------------------

def _lens(resp, key):
    return next(l for l in resp.context["lenses"] if l["key"] == key)


def _titles(lens):
    return {i["title"] for i in lens["items"]}


@pytest.fixture
def tracked(client, board):
    """A user tracking every role on the board, so the lenses see the same set
    the Today widget counts."""
    user = _user()
    for o in board.values():
        UserOpportunity.all_objects.create(user=user, opportunity=o)
    client.force_login(user)
    return user


@pytest.mark.django_db
def test_closing_lens_matches_the_shared_window(client, board, tracked):
    resp = client.get(reverse("my_applications"))
    campus = Opportunity.objects.filter(status="open", bucket__in=TARGET_BUCKETS)
    assert len(_lens(resp, "closing")["items"]) == closing_soon_filter(
        campus, today=TODAY
    ).count()
    assert _titles(_lens(resp, "closing")) == {
        board["today"].title, board["last_in"].title,
    }


@pytest.mark.django_db
def test_rolling_lens_excludes_every_dated_role(client, board, tracked):
    """Rolling means "no posted deadline", not "a deadline that is far away".
    A role closing in 90 days is dated and simply not urgent; calling it
    rolling would misstate the posting."""
    rolling = _lens(client.get(reverse("my_applications")), "rolling")
    assert _titles(rolling) == {board["rolling"].title}
    for key in ("past", "today", "last_in", "first_out", "far"):
        assert board[key].title not in _titles(rolling)


@pytest.mark.django_db
def test_a_past_deadline_is_in_neither_lens(client, board, tracked):
    """It is not closing soon (it closed) and it is not rolling (it had a
    date). Silently sweeping it into Rolling would make a dead role look live."""
    resp = client.get(reverse("my_applications"))
    assert board["past"].title not in _titles(_lens(resp, "closing"))
    assert board["past"].title not in _titles(_lens(resp, "rolling"))


# ---------------------------------------------------------------------------
# Done — the terminal state.
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_done_is_its_own_state_not_offer(client, board):
    """Marking a role Done must not go through the Offer stage, and must not
    stamp the funnel: a rejected application never had an offer and was never
    "applied" just because it ended."""
    user = _user()
    o = board["today"]
    client.force_login(user)
    client.post(reverse("track_opportunity", args=[o.id]), {"status": "saved"})
    client.post(reverse("track_opportunity", args=[o.id]), {"status": "closed"})

    uo = UserOpportunity.objects.for_user(user).get(opportunity=o)
    assert uo.applied_status == "closed"
    assert uo.applied_at is None       # Done is not funnel entry
    assert uo.dismissed is False       # and it is NOT the dismissed flag


@pytest.mark.django_db
def test_done_rows_leave_the_lenses_but_stay_counted(client, board):
    """A finished application has no deadline urgency left, so it drops out of
    Closing Soon — but it is still a tracked role and still counted."""
    user = _user()
    client.force_login(user)
    for o in (board["today"], board["rolling"]):
        client.post(reverse("track_opportunity", args=[o.id]), {"status": "saved"})

    resp = client.get(reverse("my_applications"))
    assert len(_lens(resp, "closing")["items"]) == 1
    assert len(_lens(resp, "rolling")["items"]) == 1
    assert resp.context["total"] == resp.context["live_total"] == 2

    client.post(reverse("track_opportunity", args=[board["today"].id]), {"status": "closed"})
    resp = client.get(reverse("my_applications"))
    assert _lens(resp, "closing")["items"] == []
    assert resp.context["total"] == 2        # still tracked
    assert resp.context["live_total"] == 1   # but no longer live
    done = next(s for s in resp.context["stages"] if s["key"] == "closed")
    assert len(done["items"]) == 1


@pytest.mark.django_db
def test_done_can_be_reopened(client, board):
    """Terminal is a label, not a trapdoor — a student who closed a row by
    mistake can move it back and it returns to the lenses."""
    user = _user()
    client.force_login(user)
    o = board["today"]
    client.post(reverse("track_opportunity", args=[o.id]), {"status": "closed"})
    client.post(reverse("track_opportunity", args=[o.id]), {"status": "interview"})
    resp = client.get(reverse("my_applications"))
    assert len(_lens(resp, "closing")["items"]) == 1
    assert _lens(resp, "closing")["items"][0]["stage_label"] == "Interviewing"


# ---------------------------------------------------------------------------
# Overlap — the page must never imply more tracked roles than there are.
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_the_funnel_is_the_partition_and_sums_to_the_total(client, board, tracked):
    """Roles appear twice by design (once in a lens, once in their stage). The
    guarantee is that exactly ONE of those is the count: the funnel stages
    partition the tracked set, so their lengths sum to `total`."""
    resp = client.get(reverse("my_applications"))
    stage_sum = sum(len(s["items"]) for s in resp.context["stages"])
    assert stage_sum == resp.context["total"] == 6

    # The lenses are a SECOND partition, of the live rows, over the same set —
    # never a third pile of roles. Each of the six fixture rows sits in exactly
    # one lens: past -> passed, today + day 9 -> closing, day 10 + day 90 ->
    # later, no deadline -> rolling. Every posting on this board is open, so
    # the fifth lens (rows the FIRM took down, `Opportunity.status`) is empty
    # and is asserted at 0 rather than dropped from the dict — an empty lens
    # still has to be part of the partition for the sum below to mean anything.
    by_key = {lens["key"]: len(lens["items"]) for lens in resp.context["lenses"]}
    assert by_key == {
        "posting_closed": 0, "passed": 1, "closing": 2, "later": 2, "rolling": 1,
    }
    assert sum(by_key.values()) == resp.context["live_total"] == 6
    # Before the residue lenses existed this sum was 3 — the day-10 and
    # day-90 rows were in no lens at all while still being counted in the
    # "of N live" denominator printed beside every lens.


@pytest.mark.django_db
def test_lens_rows_show_the_stage_they_are_counted_in(client, board, tracked):
    """The overlap is made obvious on the row itself: every lens row carries
    the funnel stage that actually counts it."""
    resp = client.get(reverse("my_applications"))
    for lens in resp.context["lenses"]:
        for item in lens["items"]:
            assert item["stage_label"] in {
                "Saved", "Applied", "Interviewing", "Offer", "Done",
            }
    body = resp.content.decode()
    # 2026 redesign shortened the intro; "sorted by deadline instead of
    # stage" replaced the old "same roles seen by deadline, not extra ones"
    # sentence, but the overlap claim still has to be ON the page somewhere.
    assert "sorted by deadline instead of stage" in body
    assert f"of {resp.context['live_total']} live" in body


# ---------------------------------------------------------------------------
# The feed's own countdown vs. the date's stated precision.
#
# `_urgency_item` builds its countdown inline rather than calling
# `deadline_marker`, so for a release it printed "Closes in 4 days" on a row
# whose precision never named a day — the firm page (which does call
# `deadline_marker`) rendered the same row honestly, and the two surfaces
# disagreed about one date.
#
# The fix splits the two jobs `days_left` was doing. It stays the true day
# count, because four consumers sort and aggregate on it; only the reader-
# facing countdown, urgency band and fuse move to the coarser unit.
# ---------------------------------------------------------------------------
@pytest.mark.django_db
@pytest.mark.parametrize(
    "precision,expected_countdown",
    [("month", "closes in 2 months"), ("estimated", "estimated in 2 months")],
)
def test_an_inexact_deadline_gets_no_day_countdown_on_the_feed(
    precision, expected_countdown
):
    from directory.views import _urgency_item

    today = timezone.localdate()
    firm = Firm.objects.create(slug="inexact-co", name="Inexact Co")
    opp = Opportunity.objects.create(
        firm=firm, title="Summer Analyst", status="open",
        bucket=next(iter(TARGET_BUCKETS)),
        url="https://inexact.example/1",
        # ~62 days out, so a day-level reading would say "Closes in 62 days".
        deadline=today + timedelta(days=62),
        deadline_precision=precision,
    )

    item = _urgency_item(
        opp, now=timezone.now(), today=today, my_firm_ids=set()
    )

    assert item["countdown"] == expected_countdown
    assert "62 days" not in item["countdown"]
    # The fuse burns down to a specific afternoon, so an inexact date gets
    # none — and the template gates on this being None, not on `dated`.
    assert item["fuse_pct"] is None
    # ...while `days_left` keeps the true day count, because the feed sort,
    # the firm cluster's `next_days`, the cluster role sort and the
    # closing-this-week count all read it and each breaks on a coarser value.
    assert item["days_left"] == 62
    assert item["dated"] is True
    # A month-level date cannot earn a day-level urgency band.
    assert item["level"] == "upcoming"


@pytest.mark.django_db
def test_a_day_precise_deadline_still_gets_its_day_countdown():
    """The guard on the fix: 633 live rows carry day precision and must be
    completely unaffected."""
    from directory.views import _urgency_item

    today = timezone.localdate()
    firm = Firm.objects.create(slug="exact-co", name="Exact Co")
    opp = Opportunity.objects.create(
        firm=firm, title="Summer Analyst", status="open",
        bucket=next(iter(TARGET_BUCKETS)),
        url="https://exact.example/1",
        deadline=today + timedelta(days=4),
        deadline_precision="day",
    )

    item = _urgency_item(
        opp, now=timezone.now(), today=today, my_firm_ids=set()
    )

    assert item["countdown"] == "Closes in 4 days"
    assert item["days_left"] == 4
    assert item["level"] == "soon"
    assert item["fuse_pct"] is not None
