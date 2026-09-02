"""The Today rail's 2026-09-02 copy pass — every card, what it may not say again.

THE COMPLAINT. The founder read the rail and said the cards were wordy and the
spacing was wrong, and the cause was known: the pass of 2026-08-31 made every
claim state its provenance in prose, and the sentences stacked. The rule this
file enforces is the one that pass needed and did not have — keep every FACT,
cut the EXPLANATION. A fact the student acts on is a line; a fact about where
that fact came from is a `title`, a chip, or nothing.

So the assertions here come in pairs. Each one that forbids a phrase is
followed by one that pins the fact the phrase used to carry, because the
failure mode of a copy diet is not a sentence surviving, it is a fact leaving
with it.

Measured with headless Playwright at 1280x800 and 375x812 in both colour
schemes against the demo account. Rail card heights, desktop, before -> after:

    Pace                157 -> 151
    Schedule            205 -> 167
    Deadlines           138 -> 138   (rewritten, not shortened)
    Where do they sit?  247 -> 229
    Recent Activity     310 -> 310   (rewritten, not shortened)

and the stacked rail at 375px, 1088 -> 1059.

THE SAME NIGHT, A THIRD PASS on the two cards this file's section 5 covers.
The founder read the shortened rail and asked for the sparkline to go and
for the two cards to become one ("remove the bar below, just leave 76 /14
OUTREACH THIS WEEK / Weekly goal hit. Combine this with unsorted contacts,
make into one widget"), so those two rows above are now one row:

    Pace + Where do they sit?   327 -> 245   at 1280x800
                                333 -> 251   at 375x812

where the "before" is the two card boxes plus the 16px rail gap between
them, both colour schemes identical at both widths. The pace half alone,
with nothing unplaced, is 100px.

A Django test client has no layout engine, so those numbers are quoted
rather than re-run; what is checked here is the markup and the CSS text that
produce them.
"""

from __future__ import annotations

import datetime as dt
import re
from pathlib import Path

import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse
from django.utils import timezone

from crm.models import Contact, Touch
from crm.today import _next_deadlines, _recent_activity
from directory.models import Firm, FirmDate

pytestmark = pytest.mark.django_db

User = get_user_model()

STYLES = Path(__file__).resolve().parents[2] / "templates" / "crm" / "_styles.html"
COCKPIT = Path(__file__).resolve().parents[2] / "templates" / "crm" / "_cockpit.html"


def _user(email="rail@example.com", tracks=("st",)):
    return User.objects.create_user(
        email=email, password="pw12345!", regions=["us", "hk"],
        tracks=list(tracks),
    )


def _page(user) -> str:
    from django.test import Client
    client = Client()
    client.force_login(user)
    res = client.get(reverse("crm:week"))
    assert res.status_code == 200
    return res.content.decode()


def _styles_of(html: str, *, strip_comments: bool = False) -> str:
    """EVERY <style> block on the page, joined.

    Not the first. The page carries more than one and which block holds a
    given rule is the base template's business, not this file's — reading
    only the first is how four guards broke on the night of 2026-09-01.

    `strip_comments` for the assertions that say a rule is GONE: this
    stylesheet explains itself at length directly above each rule, and the
    comment explaining why `.pace-ring` was deleted contains the string
    `.pace-ring`. Same class of trap as `_rules` in
    `directory/tests/test_styles_block.py` — the file's own prose is inside
    the string under test.
    """
    blocks = re.findall(r"<style[^>]*>(.*?)</style>", html, re.S)
    assert blocks, "the page no longer renders a <style> block"
    css = "\n".join(blocks)
    return re.sub(r"/\*.*?\*/", "", css, flags=re.S) if strip_comments else css


def _card(html: str, heading: str) -> str:
    """One rail card, so an assertion about it cannot be satisfied by the
    rest of a large page."""
    start = html.index(f'<h3 class="rail-title">{heading}')
    return html[start:html.index("</div>", start)]


