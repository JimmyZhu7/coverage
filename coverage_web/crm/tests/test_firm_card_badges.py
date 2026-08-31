"""What a firm card on the Network board is allowed to say.

Asked for directly, looking at J.P. Morgan's card: take the "48 Open" and
"1 Act Now" badges off, leave the progress bar and the reach-out suggestion.
The card measured 109px without a wrapped badge row and 134px with one; it
measures a flat 82px now.

Two of the three deleted badges lose nothing. Open roles are the
Opportunities feed's whole subject, and the Coverage Gaps strip at the top of
this same page still names the count for the four firms it ranks. Every
contact behind "Act Now" is listed BY NAME in the action lanes to the left of
these cards. "Sponsors" is a firm attribute with its own filter in the
directory.

The third one did carry something. "N Soon" counted roles whose deadline was
inside two weeks, and a deadline is precisely the signal a progress bar
cannot carry — a bar says how far along you are, never how long you have. So
it did not go quietly: it came back as the CONFIRMED close date itself, a red
mono countdown on the card's top line, the same one the gaps strip draws.
Rumored dates still say nothing, here as everywhere else.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse
from django.utils import timezone

from crm.models import Contact, UserFirm
from directory.models import Firm, FirmDate, Opportunity

User = get_user_model()

pytestmark = pytest.mark.django_db


def _tier_board(body: str) -> str:
    """Just the tier cards, not the board around them. The Coverage Gaps
    strip above it names the same firms and legitimately still carries an
    "N Open" badge. Starting at the first tier lane skips it and leaves only
    the cards."""
    start = body.index('<div class="tier-section"')
    return body[start : body.index("</section>", start)]


@pytest.fixture
def student(db):
    return User.objects.create_user(email="firmcard@example.com", password="x" * 14)


def _board(client, user) -> str:
    client.force_login(user)
    return _tier_board(client.get(reverse("crm:contact_list")).content.decode())


def _busy_firm(user, *, sponsors=True):
    """A firm wearing every badge the card used to carry: open roles, roles
    closing inside two weeks, a contact overdue for a touch, and sponsorship.
    This is the Morgan Stanley case the old CSS comment described — three
    badges, a wrapped badge row, a 134px card in a 109px row."""
    firm = Firm.objects.create(
        slug="busy-co", name="Busy Co", regions=["us"], sponsors=sponsors
    )
    UserFirm.all_objects.create(user=user, firm=firm, tier=1)
    today = timezone.localdate()
    for n in range(48):
        Opportunity.objects.create(
            firm=firm, url=f"https://x/{n}", title=f"Summer Analyst {n}",
            bucket="internship", status="open",
            deadline=today + timedelta(days=7) if n < 3 else None,
        )
    Contact.all_objects.create(user=user, name="Nick Tehle", firm=firm, warmth="cold")
    return firm


def test_a_firm_card_carries_no_count_badges(client, student):
    """"48 Open", "N Soon", "1 Act Now" — every badge that was a NUMBER is
    off the card. A status board should read as progress and a next step, not
    as a scoreboard the reader has to convert into one. (The one remaining
    pill, "Sponsors", is not a count — see the test below.)"""
    _busy_firm(student)
    board = _board(client, student)

    assert "Busy Co" in board, "the seeded firm is not on the tier board at all"
    for badge in ("pill fc-open", "pill fc-soon", "pill fc-act"):
        assert badge not in board, f"a firm card is wearing `{badge}` again"
    assert "Act Now" not in board


def test_the_sponsors_pill_is_not_a_count_and_stays(client, student):
    """The ask was about COUNTS. "Sponsors" is a yes/no about the student —
    whether this firm is open to them at all — and no other surface on this
    board answers it. Removing it as collateral would drop information, not
    noise. Shortened to "SP" on the card itself (a 10px chip has no room for
    a word), with the full claim carried in its own title attribute — see
    `test_the_sp_chip_explains_itself_without_a_legend` for that half."""
    _busy_firm(student, sponsors=True)
    board = _board(client, student)

    assert "pill fc-spon" in board and ">SP<" in board, (
        "the Sponsors pill went with the count badges. It is not a count, "
        "and it was not what was asked to be removed."
    )
    assert "Sponsors visas" in board, (
        "the card's chip carries no explanation of what SP means — it "
        "should say so in its title attribute."
    )


def test_a_firm_that_does_not_sponsor_shows_no_pill(client, student):
    """Absence is not a negative claim: a firm with nothing recorded and a
    firm that says no both simply say nothing here."""
    _busy_firm(student, sponsors=False)
    board = _board(client, student)

    assert "pill fc-spon" not in board


def test_the_progress_bar_and_the_suggestion_are_what_stayed(client, student):
    """The two things named as keepers. The suggestion is the card's only
    verb now, which is the whole point of the change."""
    _busy_firm(student)
    board = _board(client, student)

    assert 'class="firm-bar"' in board, "the warmth progress bar is gone"
    assert "Talk to Nick Tehle" in board, (
        "the reach-out suggestion is gone — with the badges removed it is the "
        "only thing on the card telling a student what to do next."
    )


def test_sockets_hide_until_a_firm_has_an_advocate(client, student):
    """Every firm on a 54-card board starts at zero advocates, so a pair of
    empty dot sockets was the one piece of decoration reported as identical
    on nearly every card. `_busy_firm` has one contact and no advocate yet —
    exactly the common case — so its card shows the bar (it has real warmth
    to plot) but not the socket widget. The number these sockets would have
    shown does not disappear: it moves into the bar's own tooltip instead,
    the one element a reader is already hovering to read this firm's
    coverage."""
    _busy_firm(student)
    board = _board(client, student)

    assert "adv-socket" not in board, (
        "a firm with zero advocates is still drawing empty dot sockets."
    )
    assert "0 of 2 advocates" in board, (
        "the advocate target disappeared instead of moving into the bar's "
        "own tooltip."
    )


def test_sockets_return_once_a_firm_has_an_advocate(client, student):
    """The widget earns its place back the moment it has a fill to show."""
    firm = _busy_firm(student)
    Contact.all_objects.create(user=student, name="Amy Advocate", firm=firm, warmth="advocate")
    board = _board(client, student)

    assert "adv-socket" in board and "is-filled" in board, (
        "a firm with a real advocate no longer shows the fill it earned."
    )


def test_a_firm_with_nobody_added_shows_no_bar_or_sockets(client, student):
    """The furniture question, answered: an empty-coverage firm does NOT
    render the same bar-and-sockets row a covered one does. A 0-of-everything
    bar next to two empty sockets said nothing an untouched firm's own
    "＋ Add a contact" line doesn't already say — zero contacts trivially
    means zero advocates too."""
    quiet = Firm.objects.create(slug="untouched-co", name="Untouched Co", regions=["us"])
    UserFirm.all_objects.create(user=student, firm=quiet, tier=1)
    board = _board(client, student)

    assert "Untouched Co" in board
    card_start = board.index("Untouched Co")
    card = board[card_start : card_start + 600]
    assert 'class="firm-bar"' not in card, "an untouched firm still draws an empty bar"
    assert "adv-socket" not in card, "an untouched firm still draws empty sockets"
    assert "＋ Add a contact" in card, "the one verb a bare card owes a student is gone"


def test_a_confirmed_close_date_survives_the_badges_it_came_in_with(client, student):
    """The one signal the progress bar structurally cannot carry. A bar says
    how far along you are; it has no way to say how long you have."""
    firm = _busy_firm(student)
    FirmDate.objects.create(
        firm=firm, event_kind="app_close", precision="day",
        date=timezone.localdate() + timedelta(days=6), confidence=1.0,
    )
    board = _board(client, student)

    assert 'class="gap-due-tag"' in board, (
        "a firm with a confirmed close date six days out shows no deadline. "
        "That urgency arrived on this card as the 'N Soon' badge and has to "
        "outlive it — nothing else on the card can say it."
    )
    assert ">6d<" in board
    assert "Applications close in 6 days (confirmed date)." in board


def test_the_countdown_says_today_rather_than_zero_days(client, student):
    """0 is the most urgent number this tag can hold and the one a plain
    `{% if %}` would drop. It says so in words."""
    firm = _busy_firm(student)
    FirmDate.objects.create(
        firm=firm, event_kind="app_close", precision="day",
        date=timezone.localdate(), confidence=1.0,
    )
    board = _board(client, student)

    assert ">today<" in board, (
        "a firm whose applications close TODAY shows no deadline at all — "
        "either the tag is gone or 0 days fell through a falsy check."
    )


def test_a_rumored_close_date_raises_no_alarm(client, student):
    """Same bar `cadence._closing_soon` and the gaps strip hold: only a
    CONFIRMED official date may claim a student's attention."""
    firm = _busy_firm(student)
    FirmDate.objects.create(
        firm=firm, event_kind="app_close", precision="day",
        date=timezone.localdate() + timedelta(days=6), confidence=0.3,
    )
    board = _board(client, student)

    assert "gap-due-tag" not in board, (
        "a rumored close date is drawing a red countdown on a firm card. "
        "Unconfirmed dates must never move a student."
    )


