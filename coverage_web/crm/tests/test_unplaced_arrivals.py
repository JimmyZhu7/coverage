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

The rules pinned here:

  1. NOTHING GUESSES. No test in this file may pass by a region being written
     anywhere, and `test_the_card_never_writes_or_guesses_a_region` asserts
     that directly. The card reads two facts and two facts only: is `region`
     blank, and when was the row `created`.
  2. BOTH LIMITS, EACH WITH A DIFFERENT JOB. The seven-day window decides
     WHETHER the card exists (so a steady-state backlog never nags); the cap
     of five decides HOW MUCH it shows (so the ask stays a two-minute one).
  3. NO TOTAL, EVER. The count lives on the Unplaced tab. A rail card reading
     "71" is a rail card students learn to scroll past.
  4. ONE DESTINATION, AND IT ALREADY EXISTS. The card links to
     `?scope=unplaced` and rebuilds none of that tab's machinery. A second
     place to set a region would be the duplicate-widget bug this session
     removed three times over.
  5. COUNTS EQUAL WHAT RENDERS. Anyone the Network board takes off itself
     (campaign-hidden, recruitment-hidden, archived) is never named here
     either, or the card would send a student to a tab without them in it.
  6. THE HEADING NAMES THE MISSING FACT. Rewritten 2026-08-31 (the founder's
     own words: "this doesn't even make sense, make it more straightforward")
     from "Unplaced" / "Nobody has said where they sit" — jargon restating
     itself rather than naming what is actually missing — to "No market set"
     and a note that says plainly what happens without one. Every row is
     also a link to the same `?scope=unplaced` door "All" already opens,
     since a single row has nowhere more specific to go
     (`crm:contact_detail` carries no region control).

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
    UNPLACED_ARRIVAL_MAX, UNPLACED_ARRIVAL_WINDOW_DAYS, _cockpit_context,
    _unplaced_arrivals,
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


def _arrivals(user):
    return _cockpit_context(user)["unplaced_arrivals"]


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


# ---------------------------------------------------------------------------
# 1. When it speaks.
# ---------------------------------------------------------------------------
def test_a_contact_that_arrived_blank_this_week_is_named(client):
    """THE CASE THE CARD EXISTS FOR. Two declared markets, a firm running
    both desks, so `resolve_region` correctly writes nothing — and something
    finally says so on the day it happens rather than at 71."""
    user = _user()
    contact = _arrival(user, "Jude Yoon", _firm())

    assert contact.region == "", "precondition: the write path placed nobody"
    assert [a["name"] for a in _arrivals(user)] == ["Jude Yoon"]

    card = _card(_today(client, user))
    assert "Jude Yoon" in card
    assert "Citi" in card
    # The heading asks the question and the link carries the verb. Pinned
    # together because the card's whole job is to be answerable: a state
    # ("No market set") with no verb beside it was what it read as before.
    assert "Where do they sit?" in card
    assert "Place them" in card
    # And it must not name the founder's own two regions. This sentence
    # hardcoded "Hong Kong or US", which every student outside those two
    # markets would have read as a card about somewhere they do not recruit.
    assert "Hong Kong or US" not in card


def test_a_placed_contact_is_never_named(client):
    """A contact the write path COULD answer for is not a question. One
    declared market entails the region, so nothing is outstanding."""
    user = _user(regions=("us",))
    contact = _arrival(user, "Ada Lovelace", _firm())

    assert contact.region == "us", "precondition: the declaration placed them"
    assert _arrivals(user) is None
    assert not _card(_today(client, user))


def test_an_empty_week_renders_no_card_rather_than_an_empty_one(client):
    """Same "no targets means no card, not an empty card" convention every
    other rail card holds to. Nothing to ask means nothing on screen."""
    user = _user()
    _firm()
    assert _arrivals(user) is None
    assert "unplaced-card" not in _today(client, user)


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

    assert _arrivals(user) is None
    assert "unplaced-card" not in _today(client, user)


