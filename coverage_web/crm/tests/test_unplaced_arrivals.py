"""The one proactive word Today says about contacts nobody has placed.

THE MEASURED BUG, and it is a visibility bug rather than a resolution one.
`Contact.resolve_region` refuses to guess a region it cannot entail, and that
refusal is backed by measurement (174 real inbound messages: 0% Date-header
timezone coverage on corporate senders, signature cities and phone country
codes that name a firm-wide office rather than a desk). A student who declares
BOTH us and hk, holding contacts at firms that run both desks, reaches the last
tier of that chain and the rows stay blank. Working as designed.

What was NOT designed is that nobody found out. On the founder's own account,
2026-08-31: 71 blank contacts, every one `source="capture"`, 44 arriving in a
single Gmail-capture batch that day and 22 in another four days earlier. The
entire nag budget for the unplaced pool was one passive caveat line on a page
you have to navigate to, so the number grew to triple digits in silence.

WHAT THE CARD IS, AFTER 2026-09-02 (the founder's own words: "make Place them
into a button, don't show people's names, just show how many people need to be
placed"). A question, a count and a button. It named up to five people before,
under a note, with the verb as a text link in the heading's corner; the names
and the cap that held them are gone, and this file's rules changed with them.

The rules pinned here:

  1. NOTHING GUESSES. No test in this file may pass by a region being written
     anywhere, and `test_the_card_never_writes_or_guesses_a_region` asserts
     that directly. The card reads two facts and two facts only: is `region`
     blank, and when was the row `created`.
  2. ONE LIMIT, NOT TWO. The seven-day window decides WHETHER the card exists,
     so a steady-state backlog never nags. The cap of five that decided how
     many names to show went with the names: a count has one row whatever it
     counts.
  3. THE NUMBER IS THE WEEK'S, AND THE FACE SAYS SO. The card counts arrivals
     inside the window, not the standing pool, so on a board with a backlog it
     reads smaller than the tab it opens. That is not a disagreement — the two
     count different sets and each says which — and it is the answer to the
     old "a rail card reading 71 is one students scroll past" objection: a
     number that is honestly small, rather than no number.
  4. NO NAMES, EVER. Nobody is named on this card. A roster it could not act
     on was five names of a pile the reader could not size.
  5. ONE DESTINATION, AND IT ALREADY EXISTS. The card links to
     `?scope=unplaced` and rebuilds none of that tab's machinery. A second
     place to set a region would be the duplicate-widget bug this session
     removed three times over.
  6. COUNTS EQUAL WHAT THE TAB HOLDS. Anyone the Network board takes off
     itself (campaign-hidden, recruitment-hidden, archived) is never counted
     here either, or the card would send a student to a tab without them.
  7. THE HEADING AND THE VERB ARE THE FOUNDER'S OWN, from 2026-08-31 ("this
     doesn't even make sense, make it more straightforward"): "Where do they
     sit?" and "Place them". Every pass since has changed only how they are
     presented, never the words.

Its own module rather than an append to `test_region_resolution.py`: that file
is about the WRITE path (what a stated fact entails), and this one is about a
read-only surface that deliberately entails nothing.
"""

from __future__ import annotations

import re
from datetime import timedelta
from pathlib import Path

import pytest
from django.contrib.auth import get_user_model
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from crm.models import Campaign, CampaignContact, Contact, UserFirm
from crm.today import (
    UNPLACED_ARRIVAL_WINDOW_DAYS, _cockpit_context, _unplaced_arrival_count,
)
from directory.models import Firm

pytestmark = pytest.mark.django_db

User = get_user_model()

STYLES = (
    Path(__file__).resolve().parents[2] / "templates" / "crm" / "_styles.html"
)


def _user(email="student@example.com", regions=("hk", "us")):
    """The founder's own shape: two declared markets, which is exactly the
    case `resolve_region` refuses to answer at a two-desk firm."""
    return User.objects.create_user(
        email=email, password="pw12345!", regions=list(regions)
    )


def _firm(name="Citi", slug="citi", regions=("hk", "us")):
    return Firm.objects.create(name=name, slug=slug, regions=list(regions))


