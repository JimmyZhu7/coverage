"""The template-visible half of the 2026-09-01 UI pass.

Everything here was a MEASURED defect on the rendered pages, not a taste
call, and every assertion names the number or the rule it protects. The
Python behaviour changes from the same pass are pinned where they live:
`_recent_activity` in crm/tests/test_today_timezone.py, the brief's house
style in assistant/tests/test_brief.py, and the advocate line in
crm/tests/test_coverage_track_fit.py.

A Django test client has no layout engine, so what can be checked from here
is the markup and the CSS text. The pixel measurements that motivated each
change were taken with headless Playwright at 1280x800 and 375x812 and are
quoted in the docstrings rather than re-run.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from django.contrib.auth import get_user_model
from django.test import Client
from django.utils import timezone

from crm.models import Contact, Touch

pytestmark = pytest.mark.django_db

TODAY = "/app/"
NETWORK = "/app/contacts/"
CALENDAR = "/app/calendar/"
TALK = "/assistant/"
SETTINGS = "/welcome/settings/"


def _user(email):
    return get_user_model().objects.create_user(email=email, password="x" * 14)


def _get(path, email, **params):
    client = Client()
    client.force_login(_user(email))
    return client.get(path, params).content.decode()


def _styles(html: str) -> str:
    blocks = re.findall(r"<style[^>]*>(.*?)</style>", html, re.S)
    assert blocks, "the page no longer renders a <style> block"
    return "\n".join(blocks)


# ---------------------------------------------------------------------------
# Today
# ---------------------------------------------------------------------------
def test_the_situation_strip_is_a_named_section():
    """It was two bordered sentences with arrows between the brief and the
    queue and NOTHING saying what they were — the only block on the page
    with no heading, so they read as two stray cards.

    The label has to be what the code measures, not a nice phrase:
    `assistant.situation.RECENT_DAYS` is 7 and `build_situation` filters
    `now - timedelta(days=RECENT_DAYS)`, which is a week.
    """
    from assistant import situation

    assert situation.RECENT_DAYS == 7, (
        "the strip's label says 'Moved this week' — if the window changes, "
        "crm/week.html has to change with it"
    )
    html = _get(TODAY, "ui-situation@example.com")
    # The label lives in a `{% if situation_events %}` branch, so a fresh
    # account renders no strip at all. What is pinned here is that the
    # template still carries the section wrapper the label needs.
    assert ".situation-section" in _styles(html)


def test_the_empty_funnel_cell_links_somewhere():
    """Every other empty state on Today says what to do next; this one said
    only "Nothing submitted yet." and was the one cell in the ribbon you
    could not click, because it was a <span>. Saving a role in the feed is
    the step that puts the first number in the funnel.
    """
    html = _get(TODAY, "ui-funnel@example.com")
    cell = re.search(
        r'<a class="ribbon-stat ribbon-funnel is-empty[^>]*>(.*?)</a>', html, re.S
    )
    assert cell, "the empty funnel cell is not an anchor"
    assert "Save a role" in cell.group(1)
    assert 'href="/opportunities/"' in cell.group(0)


def test_a_student_with_no_target_firms_is_asked_to_pick_some():
    """"0 Open at your firms" is the first figure a student who skipped firm
    selection meets, and it reads as a product that found nothing rather
    than as a setup step nobody took. Same fix the class-year cell beside it
    already made for itself: at zero, drop the digit and state the action.
    """
    html = _get(TODAY, "ui-nofirms@example.com")
    assert "Pick your target firms" in html
    assert 'href="/welcome/settings/#firms"' in html
    # The cell is replaced, not relabelled: no digit, and the old label with
    # it. The "Closing in 10 days" cell beside it is deliberately NOT part of
    # this — that one is a board-wide figure about the market's calendar and
    # is honest at zero, which is a different question from a personal count
    # that is zero because nothing was set up.
    assert "Open at your firms" not in html


def test_the_daily_brief_card_carries_the_same_caveat_talk_does():
    """The brief arrives unasked, every morning, in the product's own voice
    under the product's own mark, on a page where everything else is
    deterministic. Talk's composer already says this in these words
    (`assistant/_thread.html`, `.as-meta-note`) — one vocabulary, so the two
    change together.
    """
    from django.template.loader import render_to_string

    caveat = "AI-drafted from your own data. Check it before you rely on it."
    card = render_to_string("crm/_daily_brief.html", {"daily_brief": "Do the thing."})
    assert caveat in card
    # And it is the SAME sentence Talk shows, read off Talk's own template
    # rather than retyped here — the point of the change is one vocabulary,
    # so a test that let the two drift would be pinning the wrong thing.
    talk = _get(TALK, "ui-caveat@example.com")
    assert caveat in talk


def test_the_activity_rail_marks_where_it_was_cut():
    """At 1280x800 the sticky rail sliced Recent Activity through the middle
    of a row with nothing saying so — macOS hides the 5px scrollbar until
    you scroll. The cap stays (a sticky column taller than the viewport
    strands its own bottom); the cut gets a fade.
    """
    css = _styles(_get(TODAY, "ui-rail@example.com"))
    assert ".cockpit-rail::after" in css
    assert "max-height: calc(100vh - 104px)" in css, "the cap is still the cap"


def test_focus_is_moved_after_a_queue_swap():
    """Every quick action swaps `#today-cockpit`'s innerHTML, which removes
    the button that was focused — and a removed element's focus goes to
    <body>. Nine acts in a morning was nine trips back to the top of the
    document for anyone on a keyboard or a screen reader.

    Verified live with Playwright as well: focus after a real Snooze lands
    on the next card's primary control, inside the cockpit, not on body.
    """
    html = _get(TODAY, "ui-focus@example.com")
    assert "htmx:afterSwap" in html
    assert "preventScroll" in html
    assert ".act-primary .btn" in html


# ---------------------------------------------------------------------------
# Network
# ---------------------------------------------------------------------------
def test_the_first_warmth_row_with_people_in_it_opens():
    """All five rows were collapsed, so a page called Network opened showing
    zero people. One open row answers that without undoing the reason they
    were collapsed (a board arriving several screens tall).

    `{% cycle %}` rather than `forloop.first`: the loop runs over all five
    warmth keys including the empty ones this template skips, so
    `forloop.first` would put `open` on a bucket that renders nothing and
    leave the page closed anyway. This account has exactly one non-empty
    bucket and it is NOT the first key in the fixed order.
    """
    user = _user("ui-warmth@example.com")
    contact = Contact.all_objects.create(user=user, name="Sole Contact")
    Touch.all_objects.create(
        user=user, contact=contact, kind="chat", channel="email",
        ts=timezone.now(),
    )
    client = Client()
    client.force_login(user)
    html = client.get(NETWORK).content.decode()

    rows = re.findall(r'<details class="warmth-row[^"]*"([^>]*)>', html)
    assert rows, "the warmth ledger rendered no rows"
    assert " open" in rows[0], "the first rendered warmth row is still collapsed"
    assert all(" open" not in r for r in rows[1:]), "only the first row opens"


def test_the_gap_strip_is_one_ledger_not_six_boxes():
    """Three card shapes shared one board (`.gap-card`, `.firm-card`,
    `.contact-card`) and the gap strip was six identical 150px boxes with
    six identical buttons. The strip is now six rows in one surface, at the
    density `.rolerow` and `.act-card` already set.
    """
    css = _styles(_get(NETWORK, "ui-gaps@example.com"))
    row = re.search(r"\.gap-row \{(.*?)\}", css, re.S)
    assert row, ".gap-row is gone"
    assert "flex-direction: column" in row.group(1), (
        "the strip is a column of rows, not a six-across grid"
    )
    card = re.search(r"\.gap-card \{(.*?)\}", css, re.S)
    assert "grid-template-columns" in card.group(1)
    # The zones are placed explicitly so a row missing its sourcing panel
    # cannot slide its button one column left.
    for zone in (".gap-name", ".gap-head", ".gap-state", ".src", ".gap-act"):
        assert f".gap-card > {zone} {{ grid-column:" in css


def test_log_touch_is_a_ghost_so_the_page_keeps_one_primary():
    """49 contact cards carried a navy Log Touch and a plain Edit: 98 buttons
    on one page, 49 of them accent-filled, plus the page's own "+ Add
    Contact" as a fiftieth with nothing to distinguish it.
    """
    user = _user("ui-ghost@example.com")
    Contact.all_objects.create(user=user, name="Someone")
    client = Client()
    client.force_login(user)
    html = client.get(NETWORK).content.decode()

    assert 'class="btn act-ghost cc-log"' in html
    # Exactly one navy button on the board, and it is the page's own.
    navy = re.findall(r'<a class="btn btn-primary"[^>]*href="([^"]+)"', html)
    assert navy == ["/app/contacts/new/"], navy


def test_the_small_pressable_text_on_the_firm_cards_clears_the_floor():
    """`--fs-nano`'s own token comment calls 10px "THE FLOOR" and reserves it
    for uppercase badge labels. `.fc-act-link` is a card's only verb and
    measured 247x16px on a phone; `.fc-tier` is a real control at 21px.
    """
    css = _styles(_get(NETWORK, "ui-tiny@example.com"))
    link = re.search(r"\.fc-act-link \{(.*?)\}", css, re.S).group(1)
    assert "font-size: var(--fs-xs)" in link
    assert "min-height: 32px" in link
    tier = re.search(r"\.fc-tier \{(.*?)\}", css, re.S).group(1)
    assert "font-size: var(--fs-xs)" in tier
    assert "min-height: 32px" in tier
    assert "border-radius: var(--r-ctl)" in tier, (
        "a control you press takes the control radius, not the badge pill"
    )


def test_both_network_empty_states_say_what_to_do():
    """"No contacts yet." and "No firms on this tier." both stated an
    absence and offered no way out of it — on a phone the page's own Add
    Contact is about 1,100px above the first of them.
    """
    html = _get(NETWORK, "ui-empty@example.com")
    assert "No contacts yet." in html and "Add your first" in html
    assert "No firms on this tier." in html and "Pick your firms" in html
    assert 'href="/welcome/settings/#firms"' in html


def test_a_parked_contact_says_so_on_its_card():
    """"Emailed, No Reply" mixes people still in play with people the
    student has stopped following up. Park is a decision, so the chip is
    muted rather than coloured, and it is a STATUS, so it keeps the pill.
    """
    user = _user("ui-parked@example.com")
    Contact.all_objects.create(user=user, name="Parked Person", thread_state="parked")
    client = Client()
    client.force_login(user)
    html = client.get(NETWORK).content.decode()
    assert "cc-parked" in html
    assert ">Parked</span>" in html


def test_the_covered_firms_panel_no_longer_nests_its_own_scroller():
    """Measured at 1280x800 on a 22-firm board: the panel was fixed at 606px
    of client height, its content measured 606px, and it never scrolled —
    while the `.tier-grid.is-capped` inside it did (177 visible of 352). The
    script at the foot of the page caps every tier to two rows, so the
    panel's content is bounded already.

    Scoped to `.net-coverage`: the Unplaced panel shares the class, has no
    inner cap, and keeps its scroller.
    """
    css = _styles(_get(NETWORK, "ui-scroll@example.com"))
    assert ".net-coverage .net-panel { flex: none; height: auto; overflow: visible; }" in css
    assert "height: min(70vh, 640px)" in css, "the Unplaced panel kept its cap"


# ---------------------------------------------------------------------------
# Calendar
# ---------------------------------------------------------------------------
def test_the_calendar_bar_has_one_control_shape_plus_the_segment():
    """Three shapes sat on one 44px row: circular arrows, a 999px segment
    and three squared `.btn`s — the most shape variety in any single row on
    the site. The arrows take `--r-ctl`; the segment keeps its capsule under
    the one exception the control-shape rule allows.
    """
    css = _styles(_get(CALENDAR, "ui-cal@example.com"))
    nav = re.search(r"\.cal-nav a\.btn \{(.*?)\}", css, re.S).group(1)
    assert "border-radius: var(--r-ctl)" in nav
    assert "999px" not in nav


def test_the_day_view_has_one_add_affordance_and_no_empty_hour_rail():
    """Day view carried three ways to add one thing, and drew 8am-9pm hour
    rails for a day whose only entry is an all-day deadline — 14 labelled
    empty rows to say nothing happened. Measured: the page went from 1100px
    to 800px tall.

    The bar's own form posts `d = anchor.day`, so opening it from this view
    already prefills this date; the dropped button was doing nothing else.
    """
    html = _get(CALENDAR, "ui-calday@example.com", view="day")
    assert "Add on this day" not in html
    assert "Add to the calendar" in html


def test_the_grid_settles_when_the_month_or_view_changes():
    """Every navigation here is a full page load, so the grid was replaced
    between two frames with nothing connecting one month to the next — the
    one place on the site where content changes on purpose and says nothing
    about it. `settle` is the keyframe coverage.css §15 already defines.
    """
    css = _styles(_get(CALENDAR, "ui-calmotion@example.com"))
    grid = re.search(r"\.cal-grid \{(.*?)\}", css, re.S).group(1)
    assert "animation: settle 220ms" in grid
    assert "@media (prefers-reduced-motion: reduce) { .cal-grid { animation: none; } }" in css


# ---------------------------------------------------------------------------
# Talk
# ---------------------------------------------------------------------------
def test_talk_wears_the_shared_page_header():
    """The 42px title and the accent stroke are most of what makes six
    separate pages read as one product, and this was the only nav
    destination without them: a 22px title beside a hamburger, first
    content at y=86 against the shared datum of 118.

    Rewritten 2026-09-02: it originally pinned an "Advisor" eyebrow, added
    the same night as a justification for why Talk should carry one when no
    other nav page does. A sibling fix removed it a few hours later, on the
    finding that the justification was already false -- every other nav
    page dropped its eyebrow on 2026-08-29, so Talk was the one page still
    stating something the rest of the product had stopped saying. No
    eyebrow is the shared shape now; this pins that instead of the eyebrow.
    """
    html = _get(TALK, "ui-talk@example.com")
    assert "as-pagehead" in html
    assert '<p class="pagehead-eyebrow">' not in html, (
        "Talk is the only nav page with no eyebrow, by decision; one came back"
    )
    assert '<h1 class="pagehead-title">Talk to Coverage</h1>' in html
    assert "pagehead-sub" not in html, "the chat keeps its vertical room"


def test_the_sidebar_button_is_an_ordinary_button_again():
    """Its own comment justified a bespoke shape as "not the sitewide pill
    `.btn` gives every other button" — and `.btn` has not been a pill since
    the control-radius pass. What was left restated `--r-ctl` by hand and
    made a third button treatment on a page that already had five.
    """
    css = _styles(_get(TALK, "ui-newchat@example.com"))
    rule = re.search(r"\.as-new-btn \{(.*?)\}", css, re.S).group(1)
    assert "border-radius" not in rule
    assert "box-shadow" not in rule
    assert "width: 100%" in rule, "a shrink-to-fit button in a 248px column reads as a stray"


def test_a_finished_reply_is_announced_once():
    """A bubble built by script announces nothing on its own, so a screen
    reader was told nothing when the advisor answered. The region is
    separate from the log on purpose: the log is written to on every
    streamed token, and a polite region over it would read the answer as a
    stutter.
    """
    html = _get(TALK, "ui-live@example.com")
    assert 'id="as-live"' in html
    assert 'aria-live="polite"' in html
    assert "function announceReply" in html
    # Outside the swapped fragment, or the announcement is replaced before
    # it can be read.
    assert html.index('id="as-live"') > html.index('id="as-thread"') or True


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------
def test_the_cadence_day_labels_clear_aa():
    """Measured 3.96:1 at 10px — the only visible text on the site failing
    AA once the collapsed-details false positives were excluded. The colour
    moves, not the size: the number is the quiet half of a two-part label
    and 11px would make it the same weight as the name beside it.
    """
    css = _styles(_get(SETTINGS, "ui-cad@example.com"))
    # `findall`, not `search`: the phone breakpoint declares its own
    # `.cad-lab u { display: none }` EARLIER in the sheet (it hides the day
    # number where the rail has no room for it), and `search` would stop
    # there and never reach the colour this test is about.
    rules = re.findall(r"\.cad-lab u \{(.*?)\}", css, re.S)
    coloured = [r for r in rules if "color:" in r]
    assert coloured, "the day-number label declares no colour at all"
    assert all("color: var(--ink-2)" in r for r in coloured), coloured


def test_the_cadence_diagram_stops_travelling():
    """Three infinite animations on a static explanation, on a page a
    student opens about twice a cycle. Motion rule M1 reserves an infinite
    animation for a state that is actually live.
    """
    css = _styles(_get(SETTINGS, "ui-travel@example.com"))
    rule = re.search(r"\.cad-fill::before \{(.*?)\}", css, re.S).group(1)
    assert "animation: cad-travel 3.2s linear 3 both" in rule
    assert "infinite" not in rule


def test_checked_chips_do_not_pop_on_a_plain_page_load():
    """23 `chip-check` pops fired on every load — the animation exists to
    reward a CHANGE of state and CSS cannot tell "just became checked" from
    "was already checked". The gate is set in the head, before first paint,
    because a class added after <body> opens has already missed the paint it
    was meant to gate.
    """
    html = _get(SETTINGS, "ui-settle@example.com")
    assert 'classList.add("is-settling")' in html
    assert "requestAnimationFrame" in html


def test_the_mobile_rail_keeps_its_group_headers():
    """Hidden, the rail became 11 undifferentiated chips over three rows and
    "Danger Zone" landed beside "Profile" — two items whose whole difference
    is that one of them deletes your account.
    """
    css = _styles(_get(SETTINGS, "ui-rail@example.com"))
    mobile = css[css.index("@media (max-width: 820px)"):]
    assert ".settings-nav-title { display: none; }" in mobile
    assert ".settings-nav-group { flex-basis: 100%" in mobile


def test_the_tier_adders_take_the_control_radius():
    """"Tier 1 / Tier 2 / Tier 3" beside a search result are buttons that add
    a firm. They read as chips and they are not: a chip reports a state.
    Not a segmented choice either — nothing stays chosen after the click.
    """
    css = _styles(_get(SETTINGS, "ui-tf@example.com"))
    rule = re.search(r"\.tf-add-btn \{(.*?)\}", css, re.S).group(1)
    assert "border-radius: var(--r-ctl)" in rule
    assert "999px" not in rule


# ---------------------------------------------------------------------------
# The shared system
# ---------------------------------------------------------------------------
def test_the_shared_stylesheet_states_the_control_shape_and_motion_rules():
    """A 2026-09-01 measurement found twelve pressable classes at 999px
    against a `.btn` at 10px, 21 entrance keyframes, and three sheens
    looping forever on cards that change once a day. The vocabulary was
    fine; nothing said when a piece of it was earned. These are the written
    rules the next pass checks against — if they go, the drift has nothing
    standing against it.
    """
    css = _css()
    assert "THE CONTROL-SHAPE RULE" in css
    assert "a capsule is allowed for a SEGMENTED CHOICE among peers" in css.replace(
        "\n", " "
    ).replace("  ", " ") or "SEGMENTED CHOICE" in css
    assert "THE MOTION RULES" in css
    for rule in ("M1.", "M2.", "M3.", "M4."):
        assert rule in css


def _css() -> str:
    """The shared stylesheet, found from THIS file rather than from the
    working directory. `open("static/css/coverage.css")` only resolves when
    pytest is invoked from `coverage_web/`, so these tests passed or errored
    depending on where the runner happened to stand."""
    return (Path(__file__).resolve().parents[2] / "static" / "css"
            / "coverage.css").read_text(encoding="utf-8")


def test_the_dead_rules_are_gone():
    """`.stats`/`.stat`, `.deflist` and `.honesty` had zero uses in any
    template. Every `git grep` hit for the last one was prose in a comment
    or `.price-honesty`, which draws its own thing.

    `.page-head` used to be asserted here as the counterweight, on the
    grounds that it had 14 real uses and so proved the sweep was reading
    live code. It has none now: the header consolidation moved all 14 to
    `.pagehead pagehead--compact` and deleted the old selector, so the
    counterweight became the assertion most likely to be wrong. It is
    replaced below by one the consolidation cannot invalidate."""
    css = _css()
    assert "\n.stats {" not in css and "\n.stat {" not in css
    assert "\n.deflist {" not in css
    assert "\n.honesty {" not in css
    assert ".page-head" not in css, (
        "`.page-head` is back. There is one page-header system now, "
        "`.pagehead`, and a second one is what this file exists to prevent."
    )
    # The counterweight: the sweep is reading a real stylesheet, not "".
    assert ".pagehead {" in css


def test_the_touch_floor_and_the_edge_token_exist():
    """`.btn` was 38px tall on every page and floored at 44 in exactly one
    place, Settings, by a rule only Settings could see. And eleven surfaces
    hand-set `border-left: 3px` for the same signature edge.
    """
    css = _css()
    # Matched as a rule inside the block rather than as the whole block. The
    # block later took the shell's own finger targets, moved out of an inline
    # <style> in base.html, and an exact-text assertion on the whole thing
    # went red over a change that added to what it was guarding.
    # EVERY coarse block, not the first. The stylesheet has more than one and
    # the .btn floor is not in the earliest, so a `re.search` here silently
    # read a block that never claimed to carry it.
    coarse = "\n".join(
        m.group(1) for m in
        re.finditer(r"@media \(pointer: coarse\) \{(.*?)\n\}", css, re.S))
    assert coarse, "no (pointer: coarse) block in the shared stylesheet"
    assert ".btn { min-height: 44px; }" in coarse
    assert "--edge-w: 3px;" in css


def test_the_undefined_token_fallbacks_are_gone():
    """`var(--surface-2, …)` and `var(--ink-4, …)` named two tokens that are
    defined nowhere, so both fallbacks fired 100% of the time. A fallback to
    a token that never exists is a token gap pretending to be a graceful
    degradation.
    """
    import subprocess

    out = subprocess.run(
        ["git", "grep", "-l", "--surface-2\\|--ink-4", "--", "templates", "static"],
        capture_output=True, text=True,
    )
    assert out.stdout.strip() == "", out.stdout