def test_a_contact_inside_the_window_still_counts_as_an_arrival():
    """The boundary is inclusive on the near side: a row created one day
    inside the window is this week's news."""
    user = _user()
    _arrival(user, "Just Inside", _firm(),
             days_ago=UNPLACED_ARRIVAL_WINDOW_DAYS - 1)
    assert [a["name"] for a in _arrivals(user)] == ["Just Inside"]


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
# 3. The cap: how much it shows, and the number it must never print.
# ---------------------------------------------------------------------------
def test_a_bulk_batch_is_capped_to_five_newest_first(client):
    """The founder's own arrival pattern was 44 rows in ONE capture batch,
    not a trickle, so the cap is the load-bearing limit here rather than the
    window. Newest first, because "new this week" is the claim the card
    makes."""
    user = _user()
    firm = _firm()
    for i in range(9):
        # Oldest first, so the last nine created are also the newest nine.
        _arrival(user, f"Person {i}", firm, days_ago=9 - i)

    arrivals = _arrivals(user)
    assert len(arrivals) == UNPLACED_ARRIVAL_MAX
    assert [a["name"] for a in arrivals] == [
        "Person 8", "Person 7", "Person 6", "Person 5", "Person 4",
    ]


def test_the_card_never_prints_a_total(client):
    """"71" is a number that makes a student close the tab. The card carries
    an "All" link instead of a count — the Deadlines card's own convention —
    so it never states a number, and therefore never states one that could
    disagree with what it actually rendered."""
    user = _user()
    firm = _firm()
    # Named without digits so the only numbers that could survive the tag
    # strip below are ones the card itself chose to print.
    for letter in "abcdefghijkl":
        _arrival(user, f"Person {letter.upper()}", firm)

    card = _card(_today(client, user))
    assert card, "precondition: the card rendered"
    text = re.sub(r"<[^>]+>", " ", card)
    assert not re.search(r"\d", text), (
        f"the card printed a number ({text.strip()!r}); it is a sample of "
        "five names and an 'All' link, and a count on it would either read "
        "12 (the number that makes students disengage) or 5 (a count "
        "disagreeing with the pool it does not describe)"
    )


def test_one_batch_names_the_same_five_on_every_render():
    """A capture batch writes dozens of rows inside the same second, so
    `created` alone is not a total order. Without the id tie-break the five
    names could reshuffle between two renders of the same unchanged page."""
    user = _user()
    firm = _firm()
    for i in range(8):
        _arrival(user, f"Person {i}", firm)  # all created "now"

    first = [a["id"] for a in _arrivals(user)]
    assert first == [a["id"] for a in _arrivals(user)]
    assert len(first) == UNPLACED_ARRIVAL_MAX


# ---------------------------------------------------------------------------
# 4. Counts equal what renders: nobody is named who is not on the tab.
# ---------------------------------------------------------------------------
def test_an_archived_contact_is_not_an_arrival(client):
    """Archived is off the board entirely, so naming one would send a
    student to a tab that does not contain them."""
    user = _user()
    _arrival(user, "Gone", _firm(), archived=True)
    assert _arrivals(user) is None


def test_a_campaign_hidden_person_is_not_named(client):
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

    assert _arrivals(user) is None
    client.force_login(user)
    resp = client.get(reverse("crm:contact_list"), {"scope": "unplaced"})
    assert resp.context["unplaced_total"] == 0, (
        "precondition: the tab does not list them either, which is exactly "
        "why the card must not"
    )


def test_a_recruitment_hidden_person_is_not_named(client):
    """Same rule, the other gate. The founder's 2026-08-25 answer that a
    person is not part of his recruiting takes them off the board, and a card
    that still asked where they sit would be arguing with him."""
    user = _user()
    firm = _firm()
    _arrival(user, "Not Recruiting", firm, recruitment_related=False)

    assert _arrivals(user) is None
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

    assert _arrivals(mine) is None
    assert [a["name"] for a in _arrivals(theirs)] == ["Their Contact"]


def test_a_contact_with_no_firm_still_gets_asked_about():
    """A hand-added row with typed firm text is exactly the kind most likely
    to be unplaced, and `_group_unplaced` already refuses to drop them. This
    card holds the same line, falling back to the typed text and then to a
    plain label rather than to silence."""
    user = _user()
    _arrival(user, "Typed Firm", None, firm_text="Some LLP")
    _arrival(user, "No Firm At All", None)

    by_name = {a["name"]: a["firm"] for a in _arrivals(user)}
    assert by_name["Typed Firm"] == "Some LLP"
    assert by_name["No Firm At All"] == ""