def _pace_card(html: str) -> str:
    """The pace card and everything now inside it.

    Anchored on the CLASS NAME, never on a whole class attribute: this card
    has picked up `panel` (D-13) and could pick up more, and matching the
    attribute exactly is how four guards broke on the night of 2026-09-01.

    The end of the slice is the card's own closing tag, found by counting
    `<div` against `</div>` from the opening tag. It used to be "the first
    `</div>` after `pace-spark`", which stopped existing when the sparkline
    was deleted — and would have been wrong anyway from 2026-09-02, when the
    unplaced block moved inside this card and gave it a nested div.
    """
    start = html.index('class="rail-card pace-card')
    start = html.rindex("<div", 0, start)
    depth, i = 0, start
    while True:
        nxt_open = html.find("<div", i)
        nxt_close = html.index("</div>", i)
        if nxt_open != -1 and nxt_open < nxt_close:
            depth += 1
            i = nxt_open + 4
            continue
        depth -= 1
        i = nxt_close + 6
        if depth == 0:
            return html[start:i]


def _css_rule(css: str, selector: str) -> str:
    flat = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    m = re.search(r"^\s*" + re.escape(selector) + r"\s*\{(.*?)\}", flat, re.S | re.M)
    assert m, f"no rule found for {selector}"
    return m.group(1)


# ---------------------------------------------------------------------------
# 1. Schedule — the send-window hints.
# ---------------------------------------------------------------------------
def _st_user_with_two_markets(email="rail@example.com"):
    """An S&T student with contacts in both markets AND something on the
    schedule.

    The second half is not decoration: the hints render inside the Schedule
    card, which is gated on a non-empty `schedule`, so a fixture with only
    contacts renders no card and no hint to assert about.
    """
    user = _user(email=email)
    for i, region in enumerate(["hk", "us"]):
        Contact.all_objects.create(
            user=user, name=f"Desk {region} {i}", region=region)
    booked = Contact.all_objects.create(
        user=user, name="Lily Liu", thread_state="chat_scheduled")
    Touch.all_objects.create(
        user=user, contact=booked, kind="reply_received", channel="email",
        ts=timezone.now() - dt.timedelta(days=1),
    )
    return user


def test_a_market_gets_one_row_and_not_a_sentence():
    """BEFORE: "HK desk good 9p to 10:30p, avoid 12:30a to 1:30a around the
    close", twice, wrapping to four rendered lines above a card whose subject
    is the day's schedule — most of the card, before a single scheduled thing
    appeared.

    A row per market, structurally: the hints are a list now, so "one idea per
    line" is enforced by the markup rather than by however wide the card
    happens to be.
    """
    card = _card(_page(_st_user_with_two_markets("row@example.com")), "Schedule")
    assert '<ul class="daybar-hint">' in card, (
        "the hints are a list of rows, not a wrapping paragraph of clauses"
    )
    assert card.count('<li class="dbh"') == 2, (
        "one row per market the student actually has contacts in"
    )


def test_the_close_is_no_longer_explained_in_the_copy():
    """"around the close" told the reader WHY that half hour is bad. It adds
    no time to avoid — `avoid` already carries a window of its own — so it was
    the sentence explaining the fact rather than the fact. It moved to the
    row's `title`, which is where a provenance fact lives on this page."""
    card = _card(_page(_st_user_with_two_markets("close@example.com")), "Schedule")
    assert "around the close" not in card
    assert "either side of that market's close" in card, (
        "the reason the window is bad is still available, in the row's title"
    )


def test_both_windows_survive_the_diet():
    """The good window and the avoid window are two different facts, and a
    good window with no avoid window is a smaller claim, not a shorter one.
    Nothing here trades a fact for a line."""
    card = _card(_page(_st_user_with_two_markets("both@example.com")), "Schedule")
    hk = re.search(r'<li class="dbh"[^>]*>(.*?)</li>', card, re.S).group(1)
    assert "good " in hk and "avoid " in hk
    assert "desk" in hk, "the row still says whose day this is a fact about"


def test_the_hint_row_is_the_rails_own_content_left_meta_right_shape():
    """Not a new layout: `.activity-row` three cards down already puts the
    subject left and its meta right, and the good window is the actionable
    half."""
    css = _css_rule(_styles_of(_page(_st_user_with_two_markets("shape@example.com"))), ".dbh")
    assert "justify-content: space-between" in css
    assert "display: flex" in css