def _arrival(user, name="Jude Yoon", firm=None, *, days_ago=0, **kw):
    """A contact that lands blank, the way Gmail capture lands them.

    `created` is `auto_now_add`, so backdating is an `.update()` after the
    fact — the same thing real time passing does to the column.
    """
    c = Contact.all_objects.create(
        user=user, name=name, firm=firm, source="capture", **kw
    )
    if days_ago:
        Contact.all_objects.filter(pk=c.pk).update(
            created=timezone.now() - timedelta(days=days_ago)
        )
        c.refresh_from_db()
    return c


def _count(user) -> int:
    return _cockpit_context(user)["unplaced_arrival_count"]


def _today(client, user) -> str:
    client.force_login(user)
    return client.get(reverse("crm:week")).content.decode()


def _card(body: str) -> str:
    """Just the rail card, so an assertion about it cannot accidentally be
    satisfied by the rest of a 200KB page."""
    # `[^"]*`: the rail card also wears the shared panel primitive since
    # 2026-09-02 (D-13). Still anchored on `unplaced-card`, so it still
    # cannot match any other rail card on the page.
    match = re.search(
        r'<div class="rail-card unplaced-card[^"]*">(.*?)</div>', body, re.S
    )
    return match.group(1) if match else ""


def _face(card: str) -> str:
    """The card's plain text: tags stripped, so nothing in a `title=` or an
    `href` can satisfy an assertion about what a student actually reads."""
    return " ".join(re.sub(r"<[^>]+>", " ", card).split())


# ---------------------------------------------------------------------------
# 1. When it speaks.
# ---------------------------------------------------------------------------
def test_a_contact_that_arrived_blank_this_week_is_counted(client):
    """THE CASE THE CARD EXISTS FOR. Two declared markets, a firm running
    both desks, so `resolve_region` correctly writes nothing — and something
    finally says so on the day it happens rather than at 71.

    Was `..._is_named`, and the name is the part that changed: the card
    reports how many arrived, not who they are.
    """
    user = _user()
    contact = _arrival(user, "Jude Yoon", _firm())

    assert contact.region == "", "precondition: the write path placed nobody"
    assert _count(user) == 1

    card = _card(_today(client, user))
    # The question, its size and its verb, which is the whole card. Pinned
    # together because the card's job is to be answerable: a state with no
    # verb beside it was what it read as two passes ago.
    assert "Where do they sit?" in card
    assert "1 new this week, no market set." in _face(card)
    assert "Place them" in card
    # Neither the person nor their firm reaches the page.
    assert "Jude Yoon" not in card
    assert "Citi" not in card
    # And it must not name the founder's own two regions. The old note
    # hardcoded "Hong Kong or US", which every student outside those two
    # markets would have read as a card about somewhere they do not recruit.
    assert "Hong Kong or US" not in card


def test_a_placed_contact_is_never_counted(client):
    """A contact the write path COULD answer for is not a question. One
    declared market entails the region, so nothing is outstanding."""
    user = _user(regions=("us",))
    contact = _arrival(user, "Ada Lovelace", _firm())

    assert contact.region == "us", "precondition: the declaration placed them"
    assert _count(user) == 0
    assert not _card(_today(client, user))


def test_an_empty_week_renders_no_card_rather_than_an_empty_one(client):
    """Same "no targets means no card, not an empty card" convention every
    other rail card holds to. Nothing to ask means nothing on screen.

    Anchored on the rendered element rather than on the bare class name: the
    card has a `.unplaced-card` CSS rule as of 2026-09-02, and the inlined
    `<style>` block puts that selector in the page body whether the card
    renders or not. A bare `"unplaced-card" not in body` would fail on the
    stylesheet and pass on nothing.
    """
    user = _user()
    _firm()
    assert _count(user) == 0
    assert not _card(_today(client, user))


