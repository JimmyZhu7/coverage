"""Regression tests for the "feed is over-claiming" audit findings (A1-A5,
A7): the honesty markers views.py computes for the public Opportunities
page. Each test below pins one specific over-claim that was live on the
public page and is now fixed.
"""

from __future__ import annotations

from datetime import timedelta

import re

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


# ---------------------------------------------------------------------------
# PROVENANCE. `Opportunity.confidence` is 1.0 when the board published the
# date in a structured field and 0.6 when `enrich_postings` read it out of the
# posting's prose — 92 of the 121 dated open roles are the second kind. Both
# are shown; only one is a quotation of a field, and rendering them
# identically claims a precision the data does not have.
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_a_prose_read_deadline_is_marked_on_the_feed(client):
    from datetime import timedelta

    from django.utils import timezone as tz

    firm = Firm.objects.create(slug="bofa", name="Bank of America")
    Opportunity.objects.create(
        firm=firm, title="Reported Analyst", bucket="internship", status="open",
        deadline=tz.localdate() + timedelta(days=20), deadline_precision="day",
        confidence=0.6, url="https://bofa.com/reported")

    body = client.get("/opportunities/").content.decode()
    assert "is-reported" in body
    # The caveat must reach a screen reader, not only a hovering mouse.
    assert "(reported)" in body


@pytest.mark.django_db
def test_a_provider_stated_deadline_carries_no_mark(client):
    from datetime import timedelta

    from django.utils import timezone as tz

    firm = Firm.objects.create(slug="ms", name="Morgan Stanley")
    Opportunity.objects.create(
        firm=firm, title="Stated Analyst", bucket="internship", status="open",
        deadline=tz.localdate() + timedelta(days=20), deadline_precision="day",
        confidence=1.0, url="https://ms.com/stated")

    # Strip <style>: the mark's own CSS rule ships inline in this page, so a
    # bare substring test would pass whether or not a card wore the class.
    # Same trap the grid-column comment in calendar.html documents.
    body = re.sub(r"<style.*?</style>", "",
                  client.get("/opportunities/").content.decode(), flags=re.S)
    assert "Stated Analyst" in body
    assert "is-reported" not in body


# ---------------------------------------------------------------------------
# FACT CHIPS. Same component on the feed, the firm page and My Applications:
# what the posting states about applying. The firm page spent a release
# showing strictly less about a role than the feed showed about the same row.
# ---------------------------------------------------------------------------

def _stated(firm, **extra):
    return Opportunity.objects.create(
        firm=firm, title="Summer Analyst", bucket="internship", status="open",
        url="https://gs.com/sa", sponsorship="no",
        raw={"facts": {"pay": {"value": "$85k–$100k", "phrase": "Pay Range $85,000-$100,000"},
                       "gpa": {"value": "3.5", "phrase": "minimum GPA of 3.5"}}},
        **extra)


@pytest.mark.django_db
def test_the_feed_card_shows_what_the_posting_states(client):
    _stated(Firm.objects.create(slug="gs", name="Goldman Sachs"))
    body = client.get("/opportunities/").content.decode()
    assert "No sponsorship" in body
    assert "$85k–$100k" in body
    # Evidence travels with the value, per directory/facts.py's contract.
    assert "Pay Range $85,000-$100,000" in body


@pytest.mark.django_db
def test_the_firm_page_shows_the_same_facts_as_the_feed(client):
    firm = Firm.objects.create(slug="gs", name="Goldman Sachs")
    _stated(firm)
    body = client.get(f"/firms/{firm.slug}/").content.decode()
    assert "No sponsorship" in body
    assert "$85k–$100k" in body


@pytest.mark.django_db
def test_an_unfetched_posting_states_nothing_rather_than_guessing(client):
    """Silence is an answer and its answer is "we don't know". A posting we
    never read must not acquire chips from its firm or its title."""
    firm = Firm.objects.create(slug="ubs", name="UBS")
    Opportunity.objects.create(
        firm=firm, title="Unread Analyst", bucket="internship", status="open",
        url="https://ubs.com/unread")
    body = re.sub(r"<style.*?</style>", "",
                  client.get("/opportunities/").content.decode(), flags=re.S)
    assert "Unread Analyst" in body
    assert "fact-chip" not in body


