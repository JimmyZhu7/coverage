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
    # "all" ("Everything") is a fifth, always-drawn segment now (2026-08-27):
    # it used to be a deep-link-only opt-in, reachable only through the
    # header's subset sentence; that sentence is gone and its escape hatch
    # had to stay one click away, so it graduated to a normal segment here.
    assert values == ["", "insight", "internship", "entry_level", "all"]
    counts = {s["value"]: s["count"] for s in resp.context["role_segments"]}
    assert counts == {"": 4, "insight": 1, "internship": 3, "entry_level": 0, "all": 5}
    html = resp.content.decode()
    assert "All Campus (<span id=\"cnt-role-campus\">4</span>)" in html
    assert "Everything (<span id=\"cnt-role-all\">5</span>)" in html


def test_other_is_not_drawn_as_a_sibling_option(client, bar):
    """Scope is not a filter. `other` (experienced rows outside the four
    campus buckets) must not sit in the control as if it were a fifth bucket
    — it is reachable only by deep link. `all` ("Everything") is the one
    exception: it is a real, always-checkable mode now (see the test above),
    not a filter over campus roles, so it belongs beside them."""
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


def test_everything_is_a_normal_checkable_segment_not_a_deep_link_echo(client, bar):
    """`all` used to only ever render CHECKED, as the conditional fifth
    segment acknowledging a deep link — never a sibling a student could
    simply click. It is a normal segment now: unchecked by default, and
    checked like any of the other four once selected."""
    default = _get(client)
    everything = next(s for s in default.context["role_segments"] if s["value"] == "all")
    assert everything["checked"] is False
    assert 'id="seg-role-all" value="all"' in default.content.decode()
    assert 'id="seg-role-all" value="all" checked' not in default.content.decode()
    assert default.context["role_optin_segment"] is None

    picked = _get(client, role="all")
    everything = next(s for s in picked.context["role_segments"] if s["value"] == "all")
    assert everything["checked"] is True
    assert _checked_roles(picked.content.decode()) == ("all",)
    # No conditional fifth segment fires for "all" any more — the normal
    # segment above is the whole story, and firing both would check two
    # radios sharing one value.
    assert picked.context["role_optin_segment"] is None


def test_optin_deep_link_renders_the_conditional_fifth_segment(client, bar):
    """`other` alone still uses this mechanism — `all` graduated to a normal
    segment (see the tests above)."""
    resp = _get(client, role="other")
    seg = resp.context["role_optin_segment"]
    assert seg is not None and seg["value"] == "other" and seg["count"] == 1
    html = resp.content.decode()
    assert 'Other / Experienced (<span id="cnt-role-other">1</span>)' in html
    # And it is the checked one, so the bar states its own mode.
    assert _checked_roles(html) == ("other",)


@pytest.mark.parametrize("role", ["all", "other"])
def test_optin_mode_survives_the_next_filter_change(client, bar, role):
    """THE MODE-RESET REGRESSION. Load an opt-in mode, then change Region the
    way the htmx form does — carrying whatever the form serializes. Without a
    checked member the group has no checked member, `role` is absent from
    that request, and the view silently re-scopes to campus.

    `all` and `other` now keep that checked member two different ways —
    `all` as a normal segment, `other` as the conditional fifth — so both are
    asked here, reading the checked state from wherever each one actually
    lives."""
    def _is_checked(ctx, role):
        if role == "other":
            seg = ctx["role_optin_segment"]
            return seg is not None and seg["value"] == role
        return any(s["value"] == role and s["checked"] for s in ctx["role_segments"])

    first = _get(client, role=role)
    assert _checked_roles(first.content.decode()) == (role,)
    assert _is_checked(first.context, role)

    # The browser would submit the checked radio's value alongside the new
    # region. Assert it is still there after the round trip, in the context AND
    # in the re-rendered control.
    second = _get(client, role=role, region="hk")
    assert second.context["selected"]["role"] == role
    assert _checked_roles(second.content.decode()) == (role,)
    # …and the mode genuinely still applies, rather than merely being echoed.
    assert _is_checked(second.context, role)


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
    """The header used to state this in its own sentence ("N with no tracked
    region"). That sentence is gone (2026-08-27, "take this thing away") and
    needed no relocation: the Region select's own "Unstated (N)" option
    already carries the same live count, one click away, regardless of which
    concrete region is currently picked."""
    resp = _get(client, region="hk")
    assert resp.context["hidden_region"] == 1
    unstated = next(o for o in resp.context["facets"]["regions"] if o["value"] == REGION_NONE)
    assert unstated["count"] == 1
    assert 'value="none">Unstated (1)</option>' in resp.content.decode()
    # The escape hatch is built from the LIVE querystring, not a bare "?" —
    # it must preserve the other filters while flipping region to `none`.
    qs = resp.context["show_unregioned_qs"]
    follow = client.get(f"{reverse('opportunities')}?{qs}")
    assert follow.context["selected"]["region"] == REGION_NONE
    assert follow.context["total"] == 1


