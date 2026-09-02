"""Regression tests for the "feed is over-claiming" audit findings (A1-A5,
A7): the honesty markers views.py computes for the public Opportunities
page. Each test below pins one specific over-claim that was live on the
public page and is now fixed.
"""

from __future__ import annotations

from datetime import date, timedelta

import re

import pytest
from django.urls import reverse
from django.utils import timezone

from directory.models import Firm, Opportunity
from directory.views import (
    _FRESH_DAYS, REGION_NONE, _fact_chips, _unconfirmed_note,
    _urgency_feed, _urgency_item,
)

TODAY = timezone.localdate()
NOW = timezone.now()

# The page inlines its stylesheet, and CSS COMMENTS reach the response body.
# Any assertion about card copy has to strip them or it can pass off a note
# someone left about a retired element — see
# `test_feed_badge_reads_first_seen_not_new`, which did exactly that.
_STYLE_RE = re.compile(r"<style.*?</style>", re.S)


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

@pytest.mark.django_db
def test_feed_badge_reads_first_seen_not_new(client):
    """The bug: after a bulk import, a role backfilled today reads "New"
    even though the firm posted it long ago — `first_seen` is when the row
    entered OUR db (auto_now_add), not when the firm posted.

    THE STYLE BLOCK IS STRIPPED FIRST, and that is the whole reliability of
    this test. It used to assert "First seen 3d ago" against the raw
    response, which the page satisfied from a CSS COMMENT in _styles.html
    describing the badge this change RETIRED — capital F and all. The card's
    own visible copy is lowercase, so the assertion was green whether or not
    the card said anything, and would have gone red if someone tidied a
    comment. Assert what a reader sees.
    """
    firm = _firm()
    o = _seen(_opp(firm, "https://x/1"), 3)
    body = _STYLE_RE.sub("", client.get(reverse("opportunities")).content.decode())
    assert "first seen 3d ago" in body
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
def test_fuse_pct_shrinks_as_the_deadline_approaches():
    """`fuse_pct` feeds `_styles.html`'s `.fuse-fill { width: var(--fuse) }`,
    which animates DOWN from a full bar to that width — so it is the bar's
    OWN remaining length: near 100 when the deadline is far off, near the
    floor of 4 when it is imminent, and exactly 0 once it has passed
    (`_urgency_item`'s "passed" branch, pinned separately above). A stray
    `1 -` in the formula inverted this: a role closing TODAY computed to a
    full 100, and a role at the far edge of `_FUSE_HORIZON` computed to the
    floor of 4 — a role about to close looking safer than one over a month
    out."""
    firm = _firm()
    closing_today = _urgency_item(
        _opp(firm, "https://x/today", deadline=TODAY), now=NOW, today=TODAY,
        my_firm_ids=set(),
    )
    far_out = _urgency_item(
        _opp(firm, "https://x/far", deadline=TODAY + timedelta(days=45)),
        now=NOW, today=TODAY, my_firm_ids=set(),
    )
    assert closing_today["fuse_pct"] < far_out["fuse_pct"], (
        "a role closing today must show a SHORTER remaining fuse than one "
        "45 days out, not a longer one"
    )
    assert closing_today["fuse_pct"] == 4    # floored, not 0 — still open today
    assert far_out["fuse_pct"] == 100


# ---------------------------------------------------------------------------
# `elapsed_pct` — the rolling card's own footer signal, the weaker cousin of
# `fuse_pct` that fills the slot a rolling card's fuse never renders in (see
# `_urgency_item`'s comment and `_rolecard.html`/`_styles.html`'s
# `.rolecard-observed`). Unlike the fuse it GROWS, never colours by urgency,
# and never animates — a measured elapsed time must not wear a real
# deadline's own visual language.
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_elapsed_pct_grows_with_time_and_caps_at_the_horizon():
    firm = _firm()
    fresh = _urgency_item(
        _seen(_opp(firm, "https://x/fresh", deadline=None), 0),
        now=NOW, today=TODAY, my_firm_ids=set(),
    )
    mid = _urgency_item(
        _seen(_opp(firm, "https://x/mid", deadline=None), 20),
        now=NOW, today=TODAY, my_firm_ids=set(),
    )
    old = _urgency_item(
        _seen(_opp(firm, "https://x/old", deadline=None), 90),
        now=NOW, today=TODAY, my_firm_ids=set(),
    )
    assert fresh["elapsed_pct"] == 0        # just posted: nothing observed yet
    assert 0 < mid["elapsed_pct"] < old["elapsed_pct"]
    assert old["elapsed_pct"] == 100        # capped, not "104% open"


@pytest.mark.django_db
def test_elapsed_pct_is_absent_for_any_dated_role():
    """A dated role — closing, upcoming, or already passed — must never
    carry `elapsed_pct`: the footer slot belongs to exactly one of `fuse_pct`
    or `elapsed_pct`, never both, and never neither on a rolling card."""
    firm = _firm()
    passed = _urgency_item(
        _opp(firm, "https://x/passed", deadline=TODAY - timedelta(days=5)),
        now=NOW, today=TODAY, my_firm_ids=set(),
    )
    upcoming = _urgency_item(
        _opp(firm, "https://x/upcoming", deadline=TODAY + timedelta(days=10)),
        now=NOW, today=TODAY, my_firm_ids=set(),
    )
    assert passed.get("elapsed_pct") is None
    assert upcoming.get("elapsed_pct") is None
    rolling = _urgency_item(
        _opp(firm, "https://x/rolling", deadline=None),
        now=NOW, today=TODAY, my_firm_ids=set(),
    )
    assert rolling.get("fuse_pct") is None
    assert rolling["elapsed_pct"] is not None


@pytest.mark.django_db
def test_rolling_card_renders_the_observed_footer_not_the_fuse(client):
    """Rendered-HTML check, the same posture the fuse's own tests take
    (`test_styles_block.py`). The row (`_rolecard.html`) has no fuse bar or
    observed bar any more — urgency moved into the colour of the deadline
    figure in `.rr-due`, and an undated role states its elapsed time in
    words on the meta line instead of a growing bar (see the row's own
    header comment). The invariant those two footers used to pin still
    holds in the new vocabulary: a rolling/undated row must show the
    "first seen" wording and never a real countdown figure, and a dated
    row must show its countdown figure and never the "first seen" wording
    — the two are still mutually exclusive on every row."""
    # Two different firms, not one: `directory.dupes.fold_duplicates` folds
    # same-firm, same-title postings into one card, and both `_opp` calls
    # below share the helper's hardcoded title — same firm would leave only
    # one of the two cards on the page to assert against.
    rolling_firm = _firm(slug="evercore", name="Evercore")
    dated_firm = _firm(slug="jefferies", name="Jefferies")
    _seen(_opp(rolling_firm, "https://x/rolling", deadline=None), 15)
    _opp(dated_firm, "https://x/dated", deadline=TODAY + timedelta(days=10))
    body = _STYLE_RE.sub("", client.get(reverse("opportunities")).content.decode())

    # The undated row: no date posted, elapsed time stated in words, and its
    # due column carries the honest dash — never a countdown figure.
    assert "No date posted, first seen 15d ago" in body
    assert "rr-due-none" in body
    # The dated row: a real countdown figure, coloured by its urgency level
    # (10 days out is "upcoming" — see `_urgency_item`'s level bands).
    assert "rr-due-n meta-upcoming" in body
    # Neither wording leaks onto the other row's kind of card.
    assert body.count("first seen") == 1
    assert body.count("rr-due-n meta-upcoming") == 1


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


@pytest.mark.django_db
def test_feed_and_firm_page_agree_on_no_date_posted_wording(client):
    """Cross-surface consistency audit, finding F: three wordings for the
    identical "this role has no deadline" fact — the feed's considered
    "No date posted, first seen Nd ago" (test-pinned:
    `test_feed_badge_reads_first_seen_not_new` above), the firm page's old
    "No deadline posted" (`views.deadline_marker`), and My Applications'
    bare "No date posted" (`_apps_body.html`). Standardized on the feed's
    base phrase, "No date posted" — `deadline_marker` is the one function
    all three read from (directly, or via `_lens_item`/`_stage_card` for My
    Applications), so fixing it there fixes the firm page and keeps My
    Applications' existing wording exactly as it was.

    JUDGMENT CALL: the firm page and My Applications do NOT also gain the
    feed's "first seen Nd ago" clause. `deadline_marker` has no `first_seen`
    in scope, and both callers already carry a DIFFERENT elapsed-time fact
    where the feed has none (the role drawer's `checked_ago`, and both the
    drawer and firm row's `unconfirmed` flag) — a second one would clutter a
    single-role view rather than help it, where the feed's earns its place
    triaging ~2,600 rows at once."""
    firm = _firm()
    _opp(firm, "https://x/undated", deadline=None)
    firm_body = client.get(
        reverse("directory:firm_detail", args=[firm.slug])
    ).content.decode()
    assert "No date posted" in firm_body
    assert "No deadline posted" not in firm_body