# ---------------------------------------------------------------------------
# 5. What it must never do: guess, write, or grow a second fix mechanism.
# ---------------------------------------------------------------------------
def test_the_card_never_writes_or_guesses_a_region(client):
    """THE RULE THIS WHOLE FEATURE EXISTS TO OBEY. Rendering Today is a read.
    No timezone offset, no signature city, no phone country code, no
    firm-wide default: the row is exactly as blank after the card names it as
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
    no verb and no form of its own — a second way to set a region is the
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


def test_every_row_is_a_link_to_the_same_place(client):
    """Not just the header's "All" — every row is its own affordance now,
    matching every other card in this rail (Schedule, Deadlines, Recent
    Activity all make their rows links). There is nowhere more specific to
    send one row than the door "All" already opens, since
    `crm:contact_detail` carries no region control of its own."""
    user = _user()
    firm = _firm()
    _arrival(user, "Jude Yoon", firm)
    _arrival(user, "Ada Lovelace", firm)

    card = _card(_today(client, user))
    href = f'href="{reverse("crm:contact_list")}?scope=unplaced"'
    # One in the heading ("All") plus one per row (two arrivals here).
    assert card.count(href) == 3, (
        f"expected the heading link plus one per row; card was:\n{card}"
    )
    assert card.count('class="unplaced-link"') == 2


def test_a_captured_local_part_name_renders_readably(client):
    """Gmail capture sometimes stores nothing better than the email's local
    part as `Contact.name` — verified on the founder's own board: 43 of 226
    non-archived contacts (19%), every one `source="capture"`, same as this
    card's own candidates. The raw form must never reach the page."""
    user = _user()
    contact = _arrival(user, "jude.yoon", _firm())
    assert contact.name == "jude.yoon", "precondition: stored exactly as captured"

    card = _card(_today(client, user))
    assert "Jude Yoon" in card
    assert "jude.yoon" not in card


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
        assert _unplaced_arrivals(user, contacts, timezone.now()) is None
    assert len(captured) == 0


def test_a_batch_of_arrivals_costs_a_bounded_number_of_queries():
    """The exclusion pair plus one firm-name read, and it does not grow with
    the size of the batch: the founder's 44-row day must not cost 44 lookups
    on a page that re-renders on every quick action."""
    user = _user()
    firm = _firm()
    for i in range(20):
        _arrival(user, f"Person {i}", firm)

    contacts = list(Contact.objects.for_user(user).filter(archived=False))
    with CaptureQueriesContext(connection) as captured:
        assert len(_unplaced_arrivals(user, contacts, timezone.now())) == 5
    # Pinned rather than bounded loosely: `campaigns.excluded_contact_ids`,
    # `recruitment.hidden_contact_ids` and one firm-name read, and it is the
    # constancy that matters — 20 rows and 44 rows must cost the same.
    n_at_20 = len(captured)
    for i in range(20, 44):
        _arrival(user, f"Person {i}", firm)
    contacts = list(Contact.objects.for_user(user).filter(archived=False))
    with CaptureQueriesContext(connection) as captured:
        _unplaced_arrivals(user, contacts, timezone.now())
    assert len(captured) == n_at_20, [q["sql"] for q in captured]
    assert n_at_20 <= 8, [q["sql"] for q in captured]


# ---------------------------------------------------------------------------
# 7. The card's own styling exists.
# ---------------------------------------------------------------------------
def test_every_class_the_card_renders_is_styled(client):
    """A rail card whose classes are not in the inlined `<style>` block
    renders as unstyled text in the middle of a designed page, and no
    template test would notice."""
    user = _user()
    _arrival(user, "Jude Yoon", _firm())
    card = _card(_today(client, user))
    assert card, "precondition: the card rendered"

    css = " ".join(STYLES.read_text().split())
    for cls in ("unplaced-note", "unplaced-list", "unplaced-row",
                "unplaced-link", "unplaced-name", "unplaced-firm"):
        assert f'class="{cls}"' in card or f'{cls}"' in card
        assert re.search(rf"\.{cls} \{{", css), (
            f".{cls} is rendered by the rail card but styled nowhere in "
            "crm/_styles.html"
        )


def test_a_long_name_gives_way_and_the_firm_does_not():
    """A truncated name is a nuisance; a truncated firm loses the one fact
    that makes the question answerable at all, so the firm holds its width."""
    css = " ".join(STYLES.read_text().split())
    name = re.search(r"\.unplaced-name \{(.*?)\}", css, re.S)
    firm = re.search(r"\.unplaced-firm \{(.*?)\}", css, re.S)
    assert name and firm
    assert "text-overflow: ellipsis" in name.group(1)
    assert "flex: none" in firm.group(1)