def test_an_ib_only_student_still_gets_no_hint_at_all():
    """P1 and the scoping in `_send_windows`: the source is a trading floor's
    day. Reshaping the copy must not widen who it is shown to."""
    user = _user(email="ib@example.com", tracks=("ib",))
    Contact.all_objects.create(user=user, name="Banker", region="hk")
    booked = Contact.all_objects.create(
        user=user, name="Booked", thread_state="chat_scheduled")
    Touch.all_objects.create(
        user=user, contact=booked, kind="reply_received", channel="email",
        ts=timezone.now() - dt.timedelta(days=1),
    )
    # The Schedule card itself renders; the hint block inside it does not.
    # Asserted on the MARKUP, not on the page text: `.daybar-hint` is styled
    # in the shared block whether or not any student sees it.
    card = _card(_page(user), "Schedule")
    assert '<ul class="daybar-hint">' not in card


# ---------------------------------------------------------------------------
# 2. Deadlines — three fields, and two day counts that pointed opposite ways.
# ---------------------------------------------------------------------------
def _deadline_user():
    user = _user(email="dl@example.com", tracks=("ib",))
    firm = Firm.objects.create(slug="hsbc", name="HSBC")
    FirmDate.objects.create(
        firm=firm, cycle="sa2028", event_kind="applications_close",
        date=timezone.localdate() + dt.timedelta(days=58),
        confidence=1.0, precision="day", region="hk",
    )
    return user


def test_the_market_is_a_chip_and_not_a_third_word_in_a_phrase():
    """BEFORE: the market and the event label were both plain
    `.activity-kind` spans separated by a space, so the row read "HSBC HK
    Applications close" — a firm, a market and an event with no seam, which
    parses as one confusing phrase and not as three fields.

    The market is the shortest and most categorical of the three, so it is the
    one that becomes the chip."""
    card = _card(_page(_deadline_user()), "Deadlines")
    assert '<span class="dl-market">HK</span>' in card
    assert "HSBC" in card and "applications close" in card.lower(), (
        "all three fields survive the separation"
    )


def test_the_market_chip_is_styled():
    """A chip whose class is styled nowhere renders as bare text in the middle
    of a designed page, and no markup assertion would notice."""
    css = _css_rule(_styles_of(_page(_deadline_user())), ".dl-market")
    assert "border" in css, "a chip needs an edge or it is not a seam"


def test_a_countdown_says_which_direction_it_points():
    """THE DEFECT, in the founder's own screenshot: "58d" on the right of a
    row and "24d" an inch under it, two identical-looking day counts pointing
    in OPPOSITE directions of time. 58 days until an application close; 24
    days a posting has ALREADY been open, an elapsed figure that
    `directory.open_runs` argues at length is explicitly not a forecast.

    This is a presentation fix and not a data change: `days` is untouched, no
    number moved, nothing was dropped. The rail's rule is now stated on both
    sides — a bare "Nd" is time already spent, and a future distance says
    "in".
    """
    user = _deadline_user()
    row = _next_deadlines(user, timezone.localdate())[0]
    assert row["when"] == "in 58d"
    assert row["days"] == 58, "the number itself did not move"


def test_today_is_still_today_and_not_in_0d():
    """Zero days away is not a distance, and "in 0d" would be the kind of
    mechanical phrasing that reads as a bug."""
    user = _user(email="dl0@example.com", tracks=("ib",))
    firm = Firm.objects.create(slug="ms", name="Morgan Stanley")
    FirmDate.objects.create(
        firm=firm, cycle="sa2028", event_kind="applications_close",
        date=timezone.localdate(), confidence=1.0, precision="day", region="us",
    )
    assert _next_deadlines(user, timezone.localdate())[0]["when"] == "today"


def test_the_two_day_counts_on_one_card_can_no_longer_read_alike():
    """The pair, checked together on the rendered card rather than one at a
    time — the defect only existed because they shared a card."""
    from directory.models import Opportunity
    user = _deadline_user()
    firm = Firm.objects.get(slug="hsbc")
    today = timezone.localdate()
    for i, age in enumerate([70, 24, 20, 9]):
        o = Opportunity.objects.create(
            firm=firm, title=f"SA {i}", bucket="internship", status="open",
            url=f"https://x.test/hsbc/{i}",
        )
        Opportunity.objects.filter(pk=o.pk).update(
            first_seen=timezone.make_aware(
                dt.datetime.combine(today - dt.timedelta(days=age), dt.time(9)),
                dt.timezone.utc,
            )
        )
    card = _card(_page(user), "Deadlines")
    assert "in 58d" in card, "the countdown carries its direction"
    assert "longest 24d" in card, "the elapsed figure is still stated"
    assert ">58d<" not in card, "a bare countdown is what caused the confusion"