# ---------------------------------------------------------------------------
# A3 — the "Everything" escape hatch must actually reveal the hidden
# non-campus roles, preserving the student's other active filters.
#
# This used to be a link built from `show_all_qs` (role forced to "all",
# every other querystring param copied across) surfaced in the header's
# subset sentence. That sentence — and `show_all_qs` with it — is gone
# (2026-08-27, "take this thing away"): "Everything" is a normal segment in
# the Role Type control now, a real form field the browser's own
# serialization carries alongside every other filter, so there is nothing
# left to build. The guarantee is asked the same way regardless.
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_the_everything_segment_actually_shows_the_hidden_roles(client):
    firm = _firm()
    _opp(firm, "https://x/1", bucket="other", region="hk")  # hidden by the default view
    resp = client.get(reverse("opportunities"), {"region": "hk"})
    everything = next(s for s in resp.context["role_segments"] if s["value"] == "all")
    assert everything["count"] == 1, "the Everything segment names the hidden role itself"

    follow = client.get(reverse("opportunities"), {"region": "hk", "role": "all"})
    assert follow.context["total"] == 1                # the hidden role is now shown
    assert follow.context["selected"]["role"] == "all"
    assert follow.context["selected"]["region"] == "hk"  # other filters preserved


# ---------------------------------------------------------------------------
# A4 — the page must claim a personalized ordering ONLY when it performed one.
#
# The claim used to live in a "Sorted for you" chip above the results, then in
# the hero subtitle after the chip was retired (it restated the subtitle two
# inches below it). The subtitle is gone too now (every hero's eyebrow and
# subtitle were removed), so `personalized` carries the contract with no
# visible copy at all — asserted directly against context instead of against
# rendered text that no longer exists.
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
    # The retired chip must not come back.
    assert "Sorted for you" not in resp.content.decode()


@pytest.mark.django_db
def test_signed_out_feed_is_not_personalized(client):
    firm = _firm()
    _opp(firm, "https://x/1")
    resp = client.get(reverse("opportunities"))
    assert resp.context["personalized"] is False
    assert "Sorted for you" not in resp.content.decode()


# ---------------------------------------------------------------------------
# A5 — sponsorship is per-REGION on Firm.sponsors; must key off opp.region,
# and a blank region must never borrow a firm-wide answer.
# ---------------------------------------------------------------------------

def _spon_chip(opp):
    """The sponsorship answer as it reaches a card, or None.

    Reads `_fact_chips`, which is the single carrier on every surface since
    the `_sponsorship_tag` pill was retired: the pill only ever rendered on
    /firms/<slug>/ while the feed built its chip off the raw
    `Opportunity.sponsorship` column, so a firm-policy row matched the
    "Sponsors visas" filter and arrived with no sponsorship label at all."""
    return next((c for c in _fact_chips(opp) if c["label"].startswith(
        ("Sponsors", "No sponsorship"))), None)


@pytest.mark.django_db
def test_sponsorship_reads_the_regional_dict_not_a_bare_bool():
    firm = _firm(sponsors={"us": True, "hk": "unknown"})
    us_role = _opp(firm, "https://x/1", region="us")
    hk_role = _opp(firm, "https://x/2", region="hk")
    # A firm-sourced answer gets its own "· firm policy" label — see
    # directory.sponsorship.effective_sponsorship — never the posting-stated
    # wording, so the chip cannot be mistaken for a claim the posting made.
    assert _spon_chip(us_role)["label"] == "Sponsors · firm policy"
    assert _spon_chip(hk_role) is None  # "unknown" for hk -> no chip


@pytest.mark.django_db
def test_a_firm_sponsoring_one_region_does_not_stamp_another():
    firm = _firm(sponsors={"hk": True})
    us_role = _opp(firm, "https://x/1", region="us")
    assert _spon_chip(us_role) is None  # HK-only sponsorship != a US chip


@pytest.mark.django_db
def test_blank_region_never_borrows_a_firm_wide_answer():
    """1,223 open rows carry no resolved region at all. Even when the firm
    has real data for SOME market, a role whose own region is unknown must
    not inherit any of it."""
    firm = _firm(sponsors={"us": True, "hk": True})
    unresolved = _opp(firm, "https://x/1", region="")
    assert _spon_chip(unresolved) is None


@pytest.mark.django_db
def test_the_posting_s_own_field_still_wins_over_the_firm_fallback():
    firm = _firm(sponsors={"us": False})
    role = _opp(firm, "https://x/1", region="us", sponsorship="yes")
    assert _spon_chip(role)["label"] == "Sponsors visas"


@pytest.mark.django_db
def test_firm_sourced_no_gets_its_own_label():
    firm = _firm(sponsors={"us": False})
    role = _opp(firm, "https://x/1", region="us")
    assert _spon_chip(role)["label"] == "No sponsorship · firm policy"


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
    # The wording used to live in the header's merged hidden-counts sentence
    # ("1 you don't qualify for ... Also hidden: ..."). That sentence is gone
    # (2026-08-27, "take this thing away") and the guarantee moved to the
    # control that causes the hide: the Eligible-only checkbox renders
    # checked, and unchecking it (the follow-up request below) is the
    # one-click reversal — no count needed anywhere else.
    assert 'name="fit" value="1" checked' in body
    restored = client.get("/opportunities/").content.decode()
    assert "Hide Me Intern" in restored, "unchecking the toggle is the escape hatch"


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


# ---------------------------------------------------------------------------
# CITY-VARIANT FOLDING — display-only, semantics untouched.
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_one_programme_many_cities_folds_but_keeps_every_card(client):
    firm = Firm.objects.create(slug="bofa", name="Bank of America")
    for city in ("Hong Kong", "Singapore", "London"):
        Opportunity.objects.create(
            firm=firm, title=f"GCB Summer Analyst - 2027 - {city}",
            bucket="internship", status="open", location=city,
            url=f"https://bofa.com/gcb-{city.replace(' ', '')}")
    body = client.get("/opportunities/").content.decode()
    # Title sort puts London before Singapore; the fold preserves the
    # column's own ordering rather than imposing one.
    assert "+2 more locations: London · Singapore" in body
    # Every sibling's own card still renders (inside the fold), with its own
    # save-target identity intact — grouping spends less column, changes
    # nothing about any row.
    assert body.count("GCB Summer Analyst") >= 3


@pytest.mark.django_db
def test_the_fold_separator_survives_locations_that_contain_commas(client):
    """Most located rows carry an internal comma, so a comma-joined summary
    turned "+3" into seven fragments with no boundary between places."""
    firm = Firm.objects.create(slug="jpm", name="J.P. Morgan")
    cities = ["Columbus, OH, United States", "Hong Kong",
              "Seoul, Korea, Republic of", "Singapore"]
    for city in cities:
        Opportunity.objects.create(
            firm=firm, title=f"Investment Banking Summer Analyst - 2027 - {city}",
            bucket="internship", status="open", location=city,
            url=f"https://jpm.com/ibd-{city[:6].replace(' ', '')}")
    body = client.get("/opportunities/").content.decode()
    start = body.index("+3 more locations:")
    listed = body[start + len("+3 more locations:"):body.index("</summary>", start)]
    parts = [p.strip() for p in listed.split("·")]
    # A "+3" label must be followed by exactly three readable places, not by
    # the seven comma-separated fragments the comma join produced.
    assert len(parts) == 3, parts
    known = {c.casefold() for c in cities}
    assert all(p.casefold() in known for p in parts), parts


@pytest.mark.django_db
def test_desk_variants_are_different_jobs_and_never_fold(client):
    """The first draft of the family rule merged 'Internship - Financial
    Engineer' with 'Internship - Cyber Security'. The tail only counts as a
    city when its words appear in the row's own location field."""
    firm = Firm.objects.create(slug="ms", name="Morgan Stanley")
    for desk in ("Financial Engineer", "Cyber Security"):
        Opportunity.objects.create(
            firm=firm, title=f"Internship - {desk}",
            bucket="internship", status="open", location="Paris, France",
            url=f"https://ms.com/{desk.replace(' ', '')}")
    body = client.get("/opportunities/").content.decode()
    assert "more location" not in body


# ---------------------------------------------------------------------------
# COLUMN LAZY-LOADING. The page shipped every firm column at once (~18,000
# DOM nodes for 55 firms) though four fit a screen.
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_the_first_page_of_columns_ships_with_a_sentinel(client):
    for i in range(20):
        f = Firm.objects.create(slug=f"firm{i}", name=f"Firm {i:02d}")
        Opportunity.objects.create(firm=f, title="Analyst", bucket="internship",
                                   status="open", url=f"https://f{i}.com/a")
    # <style> stripped: the class names appear in the page's own inline CSS
    # rules as well as its markup, the same trap the style-block tests
    # document.
    body = re.sub(r"<style.*?</style>", "",
                  client.get("/opportunities/").content.decode(), flags=re.S)
    assert body.count("firmcol-name") == 12, "one page of columns, not all 20"
    assert "cols-sentinel" in body
    assert "cols=12" in body


