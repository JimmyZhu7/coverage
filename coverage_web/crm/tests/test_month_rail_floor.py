"""The month rail's low end: one deadline must not look like none.

The rail's own comment states the contract — "one continuous strip of columns
whose HEIGHT is the count. Empty months collapse to a baseline tick ... a busy
month stands up and is found without reading a digit."

It stopped being true at the bottom of the scale. A single `min-height: 2px`
served both states, so once the phone breakpoint shortened the plot to 16px, a
month holding ONE deadline computed to 1.92px, lost to the floor, and rendered
as the identical 2px tick in the identical colour as the eight months holding
nothing. Two of the three non-empty months on a real rail were visually
silent; only the digit above them said otherwise, and the digit is the
fallback the height is supposed to make unnecessary.

These tests pin the split that fixes it: the markup has to TELL the two states
apart, and the stylesheet has to give them different floors. Both halves are
load-bearing — the class alone styles nothing, and the CSS alone has nothing
to hook onto.
"""

from __future__ import annotations

import re
from datetime import date, timedelta

import pytest
from django.utils import timezone

from crm.calendar_views import _month_rail
from directory.models import Firm, FirmDate

pytestmark = pytest.mark.django_db


@pytest.fixture
def user(django_user_model):
    return django_user_model.objects.create_user(email="rail@example.com", password="x" * 14)


def _deadline(user, firm_name: str, when: date):
    """One confirmed firm date, which is one of the layers the rail counts."""
    firm = Firm.objects.create(name=firm_name, slug=firm_name.lower().replace(" ", "-"))
    FirmDate.objects.create(
        firm=firm, cycle="2027", event_kind="applications_close",
        date=when, confidence=1.0,
    )
    return firm


def _rail_row(rail, y: int, m: int):
    return next(r for r in rail if r["y"] == y and r["m"] == m)


def test_a_one_deadline_month_is_not_marked_empty_in_the_markup(client, user):
    """The defect in one line: the template must be able to style "one" apart
    from "none". Before the fix there was no such distinction to make."""
    today = timezone.localdate()
    # A busy month and a one-deadline month, so bar_pct for the latter rounds
    # down to the low end of the scale where the old floor swallowed it.
    busy = today.replace(day=1)
    for i in range(8):
        _deadline(user, f"Busy {i}", busy + timedelta(days=i))
    lonely = (busy + timedelta(days=40)).replace(day=1)
    _deadline(user, "Lonely", lonely)

    client.force_login(user)
    html = client.get("/app/calendar/").content.decode()

    bars = re.findall(r'<span class="mrail-bar([^"]*)"[^>]*style="--h: (\d+)%', html)
    assert bars, "the rail rendered no bars at all"

    empties = [pct for cls, pct in bars if "is-empty" in cls]
    counted = [pct for cls, pct in bars if "is-empty" not in cls]

    # Every month with nothing carries the tick class; no month with something
    # does. That is the whole invariant the two floors hang off.
    assert empties, "no month was marked as the empty tick"
    assert counted, "no month was marked as carrying a count"
    assert set(empties) == {"0"}, f"a month with deadlines was marked empty: {empties}"
    assert "0" not in counted, "an empty month was left unmarked and will take the count floor"

    # And the specific regression: the lonely month's bar is a low percentage
    # AND is not the empty tick. Before the fix this bar existed but was
    # indistinguishable from its empty neighbours once rendered.
    rail = _month_rail(user, busy.year, busy.month, today)
    lonely_row = _rail_row(rail, lonely.year, lonely.month)
    assert lonely_row["count"] == 1
    assert 0 < lonely_row["bar_pct"] <= 25, (
        "expected the one-deadline month to sit at the bottom of the scale, "
        f"got {lonely_row['bar_pct']}%"
    )


def test_the_two_states_get_different_floors_at_every_plot_height(client, user):
    """A markup class that no rule reads would leave the pixels unchanged.

    Pinned as source assertions because the failure was a computed height: the
    count floor has to be an explicit floor (not a bare percentage, which is
    what collapsed), the empty tick has to override it, and the phone plot has
    to be tall enough that the smallest real step clears the tick.
    """
    client.force_login(user)
    html = client.get("/app/calendar/").content.decode()

    bar = re.search(r"\.mrail-bar \{(.*?)\}", html, re.S)
    assert bar, "the .mrail-bar rule is gone"
    body = bar.group(1)

    # The old shape. `min-height` applied one absolute floor to both states,
    # which is exactly how "one" and "none" became the same 2px.
    assert "min-height" not in body, (
        "a blanket min-height is back on .mrail-bar; it floors empty and "
        "non-empty months alike, which is the original defect"
    )
    assert re.search(r"height:\s*max\(var\(--h\),\s*(\d+)px\)", body), (
        "the count floor is gone; a bare `height: var(--h)` lets a "
        "one-deadline month round to sub-pixel again"
    )
    floor = int(re.search(r"height:\s*max\(var\(--h\),\s*(\d+)px\)", body).group(1))

    empty = re.search(r"\.mrail-bar\.is-empty \{(.*?)\}", html, re.S)
    assert empty, "nothing styles the empty tick, so the class does nothing"
    tick = int(re.search(r"height:\s*(\d+)px", empty.group(1)).group(1))

    assert tick < floor, (
        f"the empty tick ({tick}px) must sit below the count floor ({floor}px) "
        "or a month with one deadline reads as a month with none"
    )

    # The phone plot. 16px could not carry the range: the smallest real step
    # (1/8 of the busiest month) computed to 1.92px, under even the tick.
    phone = re.search(
        r"@media \(max-width: 560px\) \{(.*?)\n  \}", html, re.S)
    assert phone, "the phone breakpoint block moved; re-check the plot height"
    plot = int(re.search(r"\.mrail-plot \{ height: (\d+)px", phone.group(1)).group(1))
    # Two deadlines against a busiest of eight is 25% of the plot. That has to
    # land above the floor, or 1 and 2 flatten into each other on a phone.
    assert plot * 0.25 > floor, (
        f"a {plot}px phone plot puts a two-deadline month at {plot * 0.25}px, "
        f"at or under the {floor}px floor — 1 and 2 would render identically"
    )
