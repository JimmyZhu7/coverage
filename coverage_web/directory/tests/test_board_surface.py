"""The Opportunities board's own surface: what its counts promise, what shape
its controls are, what colour its figures are, and what is allowed to move.

Every test here pins a defect measured on the live dev board on 2026-09-01
(the read-only UI audit, `scratchpad/audit-ui.md` §3), and every one of them
asserts the RENDERED artifact — the stylesheet the browser is served and the
HTML it is served with it — because each defect was invisible to every test
in this suite while being plainly visible on the page:

  * two totals disagreed by 127 with nothing explaining the difference,
  * four pressable controls were 999px capsules under a filter bar that had
    just been squared, on a page whose own `.btn` rule states the law,
  * the "6d" on every role closing this week rendered at 3.98:1 (light) and
    3.12:1 (dark) because it was painted with a BAR token,
  * ten infinite pulse rings ran on the first viewport for a state that never
    changes,
  * the dark board's scroll affordance was invisible and its logo tiles glowed,
  * eight rules and two keyframes styled markup no template has emitted since
    the 2026-08-30 row redesign.

Colour is asserted by TOKEN NAME rather than by measuring a ratio in Python:
the tokens' own values live in `static/css/coverage.css` and are another
workstream's to move, and the promise this file can keep is "this figure is
painted with the tier built to be read", not "this hex is 4.5:1". The measured
ratios are in the docstrings so the next reader knows what was checked and how.
"""

from __future__ import annotations

import pathlib
import re

import pytest
from django.test import Client
from django.urls import reverse

from directory.models import Firm, Opportunity

pytestmark = pytest.mark.django_db

_STYLE_RE = re.compile(r"<style>(.*?)</style>", re.S)


def _css(path: str = "/opportunities/") -> str:
    """The page's own <style> blocks as the browser gets them, comments out.

    Comments in this file carry prose with braces and commas in it, which a
    regex over selectors would otherwise sweep into the next rule's prelude.
    """
    html = Client().get(path).content.decode()
    return re.sub(r"/\*.*?\*/", "", "\n".join(_STYLE_RE.findall(html)), flags=re.S)


def _rule(css: str, selector: str) -> str:
    """Every declaration block whose prelude names exactly `selector`, joined.

    EXACT, not substring: `.track-btn` and `.track-btn:hover` are different
    promises and a substring match would conflate them.

    ALL of them, not the first: a component in this file is routinely split
    across two rules (`.track-btn svg` sets its box in one place and its
    transition in another), and a helper that returned the first would assert
    against whichever happened to come earlier in the file — a test that
    passes or fails on source order rather than on what the browser computes.
    """
    found = [body for prelude, body in re.findall(r"([^{}]+)\{([^{}]*)\}", css)
             if any(" ".join(s.split()) == selector for s in prelude.split(","))]
    if not found:
        raise AssertionError(f"no rule for {selector!r} in the rendered CSS")
    return "\n".join(found)


# ---------------------------------------------------------------------------
# 1. TWO TOTALS ON ONE SCREEN.
#
# The segmented control said "All Campus (2723)" and the stat strip eight
# pixels below said "2596 Open Roles". filter-bar-redesign.md §3, principle 2:
# counts are a promise. Both numbers were right and they count different
# things, and nothing on the page said so, which makes at least one of them
# read as wrong.
# ---------------------------------------------------------------------------


@pytest.fixture
def dupes(db):
    """A board with a Class-B duplicate on it: one firm filing one job as two
    requisitions, which is what `fold_duplicates` collapses at DISPLAY time.

    Same title, same location, same deadline, different URLs — SIG posts every
    2027 internship under two iCIMS job numbers, which is the live case
    `directory.dupes` was written for.
    """
    f = Firm.objects.create(slug="sig", name="Susquehanna")
    common = dict(firm=f, title="2027 Quantitative Trading Intern", bucket="internship",
                  status="open", location="Dublin", region="eu")
    Opportunity.objects.create(url="https://x/jobs/1/a/job", **common)
    Opportunity.objects.create(url="https://x/jobs/2/b/job", **common)
    Opportunity.objects.create(url="https://x/jobs/3/c/job",
                               **{**common, "title": "2027 Summer Analyst"})
    return f