@pytest.mark.django_db
def test_the_counts_describe_the_board_not_the_loaded_slice(client):
    for i in range(20):
        f = Firm.objects.create(slug=f"firm{i}", name=f"Firm {i:02d}")
        Opportunity.objects.create(firm=f, title="Analyst", bucket="internship",
                                   status="open", url=f"https://f{i}.com/a")
    resp = client.get("/opportunities/")
    assert resp.context["total"] == 20, "the strip counts the board"
    assert len(resp.context["clusters"]) == 12, "the page renders a slice"
    # The context was always right; the STRIP was what lied. Asserting only on
    # the context is what let `{{ clusters|length }}` survive in the template,
    # printing "12 Firms" over a 20-firm board. Read the rendered strip.
    body = re.sub(r"<style.*?</style>", "", resp.content.decode(), flags=re.S)
    strip = body[body.index('class="stat-strip"'):]
    strip = strip[:strip.index("</div>")]
    assert ">20</b> Firms" in strip, "the strip names the board's firm count"
    assert ">12</b> Firms" not in strip, "never the loaded slice"


@pytest.mark.django_db
def test_a_one_firm_board_says_firm_not_firms(client):
    """The strip's counts are singular-aware. "1 Firms" is the same hardcoded
    plural that "1 deadlines" was on the calendar, one line further along."""
    f = Firm.objects.create(slug="solo", name="Solo Capital")
    Opportunity.objects.create(firm=f, title="Analyst", bucket="internship",
                               status="open", url="https://solo.com/a")
    body = re.sub(r"<style.*?</style>", "",
                  client.get("/opportunities/").content.decode(), flags=re.S)
    strip = body[body.index('class="stat-strip"'):]
    strip = strip[:strip.index("</div>")]
    assert ">1</b> Firm<" in strip
    assert ">1</b> Open Role<" in strip


@pytest.mark.django_db
def test_a_later_slice_keeps_the_live_filters(client):
    keep = Firm.objects.create(slug="keepme", name="Zebra Keep Co")
    for i in range(14):
        f = Firm.objects.create(slug=f"firm{i}", name=f"Firm {i:02d}")
        Opportunity.objects.create(firm=f, title="Analyst", bucket="internship",
                                   status="open", url=f"https://f{i}.com/a")
    Opportunity.objects.create(firm=keep, title="Zebra Analyst", bucket="internship",
                               status="open", url="https://keep.com/a")
    resp = client.get("/opportunities/?q=Zebra&cols=0",
                      headers={"HX-Request": "true"})
    body = resp.content.decode()
    assert "Zebra Keep Co" in body
    assert "Firm 00" not in body, "the filter still applies to a paged request"


@pytest.mark.django_db
def test_a_continuation_slice_skips_the_page_weight(client):
    """A cols= fragment consumes the slice, the cursor and the querystring —
    the first lazy loader ran recommendations, the feed bands and four facets
    on every scroll anyway, so each sentinel fetch cost as much as the page
    it was meant to lighten."""
    for i in range(15):
        f = Firm.objects.create(slug=f"firm{i}", name=f"Firm {i:02d}")
        Opportunity.objects.create(firm=f, title="Analyst", bucket="internship",
                                   status="open", url=f"https://f{i}.com/a")
    resp = client.get("/opportunities/?cols=12", headers={"HX-Request": "true"})
    assert resp.status_code == 200
    assert "picks" not in resp.context, "the recommender must not run for a slice"
    assert b"stat-strip" not in resp.content, "a slice is columns, not the page"


@pytest.mark.django_db
def test_the_noscript_cols_link_renders_the_full_page(client):
    """The sentinel's own no-JS fallback arrives WITHOUT the htmx header. The
    first cut keyed the skip on the cursor alone, so exactly the honest
    fallback was the request that crashed on feed=None."""
    for i in range(15):
        f = Firm.objects.create(slug=f"firm{i}", name=f"Firm {i:02d}")
        Opportunity.objects.create(firm=f, title="Analyst", bucket="internship",
                                   status="open", url=f"https://f{i}.com/a")
    resp = client.get("/opportunities/?cols=12")
    assert resp.status_code == 200
    assert b"stat-strip" in resp.content


# ---------------------------------------------------------------------------
# THE LENS→PIPELINE BRIDGE: one click saves the roles that name your year.
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_save_your_year_saves_exactly_the_year_ok_roles(client, student):
    from analytics.models import UserOpportunity

    firm = Firm.objects.create(slug="gs", name="Goldman Sachs")
    mine = _grad_role(firm, ["2029"], "2029", title="Mine Intern")
    _grad_role(firm, ["2027"], "2027", title="Not Mine Intern")
    Opportunity.objects.create(firm=firm, title="Silent Intern", bucket="internship",
                               status="open", url="https://gs.com/silent-b")

    client.force_login(student)
    body = client.get("/opportunities/").content.decode()
    assert "1 open role names your class year" in body

    resp = client.post("/opportunities/track-eligible/", {"confirmed": "1"})
    assert resp.status_code == 302
    tracked = set(UserOpportunity.all_objects.filter(user=student)
                  .values_list("opportunity_id", flat=True))
    assert tracked == {mine.id}, "year_ok only — silence saves nothing"


@pytest.mark.django_db
def test_not_for_me_outranks_your_year(client, student):
    from analytics.models import UserOpportunity

    firm = Firm.objects.create(slug="ms", name="Morgan Stanley")
    dismissed = _grad_role(firm, ["2029"], "2029", title="Dismissed Intern")
    # A second, un-dismissed role so the bulk save has real work to do — a
    # POST that writes nothing would pass this assertion for the wrong reason.
    wanted = _grad_role(firm, ["2029"], "2029", title="Wanted Intern")
    UserOpportunity.all_objects.create(user=student, opportunity=dismissed,
                                       dismissed=True)
    client.force_login(student)
    client.get("/opportunities/")
    client.post("/opportunities/track-eligible/", {"confirmed": "1"})

    assert UserOpportunity.all_objects.filter(
        user=student, opportunity=wanted).exists(), "sanity: the save ran"
    uo = UserOpportunity.all_objects.get(user=student, opportunity=dismissed)
    assert uo.dismissed is True, "the user said no; a bulk save must not unsay it"


@pytest.mark.django_db
def test_the_offer_disappears_once_everything_is_saved(client, student):
    firm = Firm.objects.create(slug="citi", name="Citi")
    _grad_role(firm, ["2029"], "2029", title="Only Intern")
    client.force_login(student)
    # The GET first: the confirm writes the ids the banner OFFERED on the last
    # render (see directory.views.BULK_SAVE_OFFER_SESSION_KEY), so a POST with
    # no render behind it has no number to honour and writes nothing.
    client.get("/opportunities/")
    client.post("/opportunities/track-eligible/", {"confirmed": "1"})
    body = client.get("/opportunities/").content.decode()
    assert "names your class year" not in body, "a satisfied offer stops offering"


@pytest.mark.django_db
def test_a_verdict_does_not_repeat_the_fact_that_produced_it(client, student):
    """A visa_out verdict IS `sponsorship == "no"` read against the user, so
    rendering both put "Won't sponsor you" and "No sponsorship" side by side
    saying one thing twice — and the duplicate crowded a real fact (a stated
    grad year) off the end of a three-chip row."""
    firm = Firm.objects.create(slug="ms", name="Morgan Stanley")
    Opportunity.objects.create(
        firm=firm, title="Ops Summer Analyst", bucket="internship", status="open",
        url="https://ms.com/ops", region="us", sponsorship="no",
        raw={"facts": {"grad": {"value": "2027-2028", "years": ["2027", "2028"],
                                "phrase": "graduating 2027-2028"}}})
    client.force_login(student)
    body = client.get("/opportunities/").content.decode()
    card = body[body.index("Ops Summer Analyst"):]
    card = card[:card.index("</article>")] if "</article>" in card else card[:2000]
    assert "sponsor you" in card, "the personalised verdict is the one that stays"
    assert "No sponsorship" not in card, "the fact that produced it must not repeat"
    assert "Grad 2027-2028" in card or "Grad 2027" in card, "the freed slot shows a real fact"


@pytest.mark.django_db
def test_a_blocking_year_verdict_does_not_repeat_the_window_beside_itself(client, student):
    """The year_out twin of the test above, and the one _fact_chips never got:
    "For 2027–2028 grads" (verdict) and "Grad 2027–2028" (fact) are built from
    the same facts["grad"] dict, carry the same source sentence in both
    tooltips, and render in the same grey. 101 of 491 cards on the first feed
    load carried the pair.

    `_FACT_CHIPS_MAX` is 2, so the freed slot has to show a real fact — that is
    the cost the duplication was imposing, not just the repetition."""
    firm = Firm.objects.create(slug="citi", name="Citi")
    Opportunity.objects.create(
        firm=firm, title="Markets Summer Analyst", bucket="internship",
        status="open", url="https://citi.com/markets",
        raw={"facts": {
            "grad": {"value": "2027–2028", "years": ["2027", "2028"],
                     "phrase": "You will graduate between October 2027 and July 2028"},
            "gpa": {"value": "3.0", "phrase": "minimum GPA of 3.0"},
        }})
    client.force_login(student)
    body = client.get("/opportunities/").content.decode()
    card = body[body.index("Markets Summer Analyst"):]
    card = card[:card.index("</article>")] if "</article>" in card else card[:2000]
    assert "For 2027–2028 grads" in card, "the personalised verdict is the one that stays"
    assert "Grad 2027–2028" not in card, "the fact that produced it must not repeat"
    assert "GPA 3.0" in card, "the freed slot shows a real fact"