# ---------------------------------------------------------------------------
# 2. The window: whether the card exists at all.
# ---------------------------------------------------------------------------
def test_the_card_expires_instead_of_nagging_forever(client):
    """THE HALF THAT PROTECTS THE STEADY STATE. `crm.views.contact_list` has
    always promised the unplaced pool is not an interruption, and a card that
    ran until a 71-row backlog was cleared would break that promise on the
    exact account it was written for. Past the window the prompt stops, the
    same rule `crm.debrief.DEBRIEF_EXPIRES_AFTER_DAYS` states for itself."""
    user = _user()
    firm = _firm()
    _arrival(user, "Old Backlog", firm, days_ago=UNPLACED_ARRIVAL_WINDOW_DAYS + 1)

    assert _count(user) == 0
    # The element, not the class name: see the note in
    # `test_an_empty_week_renders_no_card_rather_than_an_empty_one`.
    assert not _card(_today(client, user))


def test_a_contact_inside_the_window_still_counts_as_an_arrival():
    """The boundary is inclusive on the near side: a row created one day
    inside the window is this week's news."""
    user = _user()
    _arrival(user, "Just Inside", _firm(),
             days_ago=UNPLACED_ARRIVAL_WINDOW_DAYS - 1)
    assert _count(user) == 1


def test_an_aged_out_backlog_stays_answerable_on_the_tab(client):
    """Expiring is not hiding. The card goes quiet; the standing pool, its
    count and its grouped-by-firm answer are all still on the Network page,
    which is the always-available fallback the card was never replacing."""
    user = _user()
    firm = _firm()
    _arrival(user, "Old Backlog", firm, days_ago=UNPLACED_ARRIVAL_WINDOW_DAYS + 1)

    client.force_login(user)
    resp = client.get(reverse("crm:contact_list"), {"scope": "unplaced"})
    assert resp.context["unplaced_total"] == 1
    assert [(g["label"], g["count"]) for g in resp.context["unplaced_groups"]] == [
        ("Citi", 1)
    ]


# ---------------------------------------------------------------------------
# 3. The number: the whole week, and only the week.
# ---------------------------------------------------------------------------
def test_a_bulk_batch_is_counted_in_full(client):
    """REWRITTEN 2026-09-02; its premise was the cap. It read
    `test_a_bulk_batch_is_capped_to_five_newest_first` and pinned that nine
    arrivals produced the five newest names, because the card showed a sample
    and the sample had to be the fresh end of it.

    The founder cut the names and asked for the number instead, so there is
    no sample left to cap and no order left to fix: nine arrivals are nine.
    The founder's own pattern is what makes this the load-bearing case — 44
    rows landed in ONE capture batch, not a trickle, so the batch is exactly
    when a student most needs to know how big the ask is. One batch, one
    instant, which is also why nothing here is backdated: the old version
    staggered these across nine days to prove the five it kept were the
    newest, and three of the nine fell outside the window unnoticed because
    the cap hid them.
    """
    user = _user()
    firm = _firm()
    for i in range(9):
        _arrival(user, f"Person {i}", firm)

    assert _count(user) == 9
    assert "9 new this week, no market set." in _face(_card(_today(client, user)))


def test_the_card_counts_the_week_and_the_tab_counts_the_pool(client):
    """REWRITTEN 2026-09-02; its premise was reversed. It read
    `test_the_card_never_prints_a_total` and asserted no digit reached the
    card at all, on the argument that "71" is a number that makes a student
    close the tab and a smaller one would disagree with the pool it does not
    describe.

    The founder asked for the count. The disagreement the old test feared is
    answered by the face rather than by silence: the card says "new this
    week", so its number is the window's, while the Unplaced tab counts every
    region-less contact under its own heading. Both are true at once and this
    test pins them being different on purpose.
    """
    user = _user()
    firm = _firm()
    for letter in "abcdefghijkl":  # 12 inside the window
        _arrival(user, f"Person {letter.upper()}", firm)
    for i in range(3):  # 3 aged out of it
        _arrival(user, f"Old {i}", firm,
                 days_ago=UNPLACED_ARRIVAL_WINDOW_DAYS + 1 + i)

    assert _count(user) == 12
    assert "12 new this week, no market set." in _face(_card(_today(client, user)))

    client.force_login(user)
    resp = client.get(reverse("crm:contact_list"), {"scope": "unplaced"})
    assert resp.context["unplaced_total"] == 15, (
        "the tab counts the standing pool; the card counts this week, and "
        "the card's own face is what keeps the two from reading as a bug"
    )