# ---------------------------------------------------------------------------
# 3. Where do they sit? — the note, not the heading.
# ---------------------------------------------------------------------------
def _unplaced_user():
    user = _user(email="unpl@example.com", tracks=("ib",))
    for i in range(3):
        Contact.all_objects.create(user=user, name=f"Arrival {i}", region="")
    return user


def test_the_heading_and_the_verb_are_untouched():
    """Both are the founder's own words from 2026-08-31 and the template
    comment holds the argument for them. A copy pass that shortens the line
    under them must not quietly relitigate them.

    The verb's MARKUP changed on 2026-09-02 ("make Place them into a
    button"), which is why this no longer looks for `>Place them</a>` inside
    a `.rail-more`: the words are the claim, the element is not. That the
    element is the shared `.btn` is pinned in
    `crm/tests/test_unplaced_arrivals.py`.
    """
    card = _card(_page(_unplaced_user()), "Where do they sit?")
    assert "Where do they sit?" in card
    assert ">Place them</a>" in card


def test_the_line_under_the_heading_is_a_count_and_keeps_both_its_facts():
    """REWRITTEN 2026-09-02; the note it pinned was cut later the same day.

    It read `test_the_note_is_one_line_and_keeps_both_its_facts` and pinned
    "New this week. Deadlines match by market." as the one-line survivor of a
    two-line note. Then the names under it went ("just show how many people
    need to be placed"), and with a count on the face the note's first fact
    was said twice and its second was pure mechanism.

    So the count line carries both facts the note was kept for: the window
    ("new this week"), which is why this card exists rather than listing
    every region-less contact on the account, and the missing fact itself
    ("no market set"). The mechanism is a `title`, pinned by the next test,
    which is the same place it was already living.
    """
    card = _card(_page(_unplaced_user()), "Where do they sit?")
    line = re.search(r'<p class="unplaced-count"[^>]*>(.*?)</p>', card, re.S).group(1)
    assert re.sub(r"<[^>]+>", "", line).strip() == "3 new this week, no market set."
    assert "until you say" not in card
    assert "Coverage cannot match" not in card
    assert "unplaced-note" not in card, (
        "the note is back on top of the count line that replaced it"
    )


def test_the_mechanism_the_note_stopped_explaining_is_still_reachable():
    """It is a fact about how the product works, not one the student acts on,
    so it is a `title` now. Cut the explanation, do not delete it."""
    card = _card(_page(_unplaced_user()), "Where do they sit?")
    assert "matches a firm's deadlines to a person by market" in card
    assert "nothing infers one" in card, (
        "P1 still has to be visible somewhere on this card: the product does "
        "not guess a market"
    )


# ---------------------------------------------------------------------------
# 4. Recent Activity — a label that repeated the heading, six times.
# ---------------------------------------------------------------------------
def test_the_activity_rail_does_not_say_ago_under_a_heading_that_says_recent():
    """Six rows each ending "ago" under a card titled Recent Activity. "ago"
    is grammar, not a fact — the heading already places every row in the
    past — and it is exactly the "no label that repeats the heading directly
    above it" rule.

    It also settles the rail's one shared reading rule from the other side:
    a bare "Nd" anywhere in this rail is elapsed time, and a future distance
    carries "in" (see the Deadlines tests above)."""
    user = _user(email="act@example.com", tracks=("ib",))
    contact = Contact.all_objects.create(user=user, name="Ada Lovelace")
    Touch.all_objects.create(
        user=user, contact=contact, kind="outreach", channel="email",
        ts=timezone.now() - dt.timedelta(days=10),
    )
    rows = _recent_activity(user, as_of=timezone.now())
    assert rows[0]["ago"] == "10d"
    card = _card(_page(user), "Recent Activity")
    assert "ago" not in card


def test_a_touch_logged_today_still_says_today():
    """"0d" is not how anyone says it, and the zero case was never the
    complaint."""
    user = _user(email="act0@example.com", tracks=("ib",))
    contact = Contact.all_objects.create(user=user, name="Grace Hopper")
    Touch.all_objects.create(
        user=user, contact=contact, kind="outreach", channel="email",
        ts=timezone.now(),
    )
    assert _recent_activity(user, as_of=timezone.now())[0]["ago"] == "today"