@pytest.mark.django_db
def test_a_non_blocking_year_verdict_keeps_the_stated_window(client, student):
    """year_ok says "Your year (2029)" and never repeats the window, so the
    fact chip is the ONLY place the posting's own stated years appear. It must
    survive — suppressing it there would delete information rather than a
    duplicate."""
    firm = Firm.objects.create(slug="bofa", name="Bank of America")
    _grad_role(firm, ["2029"], "2029", title="Mine Summer Analyst")
    client.force_login(student)
    body = client.get("/opportunities/").content.decode()
    card = body[body.index("Mine Summer Analyst"):]
    card = card[:card.index("</article>")] if "</article>" in card else card[:2000]
    assert "Your year (2029)" in card
    assert "Grad 2029" in card


@pytest.mark.django_db
def test_an_anonymous_visitor_still_sees_the_stated_window(client):
    """No profile means no verdict, so the fact chip carries the whole story."""
    firm = Firm.objects.create(slug="barclays", name="Barclays")
    _grad_role(firm, ["2027", "2028"], "2027–2028", title="Anon Summer Analyst")
    body = client.get("/opportunities/").content.decode()
    assert "Grad 2027–2028" in body
    assert "grads" not in body.replace("Grad 2027–2028", "")


# ---------------------------------------------------------------------------
# FACT-vs-FACT DEDUPLICATION. `_fact_chips`'s docstring already documents the
# verdict-vs-fact case (year_out suppressing "Grad ..."); these are the two
# duplications where BOTH sides are facts, and the de-dup turned out to be
# value-dependent rather than kind-dependent — see `_standing_matches_grad`
# and `_NON_DISCRIMINATING_STUDY_ON_CAMPUS` in views.py.
# ---------------------------------------------------------------------------

def _facted(firm, facts, *, bucket="internship", **extra):
    return Opportunity.objects.create(
        firm=firm, title="Summer Analyst", bucket=bucket, status="open",
        url=f"https://x.com/{firm.slug}-{bucket}-facted", raw={"facts": facts},
        **extra)


@pytest.mark.django_db
def test_an_agreeing_class_standing_and_grad_year_merge_into_one_chip():
    """"Penultimate year" + "Grad 2028" is one requirement stated twice — 55
    open rows carry both, none of them disagreeing. The merge must not drop
    either sentence: the standing's own phrase has to survive in the tooltip
    of the chip that's left, same as the year_out verdict keeps `grad`'s
    phrase alive in its own `why`."""
    firm = Firm.objects.create(slug="gs2", name="Goldman Sachs 2")
    o = _facted(firm, {
        "study": {"value": "Penultimate year", "phrase": "In your penultimate year of study"},
        "grad": {"value": "2028", "years": ["2028"], "phrase": "graduating in 2028"},
    })
    chips = _fact_chips(o)
    labels = [c["label"] for c in chips]
    assert len(chips) == 1
    assert labels == ["Penultimate year · Grad 2028"]
    assert "penultimate" in chips[0]["why"].lower()
    assert "graduating in 2028" in chips[0]["why"]


@pytest.mark.django_db
def test_a_class_standing_wider_than_its_grad_year_fact_does_not_merge():
    """"Final year" names ONE imminent year. A grad fact spanning three of
    them beside it is a different, wider claim — not the same requirement
    restated — so this must stay two chips rather than quietly picking one."""
    firm = Firm.objects.create(slug="ms3", name="Morgan Stanley 3")
    o = _facted(firm, {
        "study": {"value": "Final year", "phrase": "You are a final year student"},
        "grad": {"value": "2027-2029", "years": ["2027", "2028", "2029"],
                 "phrase": "graduating between 2027 and 2029"},
    })
    chips = _fact_chips(o)
    labels = {c["label"] for c in chips}
    assert labels == {"Final year", "Grad 2027-2029"}, labels


@pytest.mark.django_db
def test_current_student_is_suppressed_on_a_campus_bucket():
    """"Current student" states nothing a campus bucket (insight/internship/
    entry_level) doesn't already establish by being on this feed at all — see
    `_NON_DISCRIMINATING_STUDY_ON_CAMPUS`. A real second fact (GPA) must take
    the freed slot rather than the row rendering only one chip."""
    firm = Firm.objects.create(slug="p72", name="Point72")
    o = _facted(firm, {
        "study": {"value": "Current student", "phrase": "You are a current student"},
        "gpa": {"value": "3.5", "phrase": "minimum GPA of 3.5"},
    }, bucket="entry_level")
    chips = _fact_chips(o)
    labels = [c["label"] for c in chips]
    assert "Current student" not in labels
    assert "GPA 3.5" in labels


@pytest.mark.django_db
def test_current_student_would_still_show_off_the_campus_buckets():
    """The suppression is scoped to the bucket, not a blanket rule on the
    VALUE — `extract_facts` never actually populates `other`-bucket rows
    today (confirmed live: 0 of 13,962), but `_fact_chips` itself must not
    be the thing making that true. Constructed directly, bypassing the
    pipeline, so the render-time gate is what's under test."""
    firm = Firm.objects.create(slug="exp1", name="Experienced Co")
    o = _facted(firm, {
        "study": {"value": "Current student", "phrase": "We also welcome current students"},
    }, bucket="other")
    chips = _fact_chips(o)
    assert [c["label"] for c in chips] == ["Current student"]


@pytest.mark.django_db
def test_recent_graduate_alone_is_not_suppressed_on_a_campus_bucket():
    """Only the two exact non-discriminating values are gated — "Recent
    graduate" alone (34 open rows) is real information on an internship
    bucket, where the default assumption is a still-enrolled student."""
    firm = Firm.objects.create(slug="pwc2", name="PwC 2")
    o = _facted(firm, {
        "study": {"value": "Recent graduate", "phrase": "recent graduates are welcome"},
    }, bucket="internship")
    chips = _fact_chips(o)
    assert [c["label"] for c in chips] == ["Recent graduate"]


def test_no_verdict_is_rendered_as_struck_through_text():
    """Strikethrough reads as NEGATION: struck-out "Won't sponsor you" says
    the opposite of what it means. A closed door is stated, not crossed out."""
    import pathlib as _pl
    css = (_pl.Path(__file__).resolve().parents[2] / "static" / "css" / "coverage.css").read_text()
    for m in re.finditer(r"([^{}]*)\{([^}]*text-decoration:[^;}]*line-through[^;}]*)[;}]", css):
        assert False, f"line-through on {m.group(1).strip()!r}"


# ---------------------------------------------------------------------------
# "Not recently confirmed live" — status=open must not read as unqualified
# confidence when our own last check of the URL couldn't reconfirm it.
#
# Opportunity id=6788 (J.P. Morgan, oracle source) was live, DB-confirmed:
# status='open', last_checked=last_verified=2026-08-14 (a genuine
# verified-open reading that day), and the Oracle posting itself now serves
# "This job is no longer available." A live re-check the next day returned
# oracle.py's "needs-verification" (the requisition no longer surfaces via
# keyword search) — which, correctly, does not flip `status`, because
# absence-from-search is not proof of closure (see oracle.py's documented
# false JPM-4731 closure). But the feed card and the drawer's apply link
# rendered with zero acknowledgement that our last check couldn't reconfirm
# it — status='open' read as full confidence the data didn't support.
# `last_checked` running ahead of `last_verified` is exactly that gap, and
# it is real data every open row already carries (see ingest.py / reverify.py).
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_unconfirmed_note_is_empty_on_a_clean_confirmation():
    """The common case — last_checked caught up with a positive last_verified
    reading (the two stamped equal, as every fresh ingest/verified-open pass
    does) — must say nothing. This is ~all of the 15k+ open rows on live
    data; a note here would be noise on every card."""
    firm = Firm.objects.create(slug="citi", name="Citi")
    o = _opp(firm, "https://citi.com/clean")
    Opportunity.objects.filter(pk=o.pk).update(last_checked=NOW, last_verified=NOW)
    o.refresh_from_db()
    assert _unconfirmed_note(o) == {}


@pytest.mark.django_db
def test_unconfirmed_note_is_empty_with_no_check_history():
    """A row that has never been checked (both timestamps null) has nothing
    to compare — silence, not a false alarm."""
    firm = Firm.objects.create(slug="citi2", name="Citi 2")
    o = _opp(firm, "https://citi.com/never-checked")
    assert o.last_checked is None and o.last_verified is None
    assert _unconfirmed_note(o) == {}


