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
    """Just the tier board. The Coverage Gaps strip above it names the same
    firms and legitimately still carries an "N Open" badge, so a whole-body
    search would prove the opposite of what these tests claim."""
    start = body.index('<h2 class="strip-title" title="Tier drives your action')
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
    noise."""
    _busy_firm(student, sponsors=True)
    board = _board(client, student)

    assert "pill fc-spon" in board and "Sponsors" in board, (
        "the Sponsors pill went with the count badges. It is not a count, "
        "and it was not what was asked to be removed."
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
    assert "adv-socket" in board, "the advocate sockets are gone from the bar row"
    assert "Work Nick Tehle" in board, (
        "the reach-out suggestion is gone — with the badges removed it is the "
        "only thing on the card telling a student what to do next."
    )


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
    strip = body[
        body.index('<h2 class="strip-title" title="Ranked by exposure')
        : body.index("Contacts Needing Action")
    ]

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
