"""The Deadlines rail, once duration facts are mixed into it.

THE ASK, AND WHAT IT TURNED INTO. The card showed a firm, an event and a
countdown; it was asked to also carry "how long the programme opened for",
still ranked by time priority. `directory.open_runs`' docstring records why
the literal version of that fact cannot ship — a 39-day observation window
can only pair the postings that closed inside it, so the open-to-close median
is a median of the FAST postings and nothing else, and on live data 77% of
still-open rows have already outlived it. What ships instead is elapsed
openness, which counts up from an open Coverage watched and predicts nothing.

The rules pinned here:

  1. TIME PRIORITY IS STILL DEADLINE PRIORITY. The card's ranking is the
     soonest confirmed date first, unchanged. A duration fact TRAILS a
     countdown that already earned its row; it never becomes a row of its
     own and never reorders one. An elapsed figure counts up from the past
     and a deadline counts down to the future — the only way to interleave
     them is to pretend "open 22 days" and "22 days away" are the same
     quantity, which would push a real deadline down the card.
  2. THE SAMPLE FLOOR IS AN EMPTY STATE. A firm below it loses the line
     entirely and keeps its row. No "no data yet", no hedge.
  3. NO FIRM ARRIVES ON THIS CARD BECAUSE OF A DURATION. The entry condition
     is still a confirmed date. A firm with open runs and no date belongs to
     the Opportunities feed, which lists every live posting.
  4. THE CARD MAKES NO FORECAST. Asserted against the vocabulary a forecast
     would need.
"""

from __future__ import annotations

import datetime as dt
import re
from pathlib import Path

import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse
from django.utils import timezone

from crm.today import _next_deadlines
from directory.models import Firm, FirmDate, Opportunity

pytestmark = pytest.mark.django_db

User = get_user_model()

STYLES = (
    Path(__file__).resolve().parents[2] / "templates" / "crm" / "_styles.html"
)


def _user(email="student@example.com"):
    return User.objects.create_user(email=email, password="pw12345!",
                                    regions=["us"])


def _firm(slug="gs", name="Goldman Sachs"):
    return Firm.objects.create(slug=slug, name=name)


def _date(firm, *, in_days, kind="applications_close", today=None):
    """A CONFIRMED firm date — `confidence=1.0` and a day-level precision,
    the two-part bar `confirmed_firm_dates` holds. Anything softer never
    reaches this card at all and so cannot carry an open-run line either."""
    today = today or timezone.localdate()
    return FirmDate.objects.create(
        # `sa2028`, not `2027`: `firm_dates_cycle_vocabulary` is a database
        # CHECK, so a fixture with a loose cycle string fails at insert.
        firm=firm, cycle="sa2028", event_kind=kind,
        date=today + dt.timedelta(days=in_days),
        # `day`, from `FIRM_DATE_PRECISIONS` — the vocabulary is a database
        # CHECK too, and `day` is the half of the confirmed bar that means
        # "this locates a real day" rather than a month-level estimate.
        confidence=1.0, precision="day",
    )


def _opp(firm, *, days_ago, status="open", bucket="internship", url=None,
         today=None):
    today = today or timezone.localdate()
    o = Opportunity.objects.create(
        firm=firm, title=f"Summer Analyst {days_ago}", bucket=bucket,
        status=status,
        url=url or f"https://example.test/{firm.slug}/{days_ago}/{status}",
    )
    stamp = timezone.make_aware(
        dt.datetime.combine(today - dt.timedelta(days=days_ago), dt.time(9, 0)),
        dt.timezone.utc,
    )
    Opportunity.objects.filter(pk=o.pk).update(first_seen=stamp)
    return o


def _watched_runs(firm, ages, today=None):
    """`ages` watched opens, plus the onboarding row that makes them
    watched — the oldest posting at a firm defines its onboarding day, so a
    fixture without one would have its own oldest role silently excluded."""
    _opp(firm, days_ago=max(ages) + 30, url=f"https://x.test/{firm.slug}/onb",
         today=today)
    for i, age in enumerate(ages):
        _opp(firm, days_ago=age, url=f"https://x.test/{firm.slug}/{i}",
             today=today)


def _today_page(client, user) -> str:
    client.force_login(user)
    res = client.get(reverse("crm:week"))
    assert res.status_code == 200
    return res.content.decode()


def _card(html: str) -> str:
    """Just the Deadlines rail card, so an assertion about it cannot be
    satisfied by the rest of a large page."""
    start = html.index('<h3 class="rail-title">Deadlines')
    # The card holds no nested <div>, so its own closing tag is the first one
    # after the heading.
    return html[start:html.index("</div>", start)]


# ---------------------------------------------------------------------------
# Rule 1 — the ranking does not move.
# ---------------------------------------------------------------------------

def test_duration_facts_do_not_reorder_the_card():
    """The firm with by far the longest open run sits LAST if its deadline is
    last. If an elapsed figure ever earned sort weight, the 40-day run would
    climb over the 2-day deadline and the card would stop being a deadline
    card."""
    user = _user()
    soon, later = _firm(), _firm(slug="ms", name="Morgan Stanley")
    _date(soon, in_days=2)
    _date(later, in_days=25)
    _watched_runs(soon, [1, 2, 3])
    _watched_runs(later, [40, 38, 35])

    rows = _next_deadlines(user, timezone.localdate())
    assert [r["firm"].slug for r in rows] == ["gs", "ms"]
    assert rows[0]["open_run"]["longest_days"] == 3
    assert rows[1]["open_run"]["longest_days"] == 40