def test_the_board_count_and_the_strip_total_reconcile(client, dupes):
    """`board_count - hidden_fit == total`, exactly — and with the fit
    filter off, the two are simply the same number.

    REWRITTEN 2026-09-02, and the premise it used to pin is retired rather
    than weakened. The old identity was `board_count - hidden_dupes -
    hidden_fit == total`, because the segmented control counted board ROWS
    while the strip counted the rows a student is shown, and the page paid
    for that gap with a footnote explaining 137 folded listings. The segment
    counts fold before they count now (`views._folded_count`), so
    `hidden_dupes` is no longer a term: the fold happens on both sides of the
    equation or on neither. That is a strictly stronger promise — three
    numbers reduced to two, and the two agree wherever the student has not
    switched a personal filter on.

    Measured on the founder's live board when this shipped: segment 2,958,
    strip 2,958, 137 rows folded and nothing left to say about them.
    """
    ctx = client.get(reverse("opportunities")).context
    assert ctx["board_count"] - ctx["hidden_fit"] == ctx["total"]
    # And the fixture really does fold something, so this is not holding
    # vacuously on a board with no duplicate on it.
    assert ctx["hidden_dupes"] == 1
    assert ctx["board_count"] == ctx["total"] == 2


def test_the_segment_pill_states_the_number_the_strip_states(client, dupes):
    """The two figures a student can see at once, read off the rendered page.

    The original defect was never about context keys: it was "All Campus
    (2723)" sitting eight pixels above "2596 Open Roles". This asserts the
    fix where the defect was — in the HTML.
    """
    body = client.get(reverse("opportunities")).content.decode()
    pill = re.search(r'id="cnt-role-campus"[^>]*>(\d+)<', body)
    strip = re.search(r'<b class="dash-num" style="--i:0">(\d+)</b>', body)
    assert pill and strip
    assert pill.group(1) == strip.group(1) == "2"


def test_the_fold_is_silent_because_there_is_nothing_left_to_say(client, dupes):
    """No footnote on a board whose only cut is the duplicate fold.

    This is the 2026-08-28 decision restored, not a regression of the
    2026-09-01 one. `opportunities.html` records it: repeat listings fold
    silently, no toggle, no count, no route back, because a firm re-filing
    one job as several requisitions is noise on every reading of this page
    and never a role anyone was trying to reach. The footnote had reopened
    that purely to account for a gap in the counts; with the counts agreeing,
    printing "1 folded as repeat listing" would be telling a student about a
    difference the page no longer has.
    """
    body = client.get(reverse("opportunities")).content.decode()
    # The class ATTRIBUTE, not the bare word: the rule that styles this line
    # ships in the page's own <style> block on every render, footnote or not.
    assert 'class="scope-line scope-foot"' not in body
    assert "folded as repeat listing" not in body


def test_no_footnote_when_the_two_counts_agree(client):
    """A board with nothing folded and nothing hidden says nothing. The line
    is a footnote to a difference; with no difference it is noise, and this
    is the state of most filtered views."""
    f = Firm.objects.create(slug="ms", name="Morgan Stanley")
    Opportunity.objects.create(firm=f, url="https://x/1", title="Summer Analyst",
                               bucket="internship", status="open", region="us")
    body = client.get(reverse("opportunities")).content.decode()
    assert 'class="scope-line scope-foot"' not in body
    ctx = client.get(reverse("opportunities")).context
    assert ctx["board_count"] == ctx["total"] == 1