# ---------------------------------------------------------------------------
# 5. Pace — one week, stated once.
# ---------------------------------------------------------------------------
def test_the_pace_ring_is_gone_and_took_its_css_with_it():
    """THE COMPLAINT: "too complicated, refine with opus." The card stated one
    week four times — the ring's arc, the figure, the note, and the last bar
    of the sparkline. On the founder's own screenshot the week was 39 against
    a goal of 14, so `pace.pct` clamped to 100, the arc closed into a full
    circle, and the ring rendered as a plain outline indistinguishable from
    its own empty track. A meter that looks the same at 100% as at 0% is not
    a meter.

    Deleted rather than left as dead rules, which is the other half of the
    same cleanup.

    THE SPARKLINE FOLLOWED IT the same day (see
    `test_the_pace_card_carries_no_picture_at_all`), so the slice below can
    no longer end at `pace-spark` and ends at the card's own closing tag
    instead. `pace-grow` stays on the dead list and `mrail-grow` joins it:
    the sparkline's bars were the only thing in this stylesheet using it.
    """
    html = _page(_user(email="pace@example.com", tracks=("ib",)))
    # 'class="rail-card pace-card"' stopped matching once D-13's panel
    # primitive appended "panel" to the same attribute (2026-09-02).
    card = _pace_card(html)
    assert "pace-ring" not in card
    css = _styles_of(html, strip_comments=True)
    for dead in (".pace-ring", ".pace-track", ".pace-fill", "pace-grow",
                 ".pace-spark", "mrail-grow"):
        assert dead not in css, f"{dead} is dead CSS now that the ring is gone"


def test_the_week_is_still_stated_to_the_unit():
    """REWRITTEN 2026-09-02, second pass. It was
    `..._and_still_has_its_memory` and its second half pinned the sparkline
    as the visual that survived the ring, on the argument that eight weeks
    beat one.

    The founder answered that argument a few hours later by asking for no
    picture at all ("remove the bar below, just leave 76 /14 OUTREACH THIS
    WEEK / Weekly goal hit."), so the memory is gone and only the fact it was
    drawn beside remains. That fact is what this test was always really
    protecting: the ring measured nothing of its own — it drew `done / goal`
    imprecisely and capped — and neither did the bars, so what has to survive
    every one of these deletions is the count, exactly, to the unit.
    """
    user = _user(email="pace2@example.com", tracks=("ib",))
    contact = Contact.all_objects.create(user=user, name="Katherine Johnson")
    Touch.all_objects.create(
        user=user, contact=contact, kind="outreach", channel="email",
        ts=timezone.now(),
    )
    html = _page(user)
    assert 'class="pace-done">1</span>' in html
    assert 'class="pace-goal">/' in html
    assert "Last 8 weeks:" not in html, (
        "the sparkline's aria-label is back; the eight-week memory was "
        "deleted, not hidden"
    )


def test_the_figure_label_cannot_break_apart_in_the_middle_of_itself():
    """"OUTREACH THIS WEEK" wrapped onto a second line beside the numeral, and
    the ring is why. Measured on the widest week the figure shows (39/14) the
    pair needs 204px; the padded rail card offers 268px at 1280 and 299px once
    the rail stacks at 375. The ring and its gap were taking 72px, which is
    the whole of the shortfall — so with the ring gone the label goes back on
    the figure's line, one fewer block in a card already read as having too
    many.

    `nowrap` is the guard: the row may still push the label to its own line at
    some width nobody renders at, but the label can never split mid-phrase.
    """
    html = _page(_user(email="pace3@example.com", tracks=("ib",)))
    figure = re.search(r'<p class="pace-figure">(.*?)</p>', html, re.S).group(1)
    assert 'class="pace-lbl"' in figure, (
        "the label shares the figure's line; the ring is what it had no room "
        "beside"
    )
    assert "white-space: nowrap" in _css_rule(_styles_of(html), ".pace-lbl")