def test_any_region_hides_nothing(client, bar):
    resp = _get(client)
    assert resp.context["hidden_region"] == 0


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

def test_the_subset_sentence_is_gone_from_the_header(client, bar):
    """It used to render at the TOP of the results, above the stat strip and
    every firm card — one line naming the campus-only scope, its hidden
    count, and a "Show everything" link. Removed outright (2026-08-27,
    "take this thing away"): the guarantee it made moved to the Role Type
    control's own "Everything" segment (see the tests above), which states
    its own live count and is a real, always-checkable mode rather than a
    sentence with a link buried in it."""
    body = _get(client).content.decode()
    assert "Showing campus roles only" not in body
    assert "experienced role" not in body
    assert ">Show everything</a>" not in body
    # The escape hatch is one click away regardless — it just lives in the
    # segmented control now, above the stat strip like the sentence used to.
    strip = body.index('class="stat-strip"')
    everything = body.index('id="seg-role-all"')
    cards = body.index('class="firmcols"')
    assert everything < strip < cards


def test_the_everything_segment_states_its_own_count_honestly(client, bar):
    """The wording moved, the honesty did not: the total behind "Everything"
    genuinely includes the row the campus scope hides, with no paywall
    theatre around a free, one-click mode. Checked against the segmented
    control's own markup, not the whole document — whose stylesheet comments
    legitimately discuss the no-paywall rule in prose."""
    resp = _get(client)
    everything = next(s for s in resp.context["role_segments"] if s["value"] == "all")
    assert everything["count"] == 5   # 4 campus rows + the 1 experienced row
    html = resp.content.decode()
    seg_list = html[html.index('class="seg-list"'):html.index("</fieldset>")]
    for word in ("premium", "Premium", "unlock", "Unlock", "Upgrade"):
        assert word not in seg_list


# ---------------------------------------------------------------------------
# ROW 2 SAYS WHAT IT IS DOING (2026-09-03).
#
# Six controls were drawn at one weight, and four of them read "Any X" whether
# or not the student had chosen anything — so a bar filtered to Hong Kong
# looked exactly like a bar filtered to nothing. `is-set` is the class that
# separates them, and it is rendered SERVER-side (not derived in script) for
# the same three reasons the mobile disclosure's `open` attribute is: first
# paint, no-JS, and surviving the htmx swap.
# ---------------------------------------------------------------------------

_SET_CASES = [
    ("q", "analyst", "Search"),
    ("year", "2027", "Programme Year"),
    ("region", "hk", "Region"),
    ("track", "ib", "Track"),
    ("sponsorship", "yes", "Sponsorship"),
    ("firm", "evercore", "Companies"),
]


def _bar(html):
    """The filter form's own markup.

    Scoped, and that is not fussiness. `_styles.html` renders into a <style>
    block on this page and its comments discuss `is-set` in prose, so a
    document-wide search for the class matches the page's own documentation
    of it and no assertion about the bar can ever fail.
    """
    return html[html.index('<form class="filters"'):html.index("</form>")]


def _label_classes(html):
    """Every filter <label>'s class attribute, keyed by the caption inside it.

    Anchored on the caption rather than on position, because the row is
    reordered whenever a control's priority changes and a positional test
    would then pin last week's layout. Search is keyed "Search" like the rest:
    its `.f-cap` is the caption a screen reader reads, so it is the one name
    the control has in both trees.
    """
    return {m.group(2): m.group(1) for m in re.finditer(
        r'<label([^>]*)>\s*<span class="f-cap">([^<]*)</span>', _bar(html))}