@pytest.mark.django_db
def test_unconfirmed_note_fires_when_the_latest_check_ran_ahead_of_confirmation():
    """The id=6788 shape: an earlier check confirmed it live, a LATER check
    ran and could not — needs-verification or unreachable, never `closed`
    for this exact reason (see oracle.py). `last_checked` moves past
    `last_verified` and stays there; that gap is the honest signal."""
    firm = Firm.objects.create(slug="jpm-oracle", name="J.P. Morgan")
    o = _opp(firm, "https://jpmc.fa.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1001/job/210763228")
    Opportunity.objects.filter(pk=o.pk).update(
        last_verified=NOW - timedelta(days=1), last_checked=NOW)
    o.refresh_from_db()
    note = _unconfirmed_note(o)
    assert note["label"] == "Not recently confirmed live"
    assert "status" not in note["label"].lower()  # names the gap, not a status claim
    assert o.status == "open"  # the note supplements "open"; it never overrides it


@pytest.mark.django_db
def test_unconfirmed_note_is_empty_once_status_is_actually_closed():
    """A row whose `status` has been flipped to "closed" has closed_at set
    precisely because a check DID confirm closure (models.py's comment on
    `closed_at`) — the exact opposite of this note's premise ("status stays
    open because absence isn't proof of closure"). Same last_checked >
    last_verified shape as id=6788 above, but status='closed' this time:
    the note must go silent, not repeat "it still shows as open because we
    also can't confirm it closed" for a row the DB itself marks closed."""
    firm = Firm.objects.create(slug="td-closed", name="TD Securities")
    o = Opportunity.objects.create(
        firm=firm, url="https://td.wd3.myworkdayjobs.com/job/closed",
        title="Banking Associate", bucket="internship", status="closed")
    Opportunity.objects.filter(pk=o.pk).update(
        last_verified=NOW - timedelta(days=1), last_checked=NOW, closed_at=NOW)
    o.refresh_from_db()
    assert _unconfirmed_note(o) == {}


@pytest.mark.django_db
def test_the_feed_card_marks_a_title_link_it_cannot_currently_vouch_for(client):
    """The card's title link is the primary discovery surface — a student
    can leave Coverage from it without ever opening the Read drawer or
    reaching a separate Apply button. It must carry the caution, not just
    the drawer behind it."""
    firm = Firm.objects.create(slug="jpm", name="J.P. Morgan")
    o = _opp(firm, "https://jpmc.fa.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1001/job/210763228")
    Opportunity.objects.filter(pk=o.pk).update(
        last_verified=NOW - timedelta(days=1), last_checked=NOW)

    body = client.get("/opportunities/").content.decode()
    assert "is-unconfirmed" in body
    # Not sighted-only: a `title` attribute alone is not an accessible
    # carrier (same rule the deadline provenance mark follows elsewhere on
    # this page).
    assert "(Not recently confirmed live)" in body


@pytest.mark.django_db
def test_a_freshly_confirmed_card_wears_no_caution(client):
    """Negative case, with the page's own inline <style> stripped first — the
    CSS rule for `.is-unconfirmed` ships on every page load regardless of
    whether any card uses it, so a bare substring check would pass either
    way (same trap `test_a_provider_stated_deadline_carries_no_mark` guards
    against for `is-reported`)."""
    firm = Firm.objects.create(slug="citi3", name="Citi 3")
    o = _opp(firm, "https://citi.com/fresh")
    Opportunity.objects.filter(pk=o.pk).update(last_checked=NOW, last_verified=NOW)

    body = re.sub(r"<style.*?</style>", "",
                  client.get("/opportunities/").content.decode(), flags=re.S)
    assert "Summer Analyst" in body
    assert "is-unconfirmed" not in body


@pytest.mark.django_db
def test_the_drawer_names_the_gap_instead_of_an_unqualified_apply_link(client):
    """The drawer's apply link is deliberately sticky and unmissable (see
    _role_drawer.html) — exactly why it must not render with unqualified
    confidence when the last check of this URL could not reconfirm it."""
    firm = Firm.objects.create(slug="jpm2", name="J.P. Morgan")
    o = _opp(firm, "https://jpmc.fa.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1001/job/210763228")
    Opportunity.objects.filter(pk=o.pk).update(
        last_verified=NOW - timedelta(days=1), last_checked=NOW)

    body = client.get(reverse("role_description", args=[o.id])).content.decode()
    assert "drawer-caution" in body
    assert "Not recently confirmed live" in body
    # status stays open; the drawer says so honestly rather than inventing a
    # closure the data doesn't support.
    assert "Open the application on" in body


@pytest.mark.django_db
def test_the_drawer_stays_silent_for_a_freshly_confirmed_role(client):
    firm = Firm.objects.create(slug="citi4", name="Citi 4")
    o = _opp(firm, "https://citi.com/fresh-drawer")
    Opportunity.objects.filter(pk=o.pk).update(last_checked=NOW, last_verified=NOW)

    body = client.get(reverse("role_description", args=[o.id])).content.decode()
    assert "drawer-caution" not in body


# ---------------------------------------------------------------------------
# AN INEXACT DATE MAY NOT CARRY AN EXACT COUNTDOWN.
#
# `deadline_marker` renders "~ Sep 2026" for `precision="estimated"` and
# "Sep 2026" for `precision="month"` — a deliberate refusal to name a day —
# and then returned `countdown="closes in 4 days"` in the same dict. One row,
# two claims about the same date, and the reader believes the specific one.
#
# REACHABILITY, since it decides whether this is a renderer fix or a
# constraint: `Opportunity.deadline_precision` is a bare `CharField` with no
# vocabulary CHECK, and `directory.admin.OpportunityAdmin` declares no
# `fields`, `exclude` or `readonly_fields` — so every column on the model is
# editable in its admin change form, this one included. That is the same
# unbounded-ModelAdmin path `opportunities_confidence_in_range` was added for
# after the `confidence=95.0` write. The value is one admin save (or one
# `manage.py shell` line) away, and `FirmDate` — same vocabulary, same
# meaning, closed to it by `firm_dates_precision_vocabulary` — already holds
# 25 `estimated` rows live. So: fix the rendering. A constraint here could
# only ban a garbage string, and "estimated" is not garbage; it is a
# legitimate value the renderer was mishandling.
# ---------------------------------------------------------------------------

from datetime import date as _date  # noqa: E402

from directory.views import deadline_marker  # noqa: E402


def test_a_null_deadline_says_no_date_posted():
    """Cross-surface consistency audit, finding F: `deadline_marker` said
    "No deadline posted" here until 2026-09-01, a third wording for the same
    fact the feed states as "No date posted, first seen Nd ago" and My
    Applications states as bare "No date posted". Standardized on the base
    phrase both of those already used — see the module-level
    `test_feed_and_firm_page_agree_on_no_date_posted_wording` for the
    firm-page rendering and the judgment call on why the "first seen" clause
    does not also move here."""
    m = deadline_marker(None, "")
    assert m["label"] == "No date posted"
    assert m["posted"] is False


def test_an_estimated_deadline_gets_no_day_count():
    m = deadline_marker(_date(2026, 9, 20), "estimated", today=_date(2026, 9, 16))
    assert m["label"] == "~ Sep 2026"
    assert "4 days" not in m["countdown"], (
        "a day count on a date the label just refused to print as a day")
    assert m["countdown"] == "estimated this month"


def test_a_month_precision_deadline_gets_no_day_count():
    m = deadline_marker(_date(2026, 9, 20), "month", today=_date(2026, 9, 16))
    assert m["label"] == "Sep 2026"
    assert "days" not in m["countdown"]
    assert m["countdown"] == "closes this month"


def test_an_inexact_deadline_counts_in_its_own_unit():
    assert deadline_marker(_date(2026, 11, 3), "month",
                           today=_date(2026, 9, 28))["countdown"] == "closes in 2 months"
    assert deadline_marker(_date(2026, 10, 3), "month",
                           today=_date(2026, 9, 28))["countdown"] == "closes next month"
    assert deadline_marker(_date(2027, 3, 1), "estimated",
                           today=_date(2026, 9, 28))["countdown"] == "estimated in 6 months"


def test_an_inexact_deadline_is_not_past_until_its_whole_unit_is():
    """The lie has a second direction. A "Sep 2026" date is not overdue on
    Sep 30 — nothing ever said which September day it was — so the
    danger-red `past` styling would be asserting a day too."""
    m = deadline_marker(_date(2026, 9, 1), "month", today=_date(2026, 9, 30))
    assert m["past"] is False
    assert m["countdown"] == "closes this month"

    gone = deadline_marker(_date(2026, 9, 1), "month", today=_date(2026, 10, 1))
    assert gone["past"] is True
    assert gone["countdown"] == "deadline passed"

    est = deadline_marker(_date(2026, 9, 1), "estimated", today=_date(2026, 10, 1))
    assert est["past"] is True
    assert est["countdown"] == "estimated date passed"


def test_a_day_precise_deadline_still_counts_days():
    """The fix must not coarsen the 633 live rows that DO name a day."""
    for prec in ("day", "", None):
        m = deadline_marker(_date(2026, 9, 20), prec, today=_date(2026, 9, 16))
        assert m["label"] == "Sep 20, 2026"
        assert m["countdown"] == "closes in 4 days"
        assert m["past"] is False
    assert deadline_marker(_date(2026, 9, 16), "day",
                           today=_date(2026, 9, 16))["countdown"] == "closes today"
    assert deadline_marker(_date(2026, 9, 17), "day",
                           today=_date(2026, 9, 16))["countdown"] == "closes tomorrow"
    past = deadline_marker(_date(2026, 9, 10), "day", today=_date(2026, 9, 16))
    assert past["countdown"] == "deadline passed" and past["past"] is True


