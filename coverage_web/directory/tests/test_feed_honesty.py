"""Regression tests for the "feed is over-claiming" audit findings (A1-A5,
A7): the honesty markers views.py computes for the public Opportunities
page. Each test below pins one specific over-claim that was live on the
public page and is now fixed.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.urls import reverse
from django.utils import timezone

from directory.models import Firm, Opportunity
from directory.views import (
    _FRESH_DAYS, REGION_NONE, _fresh_label, _sponsorship_tag, _urgency_feed,
    _urgency_item,
)

TODAY = timezone.localdate()
NOW = timezone.now()


def _firm(slug="evercore", name="Evercore", **kw):
    return Firm.objects.create(slug=slug, name=name, **kw)


def _opp(firm, url, *, deadline=None, region="", bucket="internship", **kw):
    return Opportunity.objects.create(
        firm=firm, url=url, title="Summer Analyst", bucket=bucket,
        status="open", deadline=deadline, region=region, **kw,
    )


def _seen(o, days_ago):
    """Backdate `first_seen` past `auto_now_add` — `.update()` bypasses it."""
    Opportunity.objects.filter(pk=o.pk).update(first_seen=NOW - timedelta(days=days_ago))
    o.refresh_from_db()
    return o


# ---------------------------------------------------------------------------
# A1(a) — the "New" badge must say what it measures (first_seen), not "New".
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("seen_days,expected", [
    (None, ""),
    (0, "First seen today"),
    (1, "First seen 1d ago"),
    (5, "First seen 5d ago"),
])
def test_fresh_label_says_what_it_measures(seen_days, expected):
    assert _fresh_label(seen_days) == expected


@pytest.mark.django_db
def test_feed_badge_reads_first_seen_not_new(client):
    """The bug: after a bulk import, a role backfilled today reads "New"
    even though the firm posted it long ago — `first_seen` is when the row
    entered OUR db (auto_now_add), not when the firm posted."""
    firm = _firm()
    o = _seen(_opp(firm, "https://x/1"), 3)
    resp = client.get(reverse("opportunities"))
    body = resp.content.decode()
    assert "First seen 3d ago" in body
    assert ">New<" not in body


# ---------------------------------------------------------------------------
# A1(b) — the stat-strip's "Fresh" label must be driven by `_FRESH_DAYS`,
# never a hardcoded window that can drift from it.
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_fresh_stat_strip_label_matches_fresh_days_constant(client):
    firm = _firm()
    _seen(_opp(firm, "https://x/1"), 1)
    resp = client.get(reverse("opportunities"))
    assert resp.context["dash"]["fresh_days"] == _FRESH_DAYS
    body = resp.content.decode()
    assert f"Fresh ({_FRESH_DAYS}d)" in body
    assert "Fresh This Week" not in body  # the old, drift-prone hardcoded label


# ---------------------------------------------------------------------------
# A2 — a dated role whose deadline has PASSED is neither "dated-and-live" nor
# "rolling". Rolling must mean `deadline is None`.
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_a_passed_deadline_is_not_rolling():
    firm = _firm()
    o = _opp(firm, "https://x/1", deadline=TODAY - timedelta(days=5))
    item = _urgency_item(o, now=NOW, today=TODAY, my_firm_ids=set())
    assert item["level"] == "passed"
    assert item["dated"] is True          # NOT rolling
    assert item["countdown"] == "Deadline passed"


@pytest.mark.django_db
def test_a_future_deadline_is_still_dated_and_a_null_deadline_is_rolling():
    firm = _firm()
    dated = _urgency_item(
        _opp(firm, "https://x/1", deadline=TODAY + timedelta(days=5)),
        now=NOW, today=TODAY, my_firm_ids=set(),
    )
    assert dated["level"] in ("today", "soon", "upcoming")
    rolling = _urgency_item(
        _opp(firm, "https://x/2", deadline=None), now=NOW, today=TODAY, my_firm_ids=set(),
    )
    assert rolling["level"] == "rolling"
    assert rolling["dated"] is False


@pytest.mark.django_db
def test_a_passed_deadline_never_shows_the_freshness_badge():
    firm = _firm()
    o = _seen(_opp(firm, "https://x/1", deadline=TODAY - timedelta(days=1)), 0)
    item = _urgency_item(o, now=NOW, today=TODAY, my_firm_ids=set())
    # `is_fresh` may still be true (seen today), but the template only shows
    # the badge when `not r.dated` — and a passed deadline IS dated.
    assert item["dated"] is True


@pytest.mark.django_db
def test_passed_deadlines_sort_after_live_closing_roles():
    """The old two-branch split put a passed deadline in `rolling`; the new
    three-way split must sort it to the END of `closing`, not first (a
    negative `days_left` would otherwise sort as MOST urgent)."""
    firm = _firm()
    live = _opp(firm, "https://x/1", deadline=TODAY + timedelta(days=3))
    passed = _opp(firm, "https://x/2", deadline=TODAY - timedelta(days=30))
    qs = Opportunity.objects.filter(pk__in=[live.pk, passed.pk])
    feed = _urgency_feed(qs, now=NOW, today=TODAY, my_firm_ids=set())
    assert [i["url"] for i in feed["closing"]] == [live.url, passed.url]


@pytest.mark.django_db
def test_feed_and_firm_page_agree_a_passed_deadline_has_passed(client):
    firm = _firm()
    o = _opp(firm, "https://x/1", deadline=TODAY - timedelta(days=2))
    feed_body = client.get(reverse("opportunities")).content.decode()
    firm_body = client.get(reverse("directory:firm_detail", args=[firm.slug])).content.decode()
    assert "Deadline passed" in feed_body
    assert "passed" in firm_body.lower()


# ---------------------------------------------------------------------------
# A3 — the "Show everything" escape hatch must actually reveal the hidden
# non-campus roles, preserving the student's other active filters.
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_show_all_qs_actually_shows_the_hidden_roles(client):
    firm = _firm()
    _opp(firm, "https://x/1", bucket="other", region="hk")  # hidden by the default view
    resp = client.get(reverse("opportunities"), {"region": "hk"})
    show_all_qs = resp.context["show_all_qs"]
    assert show_all_qs, "show_all_qs must not be empty"

    follow = client.get(f"{reverse('opportunities')}?{show_all_qs}")
    assert follow.context["total"] == 1                # the hidden role is now shown
    assert follow.context["selected"]["role"] == "all"
    assert follow.context["selected"]["region"] == "hk"  # other filters preserved


# ---------------------------------------------------------------------------
# A4 — the page must claim a personalized ordering ONLY when it performed one.
#
# The claim used to live in a "Sorted for you" chip above the results. That
# chip is gone (it restated the hero subtitle two inches below it), so the
# subtitle carries the contract alone and is conditional on `personalized`.
# Unconditional, it told every signed-out visitor their feed was "sorted to
# your firms" — exactly the lie the chip had been careful not to tell.
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_personalized_flag_is_in_context_and_renders(client):
    from crm.models import UserFirm

    from .test_tracking import _user

    firm = _firm()
    _opp(firm, "https://x/1")
    user = _user()
    UserFirm.all_objects.create(user=user, firm=firm, tier=1)
    client.force_login(user)

    resp = client.get(reverse("opportunities"))
    assert resp.context["personalized"] is True
    assert "sorted to your firms" in resp.content.decode()
    # The retired chip must not come back alongside it.
    assert "Sorted for you" not in resp.content.decode()


@pytest.mark.django_db
def test_signed_out_feed_is_not_personalized(client):
    firm = _firm()
    _opp(firm, "https://x/1")
    resp = client.get(reverse("opportunities"))
    assert resp.context["personalized"] is False
    body = resp.content.decode()
    assert "sorted to your firms" not in body
    assert "Sorted for you" not in body


# ---------------------------------------------------------------------------
# A5 — sponsorship is per-REGION on Firm.sponsors; must key off opp.region,
# and a blank region must never borrow a firm-wide answer.
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_sponsorship_reads_the_regional_dict_not_a_bare_bool():
    firm = _firm(sponsors={"us": True, "hk": "unknown"})
    us_role = _opp(firm, "https://x/1", region="us")
    hk_role = _opp(firm, "https://x/2", region="hk")
    assert _sponsorship_tag(us_role) == {"label": "Sponsorship", "css": "spon-known"}
    assert _sponsorship_tag(hk_role) is None  # "unknown" for hk -> no pill


@pytest.mark.django_db
def test_a_firm_sponsoring_one_region_does_not_stamp_another():
    firm = _firm(sponsors={"hk": True})
    us_role = _opp(firm, "https://x/1", region="us")
    assert _sponsorship_tag(us_role) is None  # HK-only sponsorship != a US pill


@pytest.mark.django_db
def test_blank_region_never_borrows_a_firm_wide_answer():
    """1,223 open rows carry no resolved region at all. Even when the firm
    has real data for SOME market, a role whose own region is unknown must
    not inherit any of it."""
    firm = _firm(sponsors={"us": True, "hk": True})
    unresolved = _opp(firm, "https://x/1", region="")
    assert _sponsorship_tag(unresolved) is None


@pytest.mark.django_db
def test_the_posting_s_own_field_still_wins_over_the_firm_fallback():
    firm = _firm(sponsors={"us": False})
    role = _opp(firm, "https://x/1", region="us", sponsorship="yes")
    assert _sponsorship_tag(role) == {"label": "Sponsorship", "css": "spon-known"}


# ---------------------------------------------------------------------------
# A7 — facets must reflect the same role-bucket scope the view renders, not
# the whole open table (including the default-hidden "other" roles).
# ---------------------------------------------------------------------------

def _concrete(options):
    """The real markets/verticals a facet offers, minus its two sentinels.

    "" ("Any Region" / "Any Track") and "none" ("Other / Unstated") are always
    part of the control's vocabulary rather than values drawn from the data, so
    A7 — "don't offer options that only exist in hidden roles" — is a statement
    about the CONCRETE options only."""
    return {o["value"] for o in options} - {"", REGION_NONE}


@pytest.mark.django_db
def test_facets_do_not_offer_options_that_only_exist_in_hidden_roles(client):
    firm = _firm(tracks=["ib"])
    other_firm = _firm(slug="offshore", name="Offshore Co", tracks=["consulting"])
    _opp(firm, "https://x/1", region="us")                       # campus, visible by default
    _opp(other_firm, "https://x/2", region="jp", bucket="other")  # hidden by default

    resp = client.get(reverse("opportunities"))
    assert _concrete(resp.context["facets"]["regions"]) == {"us"}  # "jp" not offered
    assert _concrete(resp.context["facets"]["tracks"]) == {"ib"}

    # Selecting role=all brings the hidden role's facets back.
    resp_all = client.get(reverse("opportunities"), {"role": "all"})
    assert _concrete(resp_all.context["facets"]["regions"]) == {"us", "jp"}
