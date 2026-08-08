"""The Opportunities filter bar: the segmented Role Type control, the counted
Region/Track facets, and the out-of-band count refresh.

These pin four defects the redesign exists to fix, each of which is invisible
in a screenshot and silent at runtime:

  1. THE MODE-RESET BUG. `?role=all` used to render a control with no member
     selected, so the next htmx GET carried no `role` at all and the view
     silently fell back to the campus scope — 3,456 roles vanishing mid-
     interaction because a radio group with nothing checked serializes nothing.
  2. THE STALE-COUNT BUG. The bar sits outside `#cov-results`, so every count
     in it froze at page-load values after the first htmx swap. The fix is an
     out-of-band fragment, which must ship on htmx requests and only then.
  3. REGION HIDING A THIRD OF THE INVENTORY. 297 of 886 open campus roles
     resolve to no region; picking a market used to delete them with no trace.
  4. THE SUBSET SENTENCE reaching the reader. It used to render below 56 firm
     cards, i.e. only to people who had already finished reading the page.
"""

from __future__ import annotations

import re

import pytest
from django.urls import reverse

from directory.models import Firm, Opportunity
from directory.views import REGION_NONE, YEAR_NONE

pytestmark = pytest.mark.django_db


def _firm(slug="evercore", name="Evercore", **kw):
    return Firm.objects.create(slug=slug, name=name, **kw)


def _opp(firm, url, title="Summer Analyst", *, region="us", bucket="internship", **kw):
    return Opportunity.objects.create(
        firm=firm, url=url, title=title, bucket=bucket, status="open",
        region=region, **kw,
    )


@pytest.fixture
def bar(db):
    """A miniature of the live shape: campus roles across two markets, a third
    with no resolvable region at all, and an experienced row the default view
    hides."""
    f = _firm(tracks=["ib"])
    other = _firm(slug="lazard", name="Lazard", tracks=["consulting"])
    _opp(f, "https://x/1", "US Summer Analyst", region="us")
    _opp(f, "https://x/2", "HK Summer Analyst", region="hk")
    _opp(other, "https://x/3", "Spring Week", region="hk", bucket="insight")
    # No resolvable region — the 297-row case, in miniature.
    _opp(other, "https://x/4", "Global Analyst Programme", region="")
    # Experienced: hidden by the default view, counted out loud.
    _opp(f, "https://x/5", "Vice President, Fund Finance", region="us", bucket="other")
    return f


def _get(client, **params):
    return client.get(reverse("opportunities"), params)


def _hx(client, **params):
    return client.get(reverse("opportunities"), params, headers={"HX-Request": "true"})


def _checked_roles(html):
    """Every `role` radio carrying `checked`, as (value, ...). This is the
    serialization question asked literally: a browser submits exactly the
    checked member of a radio group, and nothing at all if there isn't one."""
    return tuple(
        m.group(1)
        for m in re.finditer(
            r'<input[^>]*name="role"[^>]*value="([^"]*)"[^>]*\schecked[^>]*>', html
        )
    )


# ---------------------------------------------------------------------------
# The segmented control, and the mode-reset bug it exists to prevent.
# ---------------------------------------------------------------------------

def test_the_four_campus_segments_render_with_live_counts(client, bar):
    resp = _get(client)
    values = [s["value"] for s in resp.context["role_segments"]]
    assert values == ["", "insight", "internship", "entry_level"]
    counts = {s["value"]: s["count"] for s in resp.context["role_segments"]}
    assert counts == {"": 4, "insight": 1, "internship": 3, "entry_level": 0}
    html = resp.content.decode()
    assert "All Campus (<span id=\"cnt-role-campus\">4</span>)" in html


def test_other_and_all_are_not_drawn_as_sibling_options(client, bar):
    """Scope is not a filter. The two opt-in modes must not sit in the control
    as if they were a fifth and sixth campus bucket — they are reachable by
    deep link and by the subset sentence's link, and nowhere else."""
    resp = _get(client)
    assert resp.context["role_optin_segment"] is None
    assert [s["value"] for s in resp.context["role_segments"]].count("other") == 0
    assert "Other / Experienced" not in resp.content.decode()