@pytest.mark.django_db
def test_an_estimated_deadline_never_reaches_the_firm_page_as_a_day_count(client):
    """End to end, through `_card` — the reader that actually calls
    `deadline_marker` and prints `countdown|capfirst` on the firm page.

    The Opportunities FEED is deliberately not asserted here: `_urgency_item`
    builds its own countdown and does NOT go through `deadline_marker`, so it
    still prints "Closes in 4 days" for this row. That is a real second
    instance of the same bug, left alone on purpose — see the comment on
    `_urgency_item`'s inexact-precision branch for why it is not a local
    edit."""
    firm = Firm.objects.create(slug="ubs", name="UBS")
    Opportunity.objects.create(
        firm=firm, title="Guessed Analyst", bucket="internship", status="open",
        deadline=TODAY + timedelta(days=4), deadline_precision="estimated",
        confidence=0.6, url="https://ubs.com/estimated")

    body = re.sub(r"<style.*?</style>", "",
                  client.get("/firms/ubs/").content.decode(), flags=re.S)
    assert "Guessed Analyst" in body
    assert "in 4 days" not in body.lower()
    # "this month" or "next month" depending on where +4 days lands; the
    # assertion that matters is the word, not which of the two.
    assert "Estimated" in body


# ---------------------------------------------------------------------------
# The silent-staleness gap: a board that fails OUTRIGHT froze both timestamps
# together, so the relative `last_checked > last_verified` test above went
# quiet on exactly the rows it existed to warn about.
#
# Measured on live data 2026-09-01: 92 open campus rows more than 3 days
# stale, 37 of them silent under the relative test alone, 23 of those still
# rendering a live countdown. Every one of the 23 was HSBC, whose board had
# been throwing an SSL error since 2026-08-25 — one card read "closes in 2
# days" off a page last successfully read six days earlier. `health.py` had
# been printing "stale data being presented as fresh" for that firm to an
# operator-only channel the whole time.
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_unconfirmed_note_fires_when_a_dead_board_froze_both_stamps():
    """The HSBC shape. No check ever FAILED — the connector stopped running
    at all, so `last_checked` never ran ahead of `last_verified` and the
    relative test read the row as cleanly confirmed. Absolute age is the only
    evidence left, and it has to be enough."""
    firm = _firm(slug="hsbc-dead", name="HSBC")
    o = _opp(firm, "https://hsbc.com/frozen", deadline=(NOW + timedelta(days=2)).date())
    stamp = NOW - timedelta(days=6)
    Opportunity.objects.filter(pk=o.pk).update(last_checked=stamp, last_verified=stamp)
    o.refresh_from_db()

    note = _unconfirmed_note(o)
    assert note, "a six-day-old confirmation with a live countdown must not read as fresh"
    assert note["label"] == "Not recently confirmed live"
    # It names the age, because "could not confirm" would be a false account
    # of what happened: nothing came back unable to confirm, nothing ran.
    assert "6 days" in note["why"]
    assert "could not confirm it is" not in note["why"]


@pytest.mark.django_db
def test_unconfirmed_note_stays_quiet_on_a_recent_clean_confirmation():
    """The other half of the same rule. Absolute age must not turn the note
    into noise on every card: a row confirmed yesterday, by a check that
    succeeded, says nothing. Without this the threshold could drift down to
    where 2,700 rows all carry a warning and the warning stops meaning
    anything."""
    firm = _firm(slug="ubs-fresh", name="UBS")
    o = _opp(firm, "https://ubs.com/fresh")
    stamp = NOW - timedelta(days=1)
    Opportunity.objects.filter(pk=o.pk).update(last_checked=stamp, last_verified=stamp)
    o.refresh_from_db()
    assert _unconfirmed_note(o) == {}


@pytest.mark.django_db
def test_unconfirmed_note_still_names_a_failed_check_as_a_failed_check():
    """A check that RAN and came back unable to reconfirm is a different fact
    from a board we could not reach, and keeps its own wording even once the
    row is also old enough to trip the absolute test. Both are true; the
    specific one is the honest one."""
    firm = _firm(slug="jpm-failed", name="J.P. Morgan")
    o = _opp(firm, "https://jpmc.oraclecloud.com/failed")
    Opportunity.objects.filter(pk=o.pk).update(
        last_verified=NOW - timedelta(days=9), last_checked=NOW)
    o.refresh_from_db()
    note = _unconfirmed_note(o)
    assert "could not confirm it is" in note["why"]
    assert "9 days" not in note["why"]


# --------------------------------------------------------------------------- #
# One card built per row (2026-09-01)
#
# The Opportunities view rendered the urgency band and the firm columns off
# one `rows` list and built a card per row in EACH: 5,166 `_urgency_item`
# calls at campus scope on the founder's board and 30,068 at `?role=all`, for
# a dict that is a pure function of the row and the request clock. It is built
# once now and the band takes copies.
#
# Two things have to stay true, and they pull against each other. The cards
# must be IDENTICAL to what two independent builds produced — this is a speed
# change and nothing else. And the band's card must not be the clusters' card
# OBJECT, because the cluster pass mutates its dicts afterwards (the save-star
# and the city-variant grouping) and the band never showed either.
# --------------------------------------------------------------------------- #

def _bands(rows, **kw):
    return _urgency_feed(rows, now=NOW, today=TODAY, my_firm_ids=set(), **kw)


def _titled(firm, url, title, **kw):
    """`_opp` with the title free. Distinct titles matter here: the feed folds
    repeat listings (`directory.dupes`), so three rows sharing a title at one
    firm reach the render as one and a per-row count would prove nothing."""
    return Opportunity.objects.create(
        firm=firm, url=url, title=title, bucket="internship", status="open",
        region="", **kw,
    )


@pytest.mark.django_db
def test_prebuilt_items_give_the_band_exactly_what_it_built_itself():
    """The equivalence, band by band and key by key. A supplied `items` map
    must change the COST of the call and nothing else."""
    firm = _firm(slug="one-build", name="One Build")
    dated = _titled(firm, "https://one-build.com/1", "Dated Analyst",
                    deadline=TODAY + timedelta(days=5))
    rolling = _titled(firm, "https://one-build.com/2", "Rolling Analyst")
    _seen(rolling, 2)
    rows = [dated, rolling]

    # One `cutoffs` map on both sides, because that argument is what the view
    # passes and it is the only input that changes a card without changing the
    # row (`open_run_days`). Leaving it to default on one side would compare
    # two different requests, not two ways of serving one.
    from directory.open_runs import onboarding_cutoffs

    cutoffs = onboarding_cutoffs({o.firm_id for o in rows})
    on_its_own = _bands(rows, cutoffs=cutoffs)
    prebuilt = {o.id: _urgency_item(o, now=NOW, today=TODAY, my_firm_ids=set(),
                                    cutoffs=cutoffs)
                for o in rows}
    supplied = _bands(rows, cutoffs=cutoffs, items=prebuilt)
    assert supplied == on_its_own


@pytest.mark.django_db
def test_the_bands_card_is_a_copy_not_the_callers_card():
    """The band's cards are COPIES. Handing it the caller's own dicts would
    have been the faster change and a wrong one: the `opportunities` view
    mutates those dicts after this call — `_group_city_variants` writes
    `variants`/`in_group` and the save-star pass writes `track_status` — and
    a band card that suddenly grew a star or a "+N more locations" disclosure
    would be this optimisation leaking onto the page."""
    firm = _firm(slug="no-bleed", name="No Bleed")
    o = _titled(firm, "https://no-bleed.com/1", "Copy Me")
    prebuilt = {o.id: _urgency_item(o, now=NOW, today=TODAY, my_firm_ids=set())}

    band = _bands([o], items=prebuilt)
    card = (band["closing"] + band["rolling"])[0]
    assert card == prebuilt[o.id]
    assert card is not prebuilt[o.id]

    prebuilt[o.id]["track_status"] = "saved"
    assert "track_status" not in card


@pytest.mark.django_db
def test_one_urgency_item_per_row(client, monkeypatch):
    """The count, which is the whole point: three rows used to cost six
    builds, one for the urgency band and one for the firm column."""
    from directory import views

    firm = _firm(slug="count-builds", name="Count Builds")
    for i in range(3):
        _titled(firm, f"https://count-builds.com/{i}", f"Analyst {i}")

    built = []
    real = views._urgency_item

    def counting(o, **kw):
        built.append(o.id)
        return real(o, **kw)

    monkeypatch.setattr(views, "_urgency_item", counting)
    client.get(reverse("opportunities"))
    assert len(built) == len(set(built)) == 3, built