def test_turning_the_fold_off_moves_both_counts_together(client, dupes):
    """`?dupes=1` unfolds the board, and the segment count unfolds with it.

    The escape hatch is the one place a folded count could quietly become a
    lie in the other direction: the render stops folding, and a pill still
    quoting the folded number would put the same two-totals defect back on
    the page wearing the URL fallback as a disguise. Both sides move.

    It stays unadvertised (the "Show repeat listings" checkbox was cut on
    2026-08-28), so nothing on the page links it.
    """
    ctx = client.get(reverse("opportunities"), {"dupes": "1"}).context
    assert ctx["hidden_dupes"] == 0
    assert ctx["board_count"] == ctx["total"] == 3
    body = client.get(reverse("opportunities"), {"dupes": "1"}).content.decode()
    assert 'class="scope-line scope-foot"' not in body
    assert "dupes=1" not in body, (
        "the fold's URL fallback stays unadvertised; see _results.html")


def test_the_segment_count_does_not_depend_on_who_is_asking(dupes):
    """The fold's tie-break may not move a shared count, and it cannot.

    `fold_duplicates` reads `sticky_ids` only in `_survivor_rank`, which
    decides WHICH copy of a cluster the student sees. The number of survivors
    is the same either way — and that is the whole licence for the segmented
    control to fold before it counts while staying a figure about the board
    rather than about one reader. If this ever stops holding, the pill and
    the strip stop being one number and the footnote has to come back.
    """
    from directory.dupes import fold_duplicates

    rows = list(Opportunity.objects.filter(status="open"))
    plain, folded_plain = fold_duplicates(rows)
    for sticky in ([], [rows[0].id], [r.id for r in rows]):
        kept, folded = fold_duplicates(rows, sticky_ids=sticky)
        assert (len(kept), folded) == (len(plain), folded_plain)


def test_every_segment_states_what_clicking_it_shows(client, dupes):
    """Each pill's count is that segment's own rendered total, not the
    board's row count — checked by clicking through every one of them.

    The pills are cross-filtered facets, so each is folded over its OWN
    scope: `Everything` over the whole board, `All Campus` over the three
    campus buckets together, a single bucket over itself. Summing folded
    buckets to get the wider scopes would be exact only while no duplicate
    cluster straddles a bucket boundary, which is data rather than an
    invariant.
    """
    segments = {s["value"]: s["count"]
                for s in client.get(reverse("opportunities")).context["role_segments"]}
    assert segments  # the control is drawn at all
    for value, count in segments.items():
        params = {"role": value} if value else {}
        assert client.get(reverse("opportunities"), params).context["total"] == count, (
            f"segment {value!r} promises {count} roles")


def test_the_out_of_band_refresh_ships_the_folded_counts(client, dupes):
    """The counts that survive an htmx swap are the folded ones too.

    The filter bar sits outside `#cov-results`, so its numbers are re-sent as
    out-of-band spans (`_filter_counts.html`). That fragment reads
    `role_segments`, the same list the initial render draws from, which is
    what stops the bar drifting back to unfolded numbers after the first
    keystroke — the exact failure mode that fragment exists to prevent.
    """
    body = client.get(reverse("opportunities"),
                      HTTP_HX_REQUEST="true").content.decode()
    oob = re.search(r'<span id="cnt-role-campus" hx-swap-oob="innerHTML">(\d+)</span>',
                    body)
    assert oob, "the campus segment's count must ship in the out-of-band swap"
    assert oob.group(1) == "2"


# ---------------------------------------------------------------------------
# 2. ONE CONTROL SHAPE.
#
# coverage.css's `.btn` rule (L1089-1093) states it: "Status chips keep the
# pill; controls don't." The board was the last page ignoring it — Save, Read,
# "Save them all" and Undo were all 999px under a filter bar squared to
# `--r-ctl` in the same pass, so a student met both shapes on one screen.
# ---------------------------------------------------------------------------