def test_the_pace_card_carries_no_picture_at_all():
    """REWRITTEN 2026-09-02, second pass; it read
    `..._carries_one_picture_not_two_of_the_same_number` and pinned exactly
    one sparkline in a card that had just lost its ring.

    That version answered "which of the two pictures stays". The founder's
    next instruction answered a question it had not asked — whether the card
    wants a picture — with "remove the bar below, just leave 76 /14 OUTREACH
    THIS WEEK / Weekly goal hit." A rail card is read in the seconds between
    two queue rows, and in those seconds an eight-week shape is decoration
    beside a figure that already states the week to the unit.

    So the count that matters is zero, and it is asserted over the card as it
    is now — the unplaced block moved inside it the same day, and this guard
    has to keep holding with that block present. The `.unplaced-act` button
    is not a picture; `<svg>`, `<canvas>` and the spark classes are.
    """
    user = _user(email="pace4@example.com", tracks=("ib",))
    contact = Contact.all_objects.create(user=user, name="Mary Jackson")
    Touch.all_objects.create(
        user=user, contact=contact, kind="outreach", channel="email",
        ts=timezone.now(),
    )
    card = _pace_card(_page(user))
    for drawing in ("<svg", "<canvas", "pace-spark", "role=\"img\""):
        assert drawing not in card, (
            f"the pace card grew a {drawing}; the figure and the note are "
            "the whole of its top half"
        )


# ---------------------------------------------------------------------------
# 5b. Pace and "Where do they sit?" as ONE widget (2026-09-02, third pass).
#
# "Combine this with unsorted contacts, make into one widget." Both halves
# are facts about this week, which is why they are together, but they are not
# the same KIND of fact: the pace figure is a self-set goal you are measured
# against and has no action at all, while the unplaced block is a queue of
# work waiting on you and has exactly one. Everything below pins that
# distinction being carried by the markup rather than by a comment.
# ---------------------------------------------------------------------------
def _merged_user(email="merge@example.com"):
    """Both halves at once: a pace figure and something unplaced under it.

    Deliberately NOT `_unplaced_user` from section 3 — that one is shared by
    the copy tests above and a rename there would be a second edit to guards
    this change has no business touching.
    """
    user = _user(email=email, tracks=("ib",))
    Contact.all_objects.create(user=user, name="Ada Lovelace", region="us")
    Contact.all_objects.create(user=user, name="Jude Yoon", source="capture")
    return user


def test_the_two_cards_are_one_card(client):
    """The founder asked for one widget, so there is one rail card carrying
    both halves and no second card carrying either.

    `rail-card` is the load-bearing token: the unplaced block still has its
    own `unplaced-card` class (its styling and every guard in
    `test_unplaced_arrivals.py` hang off it) and what it no longer has is the
    class that makes something a card in this rail.
    """
    user = _merged_user()
    from crm.today import _cockpit_context
    assert _cockpit_context(user)["unplaced_arrival_count"], (
        "precondition: an unplaced arrival exists, so both halves render"
    )

    html = _page(user)
    assert 'class="rail-card unplaced-card' not in html, (
        "the unplaced block is a rail card again; it belongs inside the pace "
        "card"
    )
    card = _pace_card(html)
    assert 'class="pace-figure"' in card
    assert "Where do they sit?" in card
    assert "Place them" in card


def test_the_pace_figure_is_the_headline_and_the_count_is_not(client):
    """One headline per card. The pace figure keeps `--fs-figure` in the
    display face; the count beside "new this week" is demoted to `--fs-l`,
    which is what stops the merged card from reading as two numbers arguing
    about which one you were meant to look at."""
    html = _page(_merged_user(email="merge2@example.com"))
    css = _styles_of(html)
    assert "var(--fs-figure)" in _css_rule(css, ".pace-done")
    count = _css_rule(css, ".unplaced-n")
    assert "font-size: var(--fs-l)" in count
    assert "var(--fs-figure)" not in count


def test_the_merged_card_has_exactly_one_primary(client):
    """The pace half has no action at all, which is precisely what earns the
    unplaced half's verb the only accent fill in the widget. A second primary
    in here would make the card ask twice."""
    card = _pace_card(_page(_merged_user(email="merge3@example.com")))
    assert card.count("btn-primary") == 1, (
        f"expected one primary in the merged card; card was:\n{card}"
    )
    assert re.search(r'class="btn btn-primary[^"]*"[^>]*>Place them</a>', card)
    assert "rail-more" not in card, (
        "the corner text link is back; the founder replaced it with a button"
    )