@pytest.mark.django_db
def test_the_card_asks_for_its_eligibility_verdict_once(client, monkeypatch):
    """`facts` and `verdict` are the same `_eligibility` call, and it walks
    `raw.facts`. Asking twice per card doubled that walk for two answers that
    are equal by construction."""
    from directory import views

    firm = _firm(slug="one-verdict", name="One Verdict")
    o = _opp(firm, "https://one-verdict.com/1",
             raw={"facts": {"grad": {"value": "2028", "years": ["2028"],
                                     "phrase": "graduating in 2028"}}})

    calls = []
    real = views._eligibility

    def counting(opp, profile):
        calls.append(opp.id)
        return real(opp, profile)

    monkeypatch.setattr(views, "_eligibility", counting)
    item = _urgency_item(o, now=NOW, today=TODAY, my_firm_ids=set(),
                         profile={"class_year": 2028, "work_auth": {},
                                  "languages": [], "study_level": ""})
    assert calls == [o.id]
    assert item["verdict"]["kind"] == "year_ok"


# ---------------------------------------------------------------------------
# ABSOLUTE FRESHNESS ON THE CARD (WS-OPP-07).
#
# The countdown in the deadline column is a claim about a page Coverage read
# at SOME point, and until now the only place the product said when was the
# drawer — which a student reaches by opening it. Measured 2026-09-01: a
# quarter of the rows under Today's "Closing in 10 days" ribbon were counting
# down off pages six days old, 23 of them HSBC rows whose board had been
# throwing an SSL error since 2026-08-25.
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_a_stale_row_prints_its_age_where_the_countdown_is(client):
    """Six days old: the age is VISIBLE text, not only a tooltip."""
    firm = Firm.objects.create(slug="hsbc-stale", name="HSBC Stale")
    o = _opp(firm, "https://hsbc.test/stale", deadline=TODAY + timedelta(days=2))
    Opportunity.objects.filter(pk=o.pk).update(
        last_verified=NOW - timedelta(days=6), last_checked=NOW - timedelta(days=6))

    body = _STYLE_RE.sub("", client.get("/opportunities/").content.decode())
    assert "6d old" in body, (
        "a row last confirmed six days ago must say so beside its countdown"
    )
    # Not sighted-only, same rule the provenance mark follows on this page.
    assert "Last confirmed live 6 days ago" in body


@pytest.mark.django_db
def test_a_row_verified_today_prints_no_age(client):
    """The negative half, and the reason the threshold is borrowed rather
    than invented: 2,627 of 2,723 open campus rows were verified inside 24
    hours on 2026-09-01, so a mark on every row would mark nothing."""
    firm = Firm.objects.create(slug="citi-fresh-age", name="Citi Fresh Age")
    o = _opp(firm, "https://citi.test/fresh-age", deadline=TODAY + timedelta(days=2))
    Opportunity.objects.filter(pk=o.pk).update(last_verified=NOW, last_checked=NOW)

    body = _STYLE_RE.sub("", client.get("/opportunities/").content.decode())
    assert "d old" not in body
    # …but the age is still on the deadline column's own tooltip, on every
    # row that has one. That half is unconditional.
    assert "Coverage last confirmed this posting live today" in body


@pytest.mark.django_db
def test_the_visible_age_uses_the_unconfirmed_notes_own_threshold():
    """P5. The template's condition is `r.unconfirmed`, so the "how stale is
    too stale" number lives in exactly one place — `_UNCONFIRMED_AFTER_DAYS`,
    itself borrowed from `health.py`'s CONSECUTIVE_FAILURES so the operator
    alarm and the student-facing note fire on the same evidence. A second
    comparison in the template would be a third idea of stale."""
    from directory.views import _UNCONFIRMED_AFTER_DAYS

    firm = Firm.objects.create(slug="threshold", name="Threshold")
    o = _opp(firm, "https://threshold.test/1")

    just_under = _UNCONFIRMED_AFTER_DAYS - 1
    Opportunity.objects.filter(pk=o.pk).update(
        last_verified=NOW - timedelta(days=just_under),
        last_checked=NOW - timedelta(days=just_under))
    o.refresh_from_db()
    assert _unconfirmed_note(o) == {}

    Opportunity.objects.filter(pk=o.pk).update(
        last_verified=NOW - timedelta(days=_UNCONFIRMED_AFTER_DAYS),
        last_checked=NOW - timedelta(days=_UNCONFIRMED_AFTER_DAYS))
    o.refresh_from_db()
    note = _unconfirmed_note(o)
    assert note["days"] == _UNCONFIRMED_AFTER_DAYS, (
        "the note must carry the number as a number so a template can print "
        "it rather than dig it out of the prose"
    )


# ---------------------------------------------------------------------------
# THE DEADLINE LAYER'S OWN DOCUMENTATION (WS-OPP-15).
#
# Two bare numbers in a comment went stale twice: "92 of the 121" survived
# until the board had nearly trebled (2.8x off), and the "327 of the 341"
# that replaced it was 8% off within a day. A share of the board is not a
# constant, so the figures now live in one dated object and the claim they
# make is tied to the code path that produces it.
#
# WHY THIS IS NOT A LIVE RE-COUNT. The audit's preferred criterion was a test
# asserting the comment's number equals the live count. That is not available
# to a pytest: the suite runs against an empty per-worktree test database
# (`settings/base.py`'s `TEST.NAME`), so such a test would assert 0 == 354 and
# the only way to make it pass would be to weaken it into nothing. What is
# available, and is what these do, is to pin the DATE and the PREDICATE — the
# numbers may go stale, but they can never quietly start describing a
# different question than the code asks.
# ---------------------------------------------------------------------------

def test_the_prose_read_measurement_carries_the_day_it_was_taken():
    from directory.views import PROSE_READ_DEADLINES

    measured = PROSE_READ_DEADLINES["measured_on"]
    assert isinstance(measured, date), "the measurement must carry a real date"
    assert measured <= TODAY, "a measurement cannot have been taken in the future"
    assert (PROSE_READ_DEADLINES["prose_read"]
            <= PROSE_READ_DEADLINES["dated_open_campus"]), (
        "the prose-read subset cannot exceed the dated set it is a subset of"
    )
    assert PROSE_READ_DEADLINES["query"], (
        "the measurement must name the predicate it counted, or re-taking it "
        "is a reconstruction rather than a re-run"
    )


@pytest.mark.django_db
def test_the_measurements_predicate_is_the_one_the_code_branches_on():
    """The number claims something about `confidence < _CONFIRMED_AT` on
    dated rows. This asserts `deadline_provenance` — the function the whole
    marker rests on — splits on exactly that, so the comment and the code
    cannot drift into describing different populations."""
    from directory.views import (
        _CONFIRMED_AT, PROSE_READ_DEADLINES, deadline_provenance,
    )

    assert "confidence < _CONFIRMED_AT" in PROSE_READ_DEADLINES["query"]
    assert "deadline is not null" in PROSE_READ_DEADLINES["query"]

    firm = Firm.objects.create(slug="prov-pred", name="Prov")
    dated_prose = _opp(firm, "https://prov.test/1", deadline=TODAY + timedelta(days=5))
    dated_prose.confidence = _CONFIRMED_AT - 0.4
    dated_stated = _opp(firm, "https://prov.test/2", deadline=TODAY + timedelta(days=5))
    dated_stated.confidence = _CONFIRMED_AT
    undated = _opp(firm, "https://prov.test/3")
    undated.confidence = _CONFIRMED_AT - 0.4

    assert deadline_provenance(dated_prose)["label"] == "reported"
    assert deadline_provenance(dated_stated) is None
    assert deadline_provenance(undated) is None


def test_the_inexact_precision_guard_is_marked_as_one():
    """`_INEXACT_PRECISIONS` gates a rendering path with zero live rows and a
    reader will assume it is live. It stays — the column has no vocabulary
    constraint and a fully editable admin over it, so a `month` value is one
    save away — but it is labelled, and this pins that the label is there
    with the day it was checked."""
    import inspect

    from directory import views

    src = inspect.getsource(views)
    block = src.split("_INEXACT_PRECISIONS = ")[0][-2000:]
    assert "GUARD, NOT A LIVE PATH" in block, (
        "the zero-live-rows note has gone; a reader will assume the branch "
        "below it is reached on today's data"
    )
    assert re.search(r"20\d\d-\d\d-\d\d", block), (
        "the guard's note must carry the date its zero was measured"
    )


def test_the_today_ribbon_reads_one_definition_of_closing_soon():
    """P5, enforced by absence. `crm/` must not spell the closing-soon window
    itself: `deadline__range=(today, today + timedelta(days=9))` was the
    second, inline copy of the arithmetic `directory.deadlines` exists to
    own, and two copies of a window are two answers to "is this closing
    soon". Walked over the source files rather than through `git grep`, so
    the check works in a worktree, a shallow clone or an unpacked tarball."""
    from pathlib import Path

    crm = Path(__file__).resolve().parents[2] / "crm"
    hits = [
        f"{path.relative_to(crm.parent)}:{n}"
        for path in sorted(crm.rglob("*.py"))
        for n, line in enumerate(path.read_text().splitlines(), 1)
        if "deadline__range" in line and not line.lstrip().startswith("#")
    ]
    assert hits == [], f"crm/ re-derives the closing-soon window: {hits}"