SQUARED = [
    (".track-btn", "Save / Saved — writes a row"),
    # "up to 310 rows" until 2026-09-02; the offer is scored now and capped at
    # `BULK_SAVE_PEEK_MAX`, so the button never commits to more than the peek
    # above it printed. Still a control that writes, still squared.
    (".scope-act", "Save them all — writes up to 8 rows"),
    (".rcd-undo", "Undo — reverses a dismissal"),
]

# Kept as capsules, each for a stated reason.
PILLED = [
    (".track-chip", "read-only funnel status: Applied, Interviewing, Offer"),
]


@pytest.mark.parametrize("selector,why", SQUARED)
def test_a_thing_you_press_is_ten_pixels(selector, why):
    body = _rule(_css(), selector)
    assert "border-radius: var(--r-ctl)" in body, f"{selector} ({why})"
    assert "999px" not in body


@pytest.mark.parametrize("selector,why", PILLED)
def test_a_thing_that_reports_state_keeps_the_pill(selector, why):
    assert "border-radius: 999px" in _rule(_css(), selector), f"{selector} ({why})"


def test_the_read_button_is_squared_for_every_page_that_draws_it():
    """`.meta-read` is defined in `static/css/coverage.css` (L1767) with a
    999px radius, and the override lives in `directory/_drawer.html` rather
    than in the feed's own `_styles.html`.

    That placement is the test's whole point. Three pages draw this button —
    the feed, the firm page and My Applications — and only the first two
    include `_styles.html`. An override there would have squared the button on
    two pages out of three and left the third a pill, which is the same class
    of bug that once left My Applications with fact chips and no way to read
    the posting behind them. `_drawer.html` is the one partial all three
    include.

    So all THREE pages are asked, including the signed-in one — a version of
    this test that checked only the two anonymous pages would have passed on
    the wrong fix.
    """
    from .test_tracking import _user

    Firm.objects.get_or_create(slug="sig", defaults={"name": "Susquehanna"})
    anon = Client()
    signed_in = Client()
    signed_in.force_login(_user())
    for client_, path in ((anon, "/opportunities/"),
                          (anon, "/firms/sig/"),
                          (signed_in, reverse("my_applications"))):
        html = client_.get(path).content.decode()
        css = re.sub(r"/\*.*?\*/", "", "\n".join(_STYLE_RE.findall(html)), flags=re.S)
        assert ".meta-read { border-radius: var(--r-ctl); }" in css, path


# ---------------------------------------------------------------------------
# 3. THE DUE FIGURE'S COLOUR.
# ---------------------------------------------------------------------------


def test_the_closing_soon_figure_uses_the_text_tier_not_the_bar_tier():
    """`.rr-due-n.meta-soon` is the "6d" on every role closing inside a week —
    the one figure on a board of 649 rows that is supposed to shout.

    It was painted `--w-chatted-bar`, which is a FILL colour for the warmth
    meter's 4px band and was never a text value. Measured on the rendered
    page: 3.98:1 in light and 3.12:1 in dark at 12px, both under AA's 4.5.
    `--w-chatted-t` is the same warmth family's text tier: 6.89:1 light and
    6.63:1 dark, measured the same way after the change.
    """
    body = _rule(_css(), ".rr-due-n.meta-soon")
    assert "var(--w-chatted-t)" in body
    assert "--w-chatted-bar" not in body


# ---------------------------------------------------------------------------
# 4 + 11. WHAT IS ALLOWED TO MOVE.
#
# Two rules, both from the audit's motion inventory: an infinite animation is
# earned only by a live state, and a number may not animate through values it
# does not hold.
# ---------------------------------------------------------------------------


def test_the_rolling_dot_does_not_pulse_forever():
    """"Rolling" is a state, not an event. `.rolling-dot::after` ran
    `pulse-ring` on an infinite loop on every rolling role — ten on the first
    viewport, sixty-odd down one scrolled column. coverage.css's own
    `.live-dot` (L2125-2137) already made this argument and removed its own
    ring. The colour carries the signal; nothing needs to move."""
    css = _css()
    assert ".rolling-dot::after" not in css
    assert "pulse-ring" not in css