@pytest.mark.parametrize("param,value,caption", _SET_CASES)
def test_an_engaged_filter_says_so_on_the_control_that_caused_it(
        client, bar, param, value, caption):
    """A control the student narrowed carries `is-set`; the five beside it
    do not. Both halves matter: a class that is always on is not a signal."""
    html = _get(client, **{param: value}).content.decode()
    labels = _label_classes(html)
    assert "is-set" in labels[caption], (
        f"{caption} is narrowed to {value!r} and does not say so")
    for other, cls in labels.items():
        if other != caption:
            assert "is-set" not in cls, (
                f"{other} is untouched but drawn as engaged")


def test_a_bar_with_nothing_chosen_lights_nothing(client, bar):
    """The default view. Every control is at "Any X", so nothing is engaged
    and nothing claims to be — which is what makes the lit state readable at
    all."""
    assert "is-set" not in _bar(_get(client).content.decode())


def test_the_engaged_state_is_rendered_not_scripted(client, bar):
    """It ships in the HTML of the FIRST response, so a deep-linked filter is
    lit before any script runs and stays lit with JS off. The bar's other
    server-rendered state (`filters_more_active`, the checked radio) is
    server-side for the same reason, and this is measured the same way: read
    off the response body, with no JS in the loop."""
    assert 'class="is-set"' in _bar(_get(client, region="hk").content.decode())


def test_the_engaged_state_has_a_script_because_the_swap_cannot_reach_it(
        client, bar):
    """AND WHY THE SERVER RENDER IS NOT ENOUGH ON ITS OWN.

    The bar sits OUTSIDE `#cov-results`. An htmx filter change swaps the
    results and refreshes the facet counts out of band; it never re-renders
    the <label> that carries `is-set`. So the first render is correct and
    every render after it is stale unless something client-side keeps up —
    the stale-count bug (defect 2 in this file's header) in a different
    currency, on the one control whose job is to say what it is doing.

    Measured the way that defect was: the htmx response is the swap target
    and the out-of-band spans, and the class is in NEITHER. That is the
    licence for the sync in `opportunities.html`, and this test is what stops
    someone deleting it as redundant with the server render.
    """
    swap = _hx(client, region="hk").content.decode()
    assert "is-set" not in swap, (
        "if the swap ever does carry the class, delete the script instead of "
        "running two mechanisms for one fact")
    assert 'id="cnt-role-campus"' in swap, "the OOB fragment is what does ship"
    # And the sync really is wired to the swap, not only to user input: a
    # `change` listener alone misses the settle after a Search keystroke has
    # re-rendered the facets.
    page = _get(client).content.decode()
    assert 'classList.toggle("is-set"' in page
    assert page.count('htmx:afterSettle') >= 2, (
        "the re-settle handler and the is-set sync both hang off it")
    # And it reads the FILTER's value, not any input that happens to sit in
    # the label. Programme Year has 13 options against base.html's
    # SEARCH_THRESHOLD of 12, so `.csel` builds a type-to-filter box inside
    # that label; counting it would light Year up for typing "2027" into a
    # dropdown the student then closed without choosing anything.
    assert 'closest(".csel-panel")' in page, (
        "the enhancer's own filter box is chrome, not a filter value")


def test_search_leads_the_band_and_is_the_only_control_carrying_a_glyph(
        client, bar):
    """Search is row 2's loudest control: it is the one used on every visit,
    the only one that takes typing, and it is FIRST in the DOM so the visual
    order, the focus order and the serialized order agree.

    The magnifier is inline SVG in the markup rather than a background-image
    data URI, so it follows `currentColor` into dark mode and into the
    engaged state — a data URI has no access to the cascade and would have to
    hardcode a light-mode ink. Five controls carry a caret; exactly one
    carries this."""
    bar_html = _bar(_get(client).content.decode())
    assert bar_html.count('class="filters-search-icon"') == 1
    assert 'stroke="currentColor"' in bar_html
    assert bar_html.index("filters-search") < bar_html.index('name="year"')


# ---------------------------------------------------------------------------
# Load-bearing strings and live-region wiring.
# ---------------------------------------------------------------------------