def test_a_distant_close_date_is_not_urgent(client, student):
    """Outside 30 days a deadline is a fact, not a warning, and the card has
    no room to hold facts. The Opportunities feed carries it."""
    firm = _busy_firm(student)
    FirmDate.objects.create(
        firm=firm, event_kind="app_close", precision="day",
        date=timezone.localdate() + timedelta(days=90), confidence=1.0,
    )
    board = _board(client, student)

    assert "gap-due-tag" not in board


def test_the_coverage_gaps_card_dropped_its_open_count_too(client, student):
    """"Take away the open xx for ALL of these" — said while pointing at a
    card in the Coverage Gaps strip, which is a different component from the
    tier board's firm card and had kept its badge through the first pass.
    Both surfaces now say the same nothing about inventory."""
    _busy_firm(student)
    client.force_login(student)
    body = client.get(reverse("crm:contact_list")).content.decode()
    # Sliced up to "Firm Coverage" — the next <h2> on the board — since the
    # "Contacts Needing Action" panel this used to end at is gone (it
    # duplicated Today's own queue; see crm/views.py::contact_list).
    # `start` bounds the second `.index()` too: the page's own inlined
    # <style> block names "Firm Coverage" in a CSS comment ABOVE this
    # section (see crm/_styles.html), and an unbounded search would find
    # that occurrence first and return an empty slice.
    strip_start = body.index('<h2 class="strip-title strip-title-lg" title="Ranked by exposure')
    strip = body[strip_start : body.index("Firm Coverage", strip_start)]

    assert "Busy Co" in strip, "the seeded firm is not on the gaps strip at all"
    assert "gap-badges" not in strip and "pill fc-open" not in strip, (
        "a Coverage Gaps card is wearing an open-role count again."
    )
    assert "48 open roles right now" in strip, (
        "the count is gone from the card's tooltip as well. It still breaks "
        "ties in the ranking, so the reader deserves to be able to find out "
        "why one identically-scored card sits above another."
    )