def test_no_looping_animation_survives_on_the_board():
    """The general form of the rule above, so the next ring cannot be added
    without a state to bind it to.

    `.cols-loading::after` is the one exception and is named rather than
    pattern-matched, so adding a second means editing this list and writing
    down why. It earns the loop: it is the lazy-load sentinel's own spinner,
    on screen only while more columns are actually in flight, which is
    precisely the "bound to a live state" test the rule states.
    """
    css = _css()
    looping = {
        " ".join(prelude.split())
        for prelude, body in re.findall(r"([^{}]+)\{([^{}]*)\}", css)
        if re.search(r"\banimation(?:-name)?:[^;}]*\binfinite\b", body)
    }
    assert looping <= {".cols-loading::after"}, looping


def test_the_strip_figures_do_not_animate_through_false_values():
    """`ss-pop` scaled every figure 0.6 -> 1.08 -> 1 over 620ms on load.

    A number that GROWS reads as a number resolving, so for half a second
    "2596 Open Roles" was a smaller figure at a smaller size and a reader
    could not tell a render from a count. Nothing had changed; the page had
    merely loaded. The settle is opacity and colour now — the figure is at
    its final size and its final value from the first frame it is legible.
    """
    css = _css()
    assert "ss-pop" not in css
    for _name, body in re.findall(r"@keyframes\s+(ss-settle\w*)\s*\{(.*?)\}\s*\n", css, re.S):
        assert "scale" not in body
        assert "transform" not in body


def test_the_two_writes_on_the_board_are_seen():
    """Save and "Not for me" are the only acts a student performs on this
    page, and both were silent: the star recoloured, the row swapped for its
    receipt with no transition. Motion belongs on a change, and these are the
    only two changes there are.

    The save pop hangs off `htmx-settling` rather than naming a keyframe
    directly, and that is load-bearing: a keyframe on `.track-btn.is-saved
    svg` fires on every PAGE LOAD too, once per already-saved row, which is
    exactly the defect the audit flagged on Settings (chip-check on 23 chips
    that merely loaded). `htmx-settling` exists only on a swap.
    """
    css = _css()
    # Save: a transition armed by the swap, not an animation armed by a render.
    assert "transform" in _rule(css, ".track-btn svg")
    assert "scale" in _rule(css, ".track.htmx-settling .track-btn.is-saved svg")
    assert "animation" not in _rule(css, ".track-btn svg")
    # Dismiss: the departing row fades, the arriving stub settles.
    assert "opacity: 0" in _rule(css, ".rolerow.htmx-swapping")
    assert "settle" in _rule(css, ".rolerow-dismissed")


def test_every_new_motion_is_switched_off_by_reduced_motion():
    """`test_a11y.py` covers looping animations; these are transitions and
    one-shots, which that guard does not reach. Each of tonight's three is
    named in a reduced-motion block in this file."""
    css = _css()
    blocks = []
    for m in re.finditer(r"@media\s*\(\s*prefers-reduced-motion:\s*reduce\s*\)\s*\{", css):
        depth, i = 1, m.end()
        while depth and i < len(css):
            depth += (css[i] == "{") - (css[i] == "}")
            i += 1
        blocks.append(css[m.end():i - 1])
    guarded = " ".join(blocks)
    for selector in (".track-btn svg", ".rolerow.htmx-swapping", ".rolerow-dismissed",
                     ".stat-strip b.dash-num"):
        assert selector in guarded, f"{selector} has no reduced-motion escape"


# ---------------------------------------------------------------------------
# 5 + 6. THE DARK BOARD.
# ---------------------------------------------------------------------------