def test_a_duration_never_becomes_a_row_of_its_own():
    """Rule 3, from the ranking side: the card's length is decided by
    confirmed dates and nothing else. A firm carrying a rich open run and no
    date must not appear, or the list would contain two kinds of item with no
    shared axis to rank them on."""
    user = _user()
    dated = _firm()
    _date(dated, in_days=5)
    undated = _firm(slug="jpm", name="JPMorgan")
    _watched_runs(undated, [30, 25, 20, 15])

    rows = _next_deadlines(user, timezone.localdate())
    assert [r["firm"].slug for r in rows] == ["gs"]


def test_the_row_still_carries_its_countdown_unchanged():
    """The trailing fact is additive. Everything the card said before it must
    still be there and still say the same thing."""
    user = _user()
    firm = _firm()
    _date(firm, in_days=3)
    _watched_runs(firm, [22, 9, 3])

    row = _next_deadlines(user, timezone.localdate())[0]
    assert row["when"] == "3d"
    assert row["days"] == 3
    assert row["urgent"] is True
    assert row["open_run"] == {"count": 3, "longest_days": 22}


# ---------------------------------------------------------------------------
# Rule 2 — the floor is an empty state.
# ---------------------------------------------------------------------------

def test_a_firm_below_the_floor_keeps_its_row_and_loses_only_the_line(client):
    """The silence `_cycle_observed` keeps for a below-threshold window, in
    the rail. Two watched postings is not a programme, and the honest
    response is nothing at all rather than a hedged sentence."""
    user = _user()
    firm = _firm()
    _date(firm, in_days=4)
    _watched_runs(firm, [12, 6])

    row = _next_deadlines(user, timezone.localdate())[0]
    assert row["open_run"] is None

    card = _card(_today_page(client, user))
    assert "Goldman Sachs" in card
    assert "4d" in card
    assert "open," not in card
    for hedge in ("no data", "not enough", "unknown", "yet to"):
        assert hedge not in card.lower(), hedge


def test_onboarding_batch_postings_do_not_unlock_the_line(client):
    """A firm whose entire visible board arrived on the day Coverage started
    watching has watched no opens at all, however many rows it shows. Those
    rows must not count toward the floor — that would use the excluded
    evidence to unlock the very line excluding it was meant to withhold."""
    user = _user()
    firm = _firm()
    _date(firm, in_days=4)
    today = timezone.localdate()
    for i in range(6):
        _opp(firm, days_ago=30, url=f"https://x.test/onb{i}", today=today)

    assert _next_deadlines(user, today)[0]["open_run"] is None
    assert "open," not in _card(_today_page(client, user))


# ---------------------------------------------------------------------------
# What the card actually renders.
# ---------------------------------------------------------------------------

def test_the_card_renders_the_census_and_the_longest_run(client):
    user = _user()
    firm = _firm()
    _date(firm, in_days=6)
    _watched_runs(firm, [22, 9, 3])

    card = _card(_today_page(client, user))
    assert "3 open, longest 22d" in card


def test_an_all_same_day_firm_reads_as_today_not_as_zero_days(client):
    """`longest_days == 0` is a real measurement, but "longest 0d" is not a
    sentence. When the maximum is zero every one of them is zero, which the
    card says in words."""
    user = _user()
    firm = _firm()
    _date(firm, in_days=6)
    today = timezone.localdate()
    _opp(firm, days_ago=30, url="https://x.test/onb", today=today)
    for i in range(3):
        _opp(firm, days_ago=0, url=f"https://x.test/new{i}", today=today)

    card = _card(_today_page(client, user))
    assert "3 open, all today" in card
    assert "0d" not in card


def test_the_open_run_line_is_styled_as_its_own_quiet_line():
    """A third clause inline on `.activity-text` would wrap mid-phrase in a
    rail column already carrying a firm name and an event label. Pinned
    because the layout claim is the reason the copy is this short."""
    css = STYLES.read_text()
    rule = re.search(r"\.activity-run\s*\{([^}]*)\}", css)
    assert rule, ".activity-run must be styled explicitly"
    body = rule.group(1)
    assert "display: block" in body
    assert "--ink-3" in body


# ---------------------------------------------------------------------------
# Rule 4 — no forecast.
# ---------------------------------------------------------------------------

def test_the_card_makes_no_forecast(client):
    """The claim the censoring measurement rules out. This card sits next to
    a real countdown, which is the single most tempting place in the product
    to print a second, invented one."""
    user = _user()
    firm = _firm()
    _date(firm, in_days=6)
    _watched_runs(firm, [22, 9, 3])

    card = _card(_today_page(client, user)).lower()
    for forecast in ("typically", "usually", "on average", "stays open",
                     "expected", "estimate", "likely to close", "about "):
        assert forecast not in card, forecast