def test_the_result_count_announces_from_outside_the_swap_target(client, bar):
    """REWRITTEN 2026-09-01. This test used to assert `role="status"` on the
    headline figure inside `.stat-strip`, and that assertion pinned a live
    region that could not fire.

    `#cov-results` is swapped with `innerHTML`, so the strip — and with it
    the node carrying the role — is DESTROYED and rebuilt on every filter
    change. A live region announces changes to its own contents; one that is
    replaced rather than updated announces nothing. The old test passed on
    the markup and told us nothing about the behaviour: a sighted user
    watched 2,596 become 431 while a screen-reader user heard silence.

    The region is now a stable element OUTSIDE the swap target, filled out
    of band by `_filter_counts.html` on every htmx response, alongside the
    facet counts it already refreshes. So there are three claims, and the
    first two are what the old test could not make:

      1. the region survives the swap (it is not in the swapped fragment),
      2. every swap carries an out-of-band update for it,
      3. there is exactly ONE live region for the count, because two would
         read the same number back twice.
    """
    full = _get(client).content.decode()
    # 1. Present on the page, outside the results div, and polite.
    assert 'id="cov-live"' in full
    assert 'aria-live="polite"' in full
    before, _, after = full.partition('<div id="cov-results">')
    assert 'id="cov-live"' in before, (
        "the live region must sit outside #cov-results; inside it, every swap "
        "replaces the region instead of updating it and nothing is announced")
    assert 'id="cov-live"' not in after

    # 2. Every htmx response updates it out of band with the live figure.
    swap = _hx(client).content.decode()
    assert 'id="cov-live" hx-swap-oob="innerHTML"' in swap
    assert "4 open roles" in swap   # the fixture's four campus rows

    # 3. One region for this fact, not two. The strip's own figure no longer
    #    claims a role: two live regions holding one count is how a screen
    #    reader ends up reading it back twice. (`base.html`'s message strip is
    #    a separate region for a separate fact and is not counted here.)
    assert full.count('id="cov-live"') == 1
    assert "role=\"status\"" not in full[full.index('class="stat-strip"'):]


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


# --------------------------------------------------------------------------- #
# The providers list is a VOCABULARY, so it has one entry per provider
# (2026-09-01)
#
# `open_qs.values_list("source").distinct()` looks like a vocabulary query and
# was not one. `Opportunity.Meta.ordering` is `["-first_seen"]`, and Django
# adds every ordering column to the SELECT list of a `.distinct()`, so it
# compiled to `SELECT DISTINCT source, first_seen ... ORDER BY first_seen
# DESC` — distinct over PAIRS, which for a per-row timestamp means distinct
# over rows. On the live board that returned all 16,029 open rows in 37 ms
# instead of the 18 providers in 5 ms, and `sorted()` does not dedupe, so the
# list the template received held every duplicate.
# --------------------------------------------------------------------------- #

@pytest.mark.django_db
def test_the_providers_list_holds_each_provider_once(client):
    """Four rows, two providers, two entries. The row count is the assertion:
    the old query returned one entry per ROW, so this reads 4 without the
    `.order_by()`."""
    firm = _firm(slug="prov", name="Provider Co")
    for i in range(3):
        _opp(firm, f"https://prov.com/gh{i}", source="greenhouse")
    _opp(firm, "https://prov.com/wd", source="workday")

    providers = client.get(reverse("opportunities")).context["facets"]["providers"]
    assert providers == ["greenhouse", "workday"]


@pytest.mark.django_db
def test_the_providers_query_does_not_carry_the_default_ordering(client):
    """The mechanism, pinned where it is visible.

    A row-count assertion alone would go green again the moment someone
    "restored" the ordering on a fixture small enough that every row has a
    distinct provider. What must hold is that the query asks for sources and
    nothing else — no `first_seen` riding along in the DISTINCT."""
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    firm = _firm(slug="prov-sql", name="Provider SQL")
    _opp(firm, "https://prov-sql.com/1", source="greenhouse")

    with CaptureQueriesContext(connection) as captured:
        client.get(reverse("opportunities"))
    distinct = [q["sql"] for q in captured.captured_queries
                if "SELECT DISTINCT" in q["sql"] and '"source"' in q["sql"]]
    assert distinct, "the providers facet no longer runs a DISTINCT on source"
    for sql in distinct:
        assert "first_seen" not in sql, sql


@pytest.mark.django_db
def test_a_provider_with_no_open_rows_is_not_in_the_vocabulary(client):
    """The list describes the OPEN board, which is what `?provider=` filters.
    A closed-only provider offering a filter that returns nothing would be a
    control that lies."""
    firm = _firm(slug="prov-closed", name="Provider Closed")
    _opp(firm, "https://prov-closed.com/1", source="greenhouse")
    Opportunity.objects.create(
        firm=firm, title="Gone", bucket="internship", status="closed",
        url="https://prov-closed.com/2", source="lever", region="us",
    )

    providers = client.get(reverse("opportunities")).context["facets"]["providers"]
    assert providers == ["greenhouse"]