def test_the_scroll_affordance_survives_the_dark_theme():
    """`.firmcol-scroll`'s two shadow layers were `rgba(23, 23, 23, 0.10)` —
    the light theme's ink, hardcoded. On `#1c201a` a 10% near-black is
    invisible, so in dark the card straddling the bottom edge read as a
    clipped box, which is the exact symptom that got this affordance reported
    as a layout bug in the first place. Derived from `--ink` it is a shadow on
    paper and a glow on ink."""
    body = _rule(_css(), ".firmcol-scroll")
    assert "color-mix(in srgb, var(--ink) 10%, transparent)" in body
    assert "rgba(23, 23, 23" not in body


def test_the_tiles_are_restated_for_dark_in_both_directions():
    """The monogram tile was a fixed `hsl(--hue 52% 90%)` and the logo tile a
    flat `#fff`: thirteen light squares across one row of a dark board.

    BOTH blocks, because dark arrives two ways. The media query sets the
    default and `[data-theme]` overrides it in either direction — a student
    reading at midnight in a light-mode OS is a real person, not a
    configuration error, and this is the shape coverage.css's own palette
    uses. A dark rule written only inside the media query is a rule the
    theme toggle cannot reach.

    The star tile is excluded by name. `.firmcol-logo--picked` is drawn on
    `--accent`, which is already theme-aware, and a bare
    `[data-theme="dark"] .firmcol-logo` outranks the two-class rule that pins
    it — the same specificity accident that once left the star rendering on a
    monogram's pastel and made it no signal at all.
    """
    css = _css()
    for prefix in (':root:not([data-theme="light"])', ':root[data-theme="dark"]'):
        mono = f"{prefix} .firmcol-logo:not(.has-logo):not(.firmcol-logo--picked)"
        assert mono in css, mono
        assert "color-mix(in srgb, var(--surface)" in _rule(css, mono)
        logo = f"{prefix} .firmcol-logo.has-logo"
        assert "color-mix(in srgb, var(--surface) 15%, white)" in _rule(css, logo)
    assert "@media (prefers-color-scheme: dark)" in css


# ---------------------------------------------------------------------------
# 8 + 10. ONE ROW SHAPE PER PAGE, AND NO RULES FOR MARKUP NOBODY EMITS.
# ---------------------------------------------------------------------------


@pytest.fixture
def firm_page(db):
    f = Firm.objects.create(slug="td", name="TD Securities")
    for i in range(20):
        Opportunity.objects.create(
            firm=f, url=f"https://td/{i}", title=f"2027 Summer Analyst {i}",
            bucket="internship", status="open", region="us")
    return f


def test_the_firm_pages_rows_are_the_ledger_form(firm_page):
    """`.frow` was a bordered, rounded, shadowed box at `--r-ctl` — the one
    place on the site where the token whose own comment reads "buttons,
    inputs" was spent on a ROW, and a fifth panel shape on a page whose other
    surfaces are 12px panels or hairline-only. Twenty-six of them stacked read
    as twenty-six cards about one firm.

    `.cyc-obs-row` folded into the same form: it was the site's only
    bordered-and-rounded list row, two occurrences, one page. Its own comment
    wanted to be distinguishable from `.tl-row`'s confirmed/rumored border
    language, and it still is — better, because `.tl-row` is now the only
    bordered row on the page.
    """
    css = _css("/firms/td/")
    for selector in (".frow", ".cyc-obs-row"):
        body = _rule(css, selector)
        assert "border-top: 1px solid var(--line)" in body, selector
        assert "border-radius: 0" in body, selector
        # No panel shadow. `.frow` had `var(--shadow-1)` and had to say
        # `none`; `.cyc-obs-row` never carried one, so the promise is the
        # absence rather than the literal.
        assert "var(--shadow" not in body, selector
        assert "--r-ctl" not in body and "--r-panel" not in body, selector
        assert f"{selector}:first-child" in css, selector