def test_the_halves_are_told_apart_by_a_surface_not_by_a_rule(client):
    """The distinction is carried by an inset panel, not by a hairline with
    two stacked blocks either side of it. `panel panel--inset panel--flat` is
    the shared primitive doing all of it, so the merged card declares no
    panel shape of its own — and `--flat` matters: a panel inside a panel
    that casts a shadow looks like it is floating off its own card."""
    html = _page(_merged_user(email="merge4@example.com"))
    block = re.search(r'<div class="([^"]*\bunplaced-card\b[^"]*)"', html)
    assert block, "the unplaced block did not render"
    classes = block.group(1).split()
    for modifier in ("panel", "panel--inset", "panel--flat"):
        assert modifier in classes, (
            f"the unplaced block is missing {modifier}; it is meant to be "
            f"the shared primitive, not a local shape. Got: {classes}"
        )
    local = _css_rule(_styles_of(html), ".unplaced-card")
    for redeclared in ("background", "box-shadow", "border-radius",
                       "border:", "border-top", "border-bottom"):
        assert redeclared not in local, (
            f"`.unplaced-card` sets {redeclared}, which is the panel "
            "primitive's job"
        )


def test_with_nothing_unplaced_the_card_is_the_pace_half_alone(client):
    """FIRST DEGRADATION CASE, and the one the merge could most easily get
    wrong. The unplaced card used to vanish on its own by being absent from
    the rail; folded inside another card it has to vanish without leaving an
    orphan heading, an empty action, or the top half of a divider.

    The pace card itself is always present, so "nothing unplaced" is not
    "no card" — it is a card with one half.
    """
    user = _user(email="merge5@example.com", tracks=("ib",))
    Contact.all_objects.create(user=user, name="Ada Lovelace", region="us")
    from crm.today import _cockpit_context
    assert _cockpit_context(user)["unplaced_arrival_count"] == 0, (
        "precondition: nothing arrived unplaced this week"
    )

    card = _pace_card(_page(user))
    assert 'class="pace-figure"' in card, "the pace half is always present"
    assert "unplaced-card" not in card
    assert "Where do they sit?" not in card, "an orphan heading survived"
    assert "Place them" not in card, "an empty action survived"
    assert "btn-primary" not in card, (
        "the merged card kept a primary with nothing left to press it for"
    )


def test_the_goal_not_yet_hit_reads_the_same_in_both_halves(client):
    """SECOND DEGRADATION CASE. "Weekly goal hit." is one of three things the
    note says, and the merge must not have quietly made it the only one the
    layout was checked against — the card is at its tallest with an unmet
    goal AND an unplaced block, which is the state the founder's own account
    is in most weeks.

    The two halves are independent: a missed goal changes the note and
    nothing else, and the queue below it neither appears nor disappears
    because of it.
    """
    user = _merged_user(email="merge6@example.com")
    from crm.today import _cockpit_context
    ctx = _cockpit_context(user)
    assert not ctx["pace"]["hit"], "precondition: the goal is not hit"
    assert ctx["unplaced_arrival_count"], "precondition: something is unplaced"

    card = _pace_card(_page(user))
    assert "Weekly goal hit." not in card
    assert "more to go." in card
    # And the queue half is unchanged by the miss.
    assert "Where do they sit?" in card
    assert "Place them" in card


# ---------------------------------------------------------------------------
# 6. The rule this whole pass was working to, checked across the rail at once.
# ---------------------------------------------------------------------------
def test_no_rail_card_states_a_direction_of_time_inconsistently():
    """The rail's one shared reading rule, asserted where it is easiest to
    break: a bare "Nd" is elapsed, and a future distance says "in". Two cards
    print day counts and this is the seam between them."""
    user = _deadline_user()
    contact = Contact.all_objects.create(user=user, name="Ada Lovelace")
    Touch.all_objects.create(
        user=user, contact=contact, kind="outreach", channel="email",
        ts=timezone.now() - dt.timedelta(days=3),
    )
    html = _page(user)
    assert "in 58d" in _card(html, "Deadlines")
    activity = _card(html, "Recent Activity")
    assert ">3d<" in activity and "in 3d" not in activity