@pytest.mark.django_db
def test_an_undated_role_says_no_date_unless_it_claimed_rolling(client):
    """"Rolling" is a claim the posting has to make. ~600 open roles simply
    never state how they close, and calling that rolling review invented a
    fact about every one of them."""
    firm = Firm.objects.create(slug="citi", name="Citi")
    Opportunity.objects.create(firm=firm, title="Silent Role", bucket="internship",
                               status="open", url="https://citi.com/silent")
    Opportunity.objects.create(
        firm=firm, title="Rolling Role", bucket="internship", status="open",
        url="https://citi.com/rolling",
        raw={"facts": {"rolling": {"value": "Rolling",
                                   "phrase": "reviewed on a rolling basis"}}})

    body = client.get("/opportunities/").content.decode()
    assert "No date posted" in body
    assert "reviewed on a rolling basis" in body


# ---------------------------------------------------------------------------
# ELIGIBILITY VERDICTS. The one chip about the READER: computed only where
# both sides stated — the posting's own text AND the user's Settings. On live
# data 232 of 240 eligibility-stating roles excluded this user's class year
# and ranked identically to the 8 that named it.
# ---------------------------------------------------------------------------

def _grad_role(firm, years, label, title="Analyst Intern", sponsorship="unknown",
               region=""):
    return Opportunity.objects.create(
        firm=firm, title=title, bucket="internship", status="open",
        sponsorship=sponsorship, region=region,
        url=f"https://x.com/{title.replace(' ', '')}-{label}",
        raw={"facts": {"grad": {"value": label, "years": years,
                                "phrase": f"graduating {label}"}}})


@pytest.fixture
def student(django_user_model):
    u = django_user_model.objects.create_user(email="s2029@x.com", password="x")
    u.class_year = 2029
    u.work_authorization = {"us": "sponsorship", "hk": "sponsorship"}
    u.save(update_fields=["class_year", "work_authorization"])
    return u


@pytest.mark.django_db
def test_a_stated_window_gets_a_personal_verdict(client, student):
    firm = Firm.objects.create(slug="gs", name="Goldman Sachs")
    _grad_role(firm, ["2029"], "2029", title="Your Year Intern")
    _grad_role(firm, ["2027", "2028"], "2027–2028", title="Other Year Intern")
    client.force_login(student)
    body = client.get("/opportunities/").content.decode()
    assert "Your year (2029)" in body
    assert "For 2027–2028 grads" in body


@pytest.mark.django_db
def test_silence_earns_no_verdict_in_either_direction(client, student):
    firm = Firm.objects.create(slug="ms", name="Morgan Stanley")
    Opportunity.objects.create(firm=firm, title="Silent Intern", bucket="internship",
                               status="open", url="https://ms.com/silent")
    client.force_login(student)
    body = client.get("/opportunities/").content.decode()
    assert "Silent Intern" in body
    assert "verdict-" not in re.sub(r"<style.*?</style>", "", body, flags=re.S)


@pytest.mark.django_db
def test_a_refused_visa_in_a_market_they_need_one_is_a_verdict(client, student):
    firm = Firm.objects.create(slug="jpm", name="J.P. Morgan")
    Opportunity.objects.create(
        firm=firm, title="No Visa Intern", bucket="internship", status="open",
        sponsorship="no", region="us", url="https://jpm.com/novisa")
    client.force_login(student)
    body = client.get("/opportunities/").content.decode()
    assert "Won&#x27;t sponsor you here" in body or "Won't sponsor you here" in body


@pytest.mark.django_db
def test_the_fit_filter_hides_only_blocking_verdicts_and_says_so(client, student):
    firm = Firm.objects.create(slug="citi", name="Citi")
    _grad_role(firm, ["2029"], "2029", title="Keep Me Intern")
    _grad_role(firm, ["2027"], "2027", title="Hide Me Intern")
    Opportunity.objects.create(firm=firm, title="Silent Keeps Intern",
                               bucket="internship", status="open",
                               url="https://citi.com/silent2")
    client.force_login(student)
    body = client.get("/opportunities/?fit=1").content.decode()
    assert "Keep Me Intern" in body
    assert "Silent Keeps Intern" in body, "silence never hides"
    assert "Hide Me Intern" not in body
    assert "1 role states a requirement you don" in body, "the scope line owns honesty"


@pytest.mark.django_db
def test_the_fit_toggle_needs_a_profile_to_exist(client, django_user_model):
    blank = django_user_model.objects.create_user(email="blank@x.com", password="x")
    blank.class_year = None
    blank.work_authorization = {}
    blank.save(update_fields=["class_year", "work_authorization"])
    firm = Firm.objects.create(slug="ubs", name="UBS")
    _grad_role(firm, ["2027"], "2027", title="Would Hide Intern")
    client.force_login(blank)
    body = client.get("/opportunities/?fit=1").content.decode()
    assert "Would Hide Intern" in body, "no Settings, no verdicts, no hiding"
    assert 'name="fit"' not in body, "no toggle offered either"