def test_the_count_is_the_same_on_every_render():
    """REWRITTEN 2026-09-02. It read `test_one_batch_names_the_same_five_on_
    every_render` and existed because a capture batch writes dozens of rows
    inside the same second: `created` alone is not a total order, so without
    an id tie-break the five names could reshuffle between two renders of an
    unchanged page.

    There is no sample to reshuffle now, and the property that mattered — an
    unchanged page renders the same card twice — is what this pins instead.
    """
    user = _user()
    firm = _firm()
    for i in range(8):
        _arrival(user, f"Person {i}", firm)  # all created "now"

    assert _count(user) == 8 == _count(user)


# ---------------------------------------------------------------------------
# 4. Counts equal what the tab holds.
# ---------------------------------------------------------------------------
def test_an_archived_contact_is_not_an_arrival(client):
    """Archived is off the board entirely, so counting one would send a
    student to a tab that does not contain them."""
    user = _user()
    _arrival(user, "Gone", _firm(), archived=True)
    assert _count(user) == 0


def test_a_campaign_hidden_person_is_not_counted(client):
    """`crm.views.contact_list` removes campaign-excluded people BEFORE it
    computes the unplaced pool. The card applies the identical exclusion, so
    the two can never disagree about who is waiting to be placed."""
    user = _user()
    firm = _firm()
    contact = _arrival(user, "Club Panelist", firm)
    campaign = Campaign.all_objects.create(
        user=user, signature="club-blast", kind=Campaign.KIND_OTHER,
        first_sent=timezone.now(), last_sent=timezone.now(),
    )
    CampaignContact.all_objects.create(
        user=user, campaign=campaign, contact=contact, originates=True,
        sent_at=timezone.now(),
    )

    assert _count(user) == 0
    client.force_login(user)
    resp = client.get(reverse("crm:contact_list"), {"scope": "unplaced"})
    assert resp.context["unplaced_total"] == 0, (
        "precondition: the tab does not list them either, which is exactly "
        "why the card must not count them"
    )


def test_a_recruitment_hidden_person_is_not_counted(client):
    """Same rule, the other gate. The founder's 2026-08-25 answer that a
    person is not part of his recruiting takes them off the board, and a card
    that still asked where they sit would be arguing with him."""
    user = _user()
    firm = _firm()
    _arrival(user, "Not Recruiting", firm, recruitment_related=False)

    assert _count(user) == 0
    client.force_login(user)
    resp = client.get(reverse("crm:contact_list"), {"scope": "unplaced"})
    assert resp.context["unplaced_total"] == 0


def test_another_tenants_arrival_never_appears(client):
    """Private zone. `_build_actions` hands this function a `.for_user`
    list, and the assertion is here so a future refactor to a direct query
    cannot quietly drop the scope."""
    mine = _user("mine@example.com")
    theirs = _user("theirs@example.com")
    firm = _firm()
    _arrival(theirs, "Their Contact", firm)

    assert _count(mine) == 0
    assert _count(theirs) == 1


def test_a_contact_with_no_firm_still_gets_asked_about():
    """REWRITTEN 2026-09-02; the card no longer prints a firm. The old test
    pinned the fallback chain the row used to render (`firm` name, then typed
    `firm_text`, then a plain label), because a hand-added row with typed firm
    text is exactly the kind most likely to be unplaced.

    The fact underneath it survives and is what is pinned now: a missing firm
    was never a reason to leave somebody out of the ask. All three shapes
    count.
    """
    user = _user()
    _arrival(user, "Typed Firm", None, firm_text="Some LLP")
    _arrival(user, "No Firm At All", None)
    _arrival(user, "Real Firm", _firm())

    assert _count(user) == 3


# ---------------------------------------------------------------------------
# 5. What it must never do: guess, write, name, or grow a second fix.
# ---------------------------------------------------------------------------
def test_the_card_never_writes_or_guesses_a_region(client):
    """THE RULE THIS WHOLE FEATURE EXISTS TO OBEY. Rendering Today is a read.
    No timezone offset, no signature city, no phone country code, no
    firm-wide default: the row is exactly as blank after the card counts it as
    it was before."""
    user = _user()
    _arrival(user, "Jude Yoon", _firm())

    _today(client, user)

    row = Contact.all_objects.get(user=user)
    assert row.region == ""
    assert row.region_source == ""