def test_the_removed_counts_still_decide_which_firm_reads_first(client, student):
    """The badges came off the cards; they did not come out of the view. A
    firm with people waiting on a reply still sorts above a quiet one inside
    its tier, which is where "act now" does its real work — the student's eye
    lands on the right card without the card having to shout a number."""
    quiet = Firm.objects.create(slug="quiet-co", name="Quiet Co", regions=["us"])
    busy = Firm.objects.create(slug="loud-co", name="Loud Co", regions=["us"])
    UserFirm.all_objects.create(user=student, firm=quiet, tier=1)
    UserFirm.all_objects.create(user=student, firm=busy, tier=1)
    Contact.all_objects.create(user=student, name="Ada Byron", firm=busy, warmth="cold")

    board = _board(client, student)
    assert board.index("Loud Co") < board.index("Quiet Co"), (
        "the firm with someone waiting no longer sorts first in its tier. "
        "With the 'Act Now' badge gone, this ordering is the only thing left "
        "pointing a student at the card that needs them."
    )


def test_the_sp_chip_explains_itself_without_a_legend(client, student):
    """The permanent `.net-legend` row that used to spell out "SP" (and four
    warmth dots most of the board's cards show no colour for) is gone —
    reported directly as decoration explaining a mapping most cards don't
    need explained. "SP" still has to be learnable without a prior hover,
    so the explanation now lives directly on the chip's own title attribute
    instead of one hop away in a legend."""
    _busy_firm(student, sponsors=True)
    board = _board(client, student)

    assert '<div class="net-legend"' not in board, "the legend row is back"
    assert "pill fc-spon" in board and ">SP<" in board
    assert "Sponsors visas" in board, (
        "the chip's own title attribute no longer explains what SP means, "
        "and there is no legend left to do it for it."
    )