def test_the_retired_fuse_bar_leaves_no_rules_behind(firm_page):
    """The fuse was the depleting countdown bar the 2026-08-30 row redesign
    replaced. Eight rules and two keyframes outlived the markup: no template
    has emitted `.fuse-fill` since, and `core/home.html`'s `.v-fuse-fill` is
    a different rule in a different file for the landing mock.

    Deleting it also removes the last hardcoded light-mode colour in this
    stylesheet (`#c1652f`, the light `--w-chatted-bar`), which would have
    painted a light-mode bar on a dark board had anything drawn it.
    """
    for path in ("/opportunities/", "/firms/td/"):
        css = _css(path)
        for dead in ("fuse-fill", "fuse-burn", "fuse-today", "fuse-soon",
                     "fuse-upcoming", "fuse-passed", "pulse-red"):
            assert dead not in css, f"{dead} still styles on {path}"


def test_a_capped_group_says_how_many_it_left_out(firm_page):
    """The cap is 12 rows per KIND group (`ROLE_ROWS_PER_GROUP`), and a
    capped group hands the remainder to the feed rather than dropping it.

    Pinned here because the row restyle above changes what a long group looks
    like and not how long it is: an uncapped group is how one firm's page
    became 74 screens of scroll, and a page that just got quieter is a page
    where a regression here would be harder to notice, not easier.
    """
    html = Client().get("/firms/td/").content.decode()
    assert html.count('<article class="frow">') == 12
    assert "Show the other 8 in Opportunities" in html


# ---------------------------------------------------------------------------
# 7. THE DRAWER'S BODY.
# ---------------------------------------------------------------------------


def _drawer(text: str) -> str:
    """The drawer fragment for one posting whose text `enrich_postings` has
    fetched. `raw["detail_text"]` is the field the view reads (see
    `role_description`); `facts.paragraphs()` turns it into `blocks`."""
    f, _ = Firm.objects.get_or_create(slug="gs", defaults={"name": "Goldman Sachs"})
    o = Opportunity.objects.create(
        firm=f, url=f"https://gs/{len(text)}", title="Summer Analyst",
        bucket="internship", status="open", region="us",
        raw={"detail_text": text})
    return Client().get(reverse("role_description", args=[o.pk])).content.decode()


def test_a_long_posting_is_folded_and_a_short_one_is_not():
    """The median fetched posting is one unbroken block: `facts.paragraphs()`
    splits on the posting's own section headings and most scrapes have none.
    Measured on the founder's first row, the drawer opened onto 2,741
    characters in two blocks — 651px of solid text with the provenance note
    and the apply link pushed under it. A student opens this to DECIDE, and
    the deciding happens in the first screen.

    The threshold reads the WHOLE description, not one block, so three short
    paragraphs adding up to 400 characters get no fold and one 3,800-character
    paragraph does.

    Nothing is cut: the fold is a CSS clamp (see `_drawer.html`), so the full
    text stays in the DOM for find-in-page and for a screen reader, and where
    `:has()` is missing the drawer renders exactly as it did before.
    """
    long_body = "The programme runs ten weeks. " * 40      # ~1,200 chars
    html = _drawer(long_body)
    assert 'class="drawer-fold"' in html
    assert 'class="drawer-prose"' in html
    # The whole text is still there. The fold is a clamp, not a cut.
    assert html.count("The programme runs ten weeks.") == 40


def test_a_short_posting_gets_no_fold():
    short = "We are hiring a summer analyst in New York. Apply by October."
    html = _drawer(short)
    assert 'class="drawer-fold"' not in html
    assert 'class="drawer-prose"' not in html
    assert short.split(".")[0] in html


# ---------------------------------------------------------------------------
# The two remaining audit enhancements, both of which turned out to be one
# line of markup and are pinned so they stay.
# ---------------------------------------------------------------------------