def test_the_today_ribbons_qualifier_names_the_reported_count():
    """The ribbon's other half of the same honesty. 96% of dated open campus
    deadlines are Coverage's own reading of the posting's prose, so an
    unqualified urgent number over that window is mostly reporting our own
    reading back as the market's calendar."""
    from pathlib import Path

    tpl = (Path(__file__).resolve().parents[2]
           / "templates" / "crm" / "week.html").read_text()
    assert "closing_10_reported" in tpl, (
        "the ribbon must qualify its count with how many of those dates are "
        "our own reading of the posting's prose"
    )
    assert "reported" in tpl


# ---------------------------------------------------------------------------
# A POSTING NOBODY TOOK DOWN (WS-OPP-14).
#
# Two open campus rows sit past their own stated deadline and both are
# `last_verified` today, so the firms genuinely still list them. The row must
# NOT be closed — 11 of 17 Citi postings that stated a close date were still
# live past it, one by eight months — so only the affordance changes.
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_a_long_abandoned_row_loses_its_save_button_and_says_why(client, django_user_model):
    """261 days past, still listed. The Save button goes; the note and the
    outbound link stay."""
    user = django_user_model.objects.create_user(
        email="abandon@example.com", password="x" * 14)
    client.force_login(user)

    firm = Firm.objects.create(slug="stifel-old", name="Stifel Old")
    o = _opp(firm, "https://stifel.test/old", deadline=TODAY - timedelta(days=261))
    Opportunity.objects.filter(pk=o.pk).update(last_verified=NOW, last_checked=NOW)

    body = _STYLE_RE.sub("", client.get("/opportunities/").content.decode())
    assert "Looks abandoned" in body
    assert "track-abandoned" in body
    assert ">\n      Save\n    </button>" not in body and "Save this role" not in body, (
        "the product must not offer to put a posting nobody has taken down "
        "into a student's pipeline"
    )
    # The link out is untouched: the firm's page is still the record.
    assert "https://stifel.test/old" in body
    # And nothing was closed.
    o.refresh_from_db()
    assert o.status == "open"
    assert Opportunity.objects.filter(status="closed").count() == 0


@pytest.mark.django_db
def test_a_row_one_day_past_its_deadline_keeps_its_save_button(client, django_user_model):
    """The case the 30-day threshold exists to protect. A stated deadline is
    a plan, not an event: Citi labels the datum "Anticipated Posting Close
    Date" and most postings stating one were still live past it."""
    user = django_user_model.objects.create_user(
        email="recent@example.com", password="x" * 14)
    client.force_login(user)

    firm = Firm.objects.create(slug="accenture-new", name="Accenture New")
    o = _opp(firm, "https://accenture.test/new", deadline=TODAY - timedelta(days=1))
    Opportunity.objects.filter(pk=o.pk).update(last_verified=NOW, last_checked=NOW)

    body = _STYLE_RE.sub("", client.get("/opportunities/").content.decode())
    assert "Save this role" in body
    assert "Looks abandoned" not in body


@pytest.mark.django_db
def test_the_abandoned_verdict_has_one_definition():
    """P5. The feed card and the htmx swap that re-renders one card's control
    both need this answer, and the day they compute their own is the day a
    student un-saves such a row and is handed back the Save button the feed
    had just withheld."""
    from directory.views import _ABANDONED_AFTER_DAYS, _abandoned_note

    firm = Firm.objects.create(slug="one-def", name="One Def")
    inside = _opp(firm, "https://one-def.test/inside",
                  deadline=TODAY - timedelta(days=_ABANDONED_AFTER_DAYS))
    outside = _opp(firm, "https://one-def.test/outside",
                   deadline=TODAY - timedelta(days=_ABANDONED_AFTER_DAYS + 1))
    undated = _opp(firm, "https://one-def.test/undated")
    future = _opp(firm, "https://one-def.test/future",
                  deadline=TODAY + timedelta(days=5))

    assert _abandoned_note(inside) == {}
    assert _abandoned_note(outside)["days"] == _ABANDONED_AFTER_DAYS + 1
    assert _abandoned_note(undated) == {}
    assert _abandoned_note(future) == {}

    # A row the scraper has confirmed dead already has its own honest
    # message and must not get a second, weaker one guessing at the same
    # thing.
    Opportunity.objects.filter(pk=outside.pk).update(status="closed")
    outside.refresh_from_db()
    assert _abandoned_note(outside) == {}


@pytest.mark.django_db
def test_an_already_saved_abandoned_row_keeps_the_control_that_undoes_it(client, django_user_model):
    """Withdrawing a control someone has already used would strand their own
    row, which is a worse failure than the one this fixes."""
    from analytics.models import UserOpportunity

    user = django_user_model.objects.create_user(
        email="saved-abandon@example.com", password="x" * 14)
    client.force_login(user)

    firm = Firm.objects.create(slug="saved-old", name="Saved Old")
    o = _opp(firm, "https://saved-old.test/1", deadline=TODAY - timedelta(days=100))
    UserOpportunity.all_objects.create(user=user, opportunity=o, applied_status="saved")

    body = _STYLE_RE.sub("", client.get("/opportunities/").content.decode())
    assert "Remove from saved" in body


# ---------------------------------------------------------------------------
# HOW A FIRM ACTUALLY HIRES (WS-OPP-04).
#
# `Firm.recruiting_style` has been a column since the CRM half shipped and
# reached no opportunity surface at all: 302 open campus rows at 15
# assessment firms carried the same framing as a Citi row.
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_the_test_gated_chip_renders_at_an_assessment_firm(client):
    firm = Firm.objects.create(slug="sig", name="SIG",
                               recruiting_style=Firm.RECRUITING_STYLE_ASSESSMENT)
    _opp(firm, "https://sig.test/1")

    body = _STYLE_RE.sub("", client.get("/opportunities/").content.decode())
    assert "Test-gated" in body
    assert "hires by assessment" in body or "test or competition" in body
    # It points at the firm page, where the fuller answer lives.
    assert 'href="/firms/sig/"' in body or "/sig/" in body


@pytest.mark.django_db
def test_the_test_gated_chip_does_not_render_at_a_campus_firm(client):
    firm = Firm.objects.create(slug="citi-campus", name="Citi Campus",
                               recruiting_style=Firm.RECRUITING_STYLE_CAMPUS)
    _opp(firm, "https://citi.test/campus")

    body = _STYLE_RE.sub("", client.get("/opportunities/").content.decode())
    assert "Test-gated" not in body


#: The claims no surface may make about networking at a test-gated firm.
#: `research-st-quant.md` Q3 (Grade A) supports "the firm hires by
#: assessment" and supports nothing stronger: it records that no mechanism
#: from a chat into the pipeline is documented, NOT that a chat is harmful.
#: The gap between those two sentences is the whole reason the chip is
#: allowed to exist at all.
_NETWORKING_OVERCLAIMS = re.compile(
    r"networking (?:does not|doesn.t) help"
    r"|hurts your odds"
    r"|networking hurts"
    r"|networking is counterproductive"
    r"|(?:don.t|do not) (?:bother|waste)",
    re.IGNORECASE,
)


@pytest.mark.django_db
def test_the_product_never_says_networking_hurts(client, django_user_model):
    """The limit the source itself states, checked on RENDERED COPY.

    Deliberately not a `git grep` over the source, which is what the audit
    criterion suggested and what a first pass here did: the phrases appear
    all over this repository's comments precisely BECAUSE they are the thing
    being forbidden, so a source grep fails on its own explanation of itself
    and would push the reasoning out of the files that need it. What matters
    is what a student reads, so this renders the two surfaces that can carry
    the claim — the feed card with the chip on it, and the scorer's own
    reason text — and reads those.
    """
    user = django_user_model.objects.create_user(
        email="no-overclaim@example.com", password="x" * 14)
    user.tracks = ["st"]
    user.regions = ["us"]
    user.save()
    client.force_login(user)

    firm = Firm.objects.create(slug="janestreet", name="Jane Street",
                               tracks=["st"],
                               recruiting_style=Firm.RECRUITING_STYLE_ASSESSMENT)
    _opp(firm, "https://janestreet.test/1", region="us")

    body = _STYLE_RE.sub("", client.get("/opportunities/").content.decode())
    assert "Test-gated" in body, "the chip must be on the page under test"
    hit = _NETWORKING_OVERCLAIMS.search(body)
    assert hit is None, f"the rendered feed overclaims about networking: {hit!r}"

    # The scorer's own reason text, which reaches the card, the digest email
    # and the pick tooltip. Read straight off the axis rather than off a
    # page, so a surface that starts rendering it later is covered too.
    from directory.recommend import Candidate, Profile, _network_fit

    profile = Profile.from_user(user, {}, warm_firms={firm.id: "warm"})
    points, reasons = _network_fit(
        profile,
        Candidate(id=1, firm_id=firm.id, firm_name=firm.name,
                  firm_slug=firm.slug, title="Quant Trader", url="https://x/1",
                  recruiting_style="assessment"),
    )
    assert points == 0
    for r in reasons:
        assert _NETWORKING_OVERCLAIMS.search(f"{r.text} {r.detail}") is None, r