def test_the_card_sends_them_to_the_tool_that_already_works(client):
    """One destination, and it is the Unplaced tab: grouped by firm,
    select-all per group, three region verbs. The card carries no checkbox,
    no verb of its own and no form — a second way to set a region is the
    duplicate-widget bug this session removed three times over."""
    user = _user()
    _arrival(user, "Jude Yoon", _firm())

    card = _card(_today(client, user))
    assert "?scope=unplaced" in card
    for reinvented in ("<form", "<input", "<button", "region_us", "region_hk"):
        assert reinvented not in card, (
            f"the rail card grew its own {reinvented}; the placement UI "
            "lives on the Unplaced tab and must stay there"
        )


def test_the_card_has_one_link_and_it_is_the_shared_button(client):
    """REWRITTEN 2026-09-02. It read `test_every_row_is_a_link_to_the_same_
    place` and counted the heading's link plus one per named row, because the
    rows had become links to match every other card in this rail.

    There are no rows. The founder asked for the verb to become a button
    ("make Place them into a button"), so the card has exactly one clickable
    thing and it is the shared control — `btn btn-primary`, not a new shape
    and not a `.rail-more` text link, which is the treatment reserved for the
    "see all" on cards that are already showing you the thing.
    """
    user = _user()
    firm = _firm()
    _arrival(user, "Jude Yoon", firm)
    _arrival(user, "Ada Lovelace", firm)

    card = _card(_today(client, user))
    href = f'href="{reverse("crm:contact_list")}?scope=unplaced"'
    assert card.count(href) == 1, f"expected one link; card was:\n{card}"
    assert re.search(r'class="btn btn-primary[^"]*"[^>]*>Place them</a>', card), (
        "the verb is not the shared button any more"
    )
    assert "rail-more" not in card, (
        "the corner text link is back; it was the presentation the founder "
        "asked to replace"
    )


def test_no_name_reaches_the_card(client):
    """REWRITTEN 2026-09-02. It read `test_a_captured_local_part_name_renders_
    readably` and pinned that "jude.yoon" — the raw local part Gmail capture
    stores as `Contact.name` for 19% of the founder's board — was title-cased
    before it reached the page.

    The founder's answer was to stop printing names at all, so the stronger
    version of the same guard is that NEITHER form reaches it. The
    title-casing rule itself is not lost: `smart_person_name` is pinned by
    its own tests and still runs everywhere a name does render.
    """
    user = _user()
    contact = _arrival(user, "jude.yoon", _firm())
    assert contact.name == "jude.yoon", "precondition: stored exactly as captured"

    card = _card(_today(client, user))
    assert card, "precondition: the card rendered"
    assert "jude.yoon" not in card
    assert "Jude Yoon" not in card


def test_the_passive_caveat_on_contacts_is_untouched(client):
    """The card is the EARLY word and it expires; this sentence is the
    always-available fallback and it does not. It also answers a different
    question ("how many rows in THIS region tab are here on a guess"), so
    the arrival of a proactive surface is no reason to reword it."""
    user = _user()
    _arrival(user, "Jude Yoon", _firm())

    client.force_login(user)
    body = client.get(
        reverse("crm:contact_list"), {"scope": "us"}
    ).content.decode()
    assert "1 of these have no region set. Shown on a guess." in body
    assert "Place them" in body
    assert "?scope=unplaced" in body


# ---------------------------------------------------------------------------
# 6. Cost.
# ---------------------------------------------------------------------------
def test_an_ordinary_day_pays_nothing_for_this_card():
    """Same cost discipline `_starter_seeds` and `_next_wave` hold to: every
    query is gated behind a non-empty candidate set, and the candidate filter
    runs over the contact list `_build_actions` already loaded. Most days
    there are no fresh unplaced rows, and those days must cost zero."""
    user = _user(regions=("us",))
    placed = _arrival(user, "Ada Lovelace", _firm())
    assert placed.region == "us", "precondition: nothing is unplaced"

    contacts = list(Contact.objects.for_user(user).filter(archived=False))
    with CaptureQueriesContext(connection) as captured:
        assert _unplaced_arrival_count(user, contacts, timezone.now()) == 0
    assert len(captured) == 0