def test_the_picked_column_says_it_is_not_a_firm_without_repeating_its_name():
    """REWRITTEN 2026-09-02. This used to assert the `.fc-eyebrow` rule — a
    mono, letterspaced, accent word reading "PICKED". It is gone.

    The word was added when three signals were supposed to distinguish this
    column and only one of them worked: on a board where every firm is one of
    the student's own targets, the firm columns wear the same `--accent-line`
    border, so the star tile was carrying it alone — and the tile was ALSO
    broken at the time, drawing a default monogram chip through a one-class
    cascade bug (see `test_the_star_tile_actually_wins_the_cascade` in
    test_firmcol_head.py, which fixed it by specificity in the same pass).

    With the tile actually rendering on `--accent` and the heading on
    `--accent-ink`, the eyebrow sat directly beneath a heading reading
    "Picked for you" and told a reader nothing the heading had not. A label
    that repeats the heading above it is the founder's own example of copy
    that should not exist.

    Two signals no firm column can wear are what carry it now, and they are
    asserted here rather than merely in the file that removed the word."""
    css = _css()
    assert ".fc-eyebrow" not in css, (
        "the 'PICKED' eyebrow is back; it repeats the heading directly above it")
    tile = _rule(css, ".firmcol-logo.firmcol-logo--picked")
    assert "background: var(--accent)" in tile, (
        "the accent-filled tile is the signal no firm column can wear — a "
        "firm tile is a white logo plate or a pastel monogram")
    assert "var(--accent-ink)" in _rule(css, ".firmcol--picked .firmcol-name"), (
        "and the accent heading is the second; a firm name is --ink")


def test_the_picked_columns_header_spends_the_same_two_rows_a_firms_does():
    """REWRITTEN 2026-09-02. Its premise was the eyebrow's POSITION: the word
    had to ride `.firmcol-stats` rather than take a line of its own above the
    name, because a line of its own measured 141px of header against every
    firm column's 126 and dropped this column's first role row 15px below the
    row it belongs to.

    There is no eyebrow to place any more. The invariant it was protecting —
    this header must not grow a row its neighbours do not have — is now
    structural, and stronger: the head is a two-row grid, the name is row one
    and `.firmcol-stats` is row two, in both columns.

    Asserted against the TEMPLATES, not a rendered page, for the reason the
    original gave: the Picked column only draws for a student whose profile
    scores picks, so a rendered assertion would silently skip on most
    fixtures, and this test exists precisely because the defect it guards was
    invisible until measured.
    """
    here = pathlib.Path(__file__).resolve().parents[2] / "templates" / "directory"
    picked = (here / "_results.html").read_text()
    assert "fc-eyebrow" not in picked
    heading = picked.index('id="pickcol-h"')
    stats = picked.index('<div class="firmcol-stats">')
    meta = picked.index('class="firmcol-meta"')
    assert heading < stats < meta, (
        "the Picked column's count line moved out of the stats row; that is a "
        "third row in a header whose neighbours have two")

    firms = (here / "_columns.html").read_text()
    fstats = firms.index('<div class="firmcol-stats">')
    fmeta = firms.index('class="firmcol-meta"')
    ftier = firms.index('class="firmcol-tier')
    assert fstats < fmeta < ftier, (
        "a firm column's category, open count and tier belong on ONE line "
        "inside .firmcol-stats — stacked, they are the three-row block beside "
        "a square tile that the founder's review called cluttered")
    assert firms.index('class="firmcol-h"') < fstats


def test_the_mobile_filter_disclosure_carries_its_active_count(client, db):
    """filter-bar-redesign.md §F: the 375px summary reads "Filters · n".

    The count comes from `filters_more_active`, computed server-side and also
    frozen into the script that decides whether the disclosure may close — so
    a summary that stopped naming it would mean a deep-linked filter could be
    both invisible and unannounced.
    """
    f = Firm.objects.create(slug="ms", name="Morgan Stanley", tracks=["ib"])
    Opportunity.objects.create(firm=f, url="https://x/1", title="Summer Analyst",
                               bucket="internship", status="open", region="us")
    plain = client.get(reverse("opportunities")).content.decode()
    assert ">Filters</summary>" in plain

    two = client.get(reverse("opportunities"), {"region": "us", "track": "ib"})
    assert two.context["filters_more_active"] == 2
    assert "Filters · 2</summary>" in two.content.decode()