def test_exactly_one_segment_is_always_checked(client, bar):
    """The invariant the whole mode-reset fix rests on. Checked for every value
    the vocabulary accepts, plus a value it does not."""
    for role in ("", "insight", "internship", "entry_level", "other", "all", "banana"):
        html = _get(client, role=role).content.decode()
        assert len(_checked_roles(html)) == 1, f"role={role!r}"


@pytest.mark.parametrize("role,label,count", [
    ("all", "Everything", 5),
    ("other", "Other / Experienced", 1),
])
def test_optin_deep_link_renders_the_conditional_fifth_segment(client, bar, role, label, count):
    resp = _get(client, role=role)
    seg = resp.context["role_optin_segment"]
    assert seg is not None and seg["value"] == role and seg["count"] == count
    html = resp.content.decode()
    assert f"{label} (<span id=\"cnt-role-{role}\">{count}</span>)" in html
    # And it is the checked one, so the bar states its own mode.
    assert _checked_roles(html) == (role,)


@pytest.mark.parametrize("role", ["all", "other"])
def test_optin_mode_survives_the_next_filter_change(client, bar, role):
    """THE MODE-RESET REGRESSION. Load an opt-in mode, then change Region the
    way the htmx form does — carrying whatever the form serializes. Without the
    fifth segment the group has no checked member, `role` is absent from that
    request, and the view silently re-scopes to campus."""
    first = _get(client, role=role)
    assert _checked_roles(first.content.decode()) == (role,)

    # The browser would submit the checked radio's value alongside the new
    # region. Assert it is still there after the round trip, in the context AND
    # in the re-rendered control.
    second = _get(client, role=role, region="hk")
    assert second.context["selected"]["role"] == role
    assert _checked_roles(second.content.decode()) == (role,)
    # …and the mode genuinely still applies, rather than merely being echoed.
    assert second.context["role_optin_segment"]["value"] == role


def test_an_unrecognised_role_checks_all_campus(client, bar):
    """`_apply_role_filter` sends unknown values to the campus scope, so the
    control must show campus checked — otherwise the group serializes nothing
    and the mode-reset bug arrives through a different door."""
    resp = _get(client, role="banana")
    assert _checked_roles(resp.content.decode()) == ("",)
    assert resp.context["total"] == 4


# ---------------------------------------------------------------------------
# Region: counts, the unstated option, and the honesty line.
# ---------------------------------------------------------------------------

def test_region_offers_unstated_with_a_live_count(client, bar):
    """The label was "Other / Unstated" until stated-but-untracked locations
    got their own real "Other Markets" region; this option now means only
    what it says."""
    resp = _get(client)
    opts = {o["value"]: (o["label"], o["count"]) for o in resp.context["facets"]["regions"]}
    assert opts[REGION_NONE] == ("Unstated", 1)
    assert opts["hk"][1] == 2 and opts["us"][1] == 1
    # "Any Region" still means everything, unstated included.
    assert opts[""][1] == 4


def test_region_none_filters_to_the_blank_region_rows(client, bar):
    resp = _get(client, region=REGION_NONE)
    assert resp.context["total"] == 1
    titles = {r["title"] for c in resp.context["clusters"] for r in c["roles"]}
    assert titles == {"Global Analyst Programme"}


def test_region_counts_match_what_the_filter_returns(client, bar):
    """Every number in the control is a promise: pick this and you see exactly
    this many. Checked option by option, including the sentinels."""
    for o in _get(client).context["facets"]["regions"]:
        assert _get(client, region=o["value"]).context["total"] == o["count"], o["value"]


def test_a_concrete_region_says_what_it_is_hiding(client, bar):
    resp = _get(client, region="hk")
    assert resp.context["hidden_region"] == 1
    body = resp.content.decode()
    assert "with no tracked region" in body
    # The escape hatch is built from the LIVE querystring, not a bare "?" —
    # it must preserve the other filters while flipping region to `none`.
    qs = resp.context["show_unregioned_qs"]
    follow = client.get(f"{reverse('opportunities')}?{qs}")
    assert follow.context["selected"]["region"] == REGION_NONE
    assert follow.context["total"] == 1


def test_any_region_hides_nothing_and_says_nothing(client, bar):
    resp = _get(client)
    assert resp.context["hidden_region"] == 0
    assert "with no tracked region" not in resp.content.decode()