def test_a_batch_of_arrivals_costs_a_bounded_number_of_queries():
    """The exclusion pair, and it does not grow with the size of the batch:
    the founder's 44-row day must not cost 44 lookups on a page that
    re-renders on every quick action.

    The firm-name read this used to allow went with the names (2026-09-02):
    the card prints no firm, so nothing here loads one.
    """
    user = _user()
    firm = _firm()
    for i in range(20):
        _arrival(user, f"Person {i}", firm)

    contacts = list(Contact.objects.for_user(user).filter(archived=False))
    with CaptureQueriesContext(connection) as captured:
        assert _unplaced_arrival_count(user, contacts, timezone.now()) == 20
    # Pinned rather than bounded loosely: `campaigns.excluded_contact_ids` and
    # `recruitment.hidden_contact_ids`, and it is the constancy that matters —
    # 20 rows and 44 rows must cost the same.
    n_at_20 = len(captured)
    for i in range(20, 44):
        _arrival(user, f"Person {i}", firm)
    contacts = list(Contact.objects.for_user(user).filter(archived=False))
    with CaptureQueriesContext(connection) as captured:
        assert _unplaced_arrival_count(user, contacts, timezone.now()) == 44
    assert len(captured) == n_at_20, [q["sql"] for q in captured]
    assert n_at_20 <= 7, [q["sql"] for q in captured]


# ---------------------------------------------------------------------------
# 7. The card's own styling exists.
# ---------------------------------------------------------------------------
def test_every_class_the_card_renders_is_styled(client):
    """A rail card whose classes are not in the inlined `<style>` block
    renders as unstyled text in the middle of a designed page, and no
    template test would notice.

    The class list is the 2026-09-02 one: the roster classes
    (`unplaced-list`, `-row`, `-link`, `-name`, `-firm`) and the note above
    them were retired with the markup that used them.
    """
    user = _user()
    _arrival(user, "Jude Yoon", _firm())
    card = _card(_today(client, user))
    assert card, "precondition: the card rendered"

    css = " ".join(STYLES.read_text().split())
    for cls in ("unplaced-count", "unplaced-n", "unplaced-act"):
        assert f'class="{cls}"' in card or f'{cls}"' in card
        assert re.search(rf"\.{cls} \{{", css), (
            f".{cls} is rendered by the rail card but styled nowhere in "
            "crm/_styles.html"
        )
    assert re.search(r"\.unplaced-card \{", css), (
        "the card itself is unstyled, so its three elements have no column "
        "to sit in"
    )


def test_the_count_leads_and_the_verb_is_not_redefined():
    """REWRITTEN 2026-09-02. It read `test_a_long_name_gives_way_and_the_firm_
    does_not` and pinned the ellipsis on `.unplaced-name` against `flex: none`
    on `.unplaced-firm`, because a truncated firm lost the one fact that made
    the question answerable. Both classes are gone with the names.

    What replaces it is the shape the card has now. The figure is the token
    whose own comment names this job ("a large number inside a panel or a
    row") and it is set on the numeral alone, so the caption beside it stays
    small print. And the button takes NOTHING from this stylesheet except
    where it sits: shape, padding, colour, min-height and every state are
    `.btn`'s, which is the control-shape rule coverage.css §6 writes down.
    """
    css = " ".join(STYLES.read_text().split())
    figure = re.search(r"\.unplaced-n \{(.*?)\}", css, re.S).group(1)
    assert "font-size: var(--fs-figure)" in figure
    caption = re.search(r"\.unplaced-count \{(.*?)\}", css, re.S).group(1)
    assert "font-size: var(--fs-micro)" in caption, (
        "the caption grew to the figure's size, so the number stopped leading"
    )
    act = re.search(r"\.unplaced-act \{(.*?)\}", css, re.S).group(1)
    for redefinition in ("background", "border", "padding", "font-size",
                         "border-radius", "min-height"):
        assert redefinition not in act, (
            f"`.unplaced-act` sets {redefinition}, which is `.btn`'s job; a "
            "rail card must not grow a second button shape"
        )