def test_an_unrecognised_region_is_a_no_op_not_an_empty_page(client, bar):
    """Matching `_apply_role_filter` and `_apply_year_filter`: a hand-typed or
    stale querystring degrades to the unfiltered feed."""
    assert _get(client, region="atlantis").context["total"] == 4


def test_a_selected_region_survives_being_crossed_to_zero(client, bar):
    """Facets are counted against every OTHER filter, so a live selection can
    fall to zero. It must still render as an option — a <select> that dropped
    its own current value would show a selection it does not have, and the
    out-of-band refresh restores state from exactly that option."""
    resp = _get(client, region="us", track="consulting")
    opts = {o["value"]: o["count"] for o in resp.context["facets"]["regions"]}
    assert opts["us"] == 0
    assert 'value="us" selected' in resp.content.decode()


# ---------------------------------------------------------------------------
# Track counts.
# ---------------------------------------------------------------------------

def test_track_offers_live_counts(client, bar):
    opts = {o["value"]: o["count"] for o in _get(client).context["facets"]["tracks"]}
    assert opts == {"": 4, "ib": 2, "consulting": 2}


def test_track_counts_match_what_the_filter_returns(client, bar):
    for o in _get(client).context["facets"]["tracks"]:
        assert _get(client, track=o["value"]).context["total"] == o["count"], o["value"]


# ---------------------------------------------------------------------------
# The out-of-band count refresh — the stale-count fix.
# ---------------------------------------------------------------------------

def test_the_oob_fragment_ships_on_htmx_requests(client, bar):
    """Without this, every count in the bar freezes at page-load values the
    moment the student touches any control."""
    body = _hx(client).content.decode()
    assert 'id="cnt-role-campus" hx-swap-oob="innerHTML"' in body
    for select_id in ("f-year", "f-region", "f-track"):
        assert f'<select id="{select_id}" hx-swap-oob="innerHTML">' in body


def test_the_oob_fragment_is_absent_from_a_full_render(client, bar):
    """On a full page load the counts are already correct in the markup; an
    out-of-band element in the initial document is inert noise, and a duplicate
    id for every counted control."""
    body = _get(client).content.decode()
    assert "hx-swap-oob" not in body
    assert body.count('id="cnt-role-campus"') == 1


def test_the_oob_counts_track_the_live_filters(client, bar):
    """The whole point: the numbers that come back are the filtered ones."""
    body = _hx(client, region="hk").content.decode()
    assert 'id="cnt-role-campus" hx-swap-oob="innerHTML">2<' in body
    assert 'id="cnt-role-insight" hx-swap-oob="innerHTML">1<' in body


def test_the_oob_fragment_never_swaps_the_form_or_an_input(client, bar):
    """The constraint that keeps focus and caret alive while typing in Search.
    Only bare count spans and the three <select> option lists may be addressed;
    a swap that reached the form or a text input would destroy the user's
    focus, selection and caret on every keystroke."""
    body = _hx(client).content.decode()
    oob_tags = re.findall(r"<(\w+)[^>]*hx-swap-oob", body)
    assert set(oob_tags) <= {"span", "select"}, oob_tags
    assert 'name="q"' not in body       # the Search input is never re-sent
    assert "<form" not in body


# ---------------------------------------------------------------------------
# The subset sentence, and where it is read.
# ---------------------------------------------------------------------------

def test_the_subset_sentence_renders_above_the_stat_strip(client, bar):
    """It used to render at the BOTTOM of the results, below every firm card —
    stating the single most important fact about the default view only to
    readers who had already finished."""
    body = _get(client).content.decode()
    line = body.index("experienced role")
    strip = body.index('class="stat-strip"')
    cards = body.index('class="firmcols"')
    assert line < strip < cards
    assert body.count("experienced role") == 1   # said once, never duplicated


def test_the_subset_sentence_states_the_hidden_count_and_links_out(client, bar):
    resp = _get(client)
    assert resp.context["hidden_other"] == 1
    body = resp.content.decode()
    assert "Showing campus roles only" in body
    assert "1 experienced role hidden" in body
    # Plain words, and a plain link: no paywall theatre around a free,
    # one-click escape hatch. Asserted on the sentence itself rather than the
    # whole document, whose stylesheet comments legitimately discuss the rule.
    sentence = body[body.index("Showing campus roles only"):]
    sentence = sentence[:sentence.index("</p>")]
    assert ">Show everything</a>" in sentence
    for word in ("premium", "Premium", "unlock", "Unlock", "Upgrade"):
        assert word not in sentence


def test_an_optin_mode_hides_nothing_so_says_nothing(client, bar):
    assert "experienced role" not in _get(client, role="all").content.decode()


# ---------------------------------------------------------------------------
# Load-bearing strings and live-region wiring.
# ---------------------------------------------------------------------------

def test_the_programme_year_hint_ships_verbatim(client, bar):
    """The sentence that stops the Year control telling ~4,000 roles' worth of
    lies: `cohort` is the intake year printed in the posting, not the year the
    student graduates. It travels inside the mobile disclosure too, so it is
    present at every breakpoint — there is one copy and this is it."""
    body = _get(client).content.decode()
    assert "Programme Year" in body
    assert "Intake year in the posting. Not a graduation year." in body


def test_the_open_roles_figure_is_a_live_region(client, bar):
    """The results swap silently for a screen-reader user, so the headline
    count re-announces itself. One figure, not the whole strip."""
    body = _get(client).content.decode()
    assert body.count('role="status"') == 1
    assert '<span class="ss-item" role="status">' in body


# ---------------------------------------------------------------------------
# Sponsorship (?sponsorship=). The first question an international student
# asks about a US posting. It became answerable only when `enrich_postings`
# started reading the postings' own pages, and the bar had no control for it.
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_sponsorship_filters_to_the_answer_the_posting_gave(client):
    firm = Firm.objects.create(slug="gs", name="Goldman Sachs")
    for title, answer in (("Sponsored Analyst", "yes"),
                          ("Unsponsored Analyst", "no"),
                          ("Silent Analyst", "unknown")):
        Opportunity.objects.create(
            firm=firm, title=title, bucket="internship", status="open",
            sponsorship=answer, url=f"https://gs.com/{answer}")

    body = client.get("/opportunities/?sponsorship=yes").content.decode()
    assert "Sponsored Analyst" in body
    assert "Unsponsored Analyst" not in body
    assert "Silent Analyst" not in body

    body = client.get("/opportunities/?sponsorship=no").content.decode()
    assert "Unsponsored Analyst" in body
    assert "Sponsored Analyst" not in body


@pytest.mark.django_db
def test_not_stated_gathers_both_ways_of_saying_nothing(client):
    """The column defaults to "unknown" and older rows carry "". They are one
    fact stored two ways, so one option must return both — otherwise the
    counts cannot sum to the total and a bucket of rows is unreachable."""
    firm = Firm.objects.create(slug="ms", name="Morgan Stanley")
    Opportunity.objects.create(firm=firm, title="Default Silent", bucket="internship",
                               status="open", sponsorship="unknown",
                               url="https://ms.com/a")
    Opportunity.objects.create(firm=firm, title="Legacy Silent", bucket="internship",
                               status="open", sponsorship="",
                               url="https://ms.com/b")

    body = client.get("/opportunities/?sponsorship=unknown").content.decode()
    assert "Default Silent" in body
    assert "Legacy Silent" in body


@pytest.mark.django_db
def test_the_sponsorship_counts_sum_to_the_whole_set(client):
    """The bar's consistency rule: a counted facet's options partition the
    set. Silence is an option here rather than a fourth invisible state —
    the mistake Region made when a filter deleted 297 unstated rows without
    saying so."""
    firm = Firm.objects.create(slug="jpm", name="J.P. Morgan")
    for i, answer in enumerate(("yes", "no", "unknown", "", "no")):
        Opportunity.objects.create(
            firm=firm, title=f"Role {i}", bucket="internship", status="open",
            sponsorship=answer, url=f"https://jpm.com/{i}")

    resp = client.get("/opportunities/")
    facet = {o["value"]: o["count"] for o in resp.context["facets"]["sponsorship"]}
    assert facet[""] == 5
    assert facet["yes"] + facet["no"] + facet["unknown"] == facet[""]
    assert facet["no"] == 2
    assert facet["unknown"] == 2, "blank and 'unknown' are one bucket"


def test_the_scope_toggle_lives_in_the_page_header(client, bar, django_user_model):
    """Browse/My-Applications is a page-level scope switch — which of two
    views of this page you are on — so it sits beside the title in the slot
    every other page uses for its page action. As a standalone band below
    the header it cost 42px plus a 24px gap for two links, on the page with
    the most chrome before its first result.

    Signed in, because My Applications needs an account: a signed-out
    visitor correctly gets no toggle at all."""
    user = django_user_model.objects.create_user(email="tabs@x.com", password="x")
    client.force_login(user)
    body = _get(client).content.decode()
    head = body[body.index("<header class=\"pagehead\""):]
    head = head[:head.index("</header>")]
    assert "Browse Openings" in head and "My Applications" in head
    assert 'aria-current="page"' in head, "the active view still says so"


@pytest.mark.django_db
def test_the_board_state_binds_the_totals_to_the_cycle(client, bar):
    """The stat strip and the cycle band answer one question between them and
    are the only two bands here that always render. They were two floating
    strips; they are one unit on a hairline now, marking the boundary between
    what you asked for and what the board answers. Both must stay INSIDE the
    htmx swap target so their numbers refresh with the filters."""
    body = _get(client).content.decode()
    assert 'class="board-state"' in body
    results = body[body.index('id="cov-results"'):]
    assert 'class="board-state"' in results, "must refresh with the filters"
    strip = results.index("stat-strip")
    band = results.index("cycband") if "cycband" in results else None
    if band is not None:
        assert strip < band, "totals lead, shape follows"


@pytest.mark.django_db
def test_other_markets_are_a_real_facet_option_distinct_from_unstated(client):
    """A Sydney posting STATED where it is; a silent posting did not. The two
    used to share one "Other / Unstated" bucket. Now "Other Markets" is a
    market (filterable, counted) and "Unstated" means only what it says."""
    firm = Firm.objects.create(slug="mq", name="Macquarie")
    Opportunity.objects.create(firm=firm, title="Graduate Analyst", bucket="internship",
                               status="open", url="https://mq.com/1", location="Sydney",
                               region="other")
    Opportunity.objects.create(firm=firm, title="Mystery Analyst", bucket="internship",
                               status="open", url="https://mq.com/2")

    resp = client.get(reverse("opportunities"), {"region": "other"})
    assert resp.context["total"] == 1
    body = resp.content.decode()
    assert "Graduate Analyst" in body and "Mystery Analyst" not in body

    labels = [o["label"] for o in resp.context["facets"]["regions"]]
    assert "Other Markets" in labels
    assert "Unstated" in labels
    assert "Other / Unstated" not in labels

    unstated = client.get(reverse("opportunities"), {"region": REGION_NONE})
    assert unstated.context["total"] == 1
    assert "Mystery Analyst" in unstated.content.decode()


@pytest.mark.django_db
def test_a_year_stated_only_in_prose_counts_and_filters(client):
    """69 live roles state their year only in the description ("graduating
    student of 2028"), where the facts extractor holds it with its evidence
    phrase. The facet filed them under No Year Stated while the eligibility
    lens issued verdicts from the same fact — two features disagreeing about
    whether the posting spoke. Facet, filter and YEAR_NONE all read the
    prose fact now, so the counts keep their promise."""
    firm = Firm.objects.create(slug="iv", name="Invesco")
    Opportunity.objects.create(
        firm=firm, title="Early Career Intern", bucket="internship",
        status="open", url="https://iv.com/1",
        raw={"facts": {"grad": {"value": "2028", "years": ["2028"],
                                "phrase": "be a graduating student of 2028"}}})
    Opportunity.objects.create(firm=firm, title="Quiet Intern", bucket="internship",
                               status="open", url="https://iv.com/2")

    resp = client.get(reverse("opportunities"))
    years = {o["value"]: o["count"] for o in resp.context["year_facet"]}
    assert years.get("2028") == 1, years
    assert years.get(YEAR_NONE) == 1, "the quiet role is the only unstated one"

    picked = client.get(reverse("opportunities"), {"year": "2028"})
    assert picked.context["total"] == 1
    assert "Early Career Intern" in picked.content.decode()

    none = client.get(reverse("opportunities"), {"year": YEAR_NONE})
    assert none.context["total"] == 1
    assert "Quiet Intern" in none.content.decode()
