"""The Cycle Dates timeline on a firm page: what it may claim, and how.

Two defects live here, both found by reading the RENDERED page rather than the
helper that feeds it — the market being named twice, and:

`FirmDate.source_url` is a URLField that nothing validates, so 26 of the 39
live rows hold a provenance token ("seed:historical-pattern", "seed:demo")
rather than a citation. The template's only gate was truthiness, so every
token became an `<a>` styled exactly like a working link — on /firms/gs/,
four identical SOURCE pills of which two went nowhere.

These assert against RENDERED HTML, because the bug lives in the seam between
the view dict and the template, which a helper-only test walks straight past.
"""

from __future__ import annotations

import datetime as dt

import pytest

from django.db import transaction
from django.db.utils import IntegrityError
from django.utils import timezone

from directory.models import Firm, FirmDate

pytestmark = pytest.mark.django_db


def _firm(slug="gs", name="Goldman Sachs"):
    return Firm.objects.create(slug=slug, name=name)


def _date(firm, **kw):
    # Since migration 0014 `cycle` holds a season+year slug and the desk lives
    # in its own `track` column; `firm_dates_cycle_vocabulary` rejects the old
    # fused spellings outright, which is why these fixtures read differently
    # from the live rows the module docstring describes.
    kw.setdefault("cycle", "sa2028")
    kw.setdefault("track", "ib")
    kw.setdefault("region", "us")
    kw.setdefault("event_kind", "app_open")
    kw.setdefault("date", dt.date(2027, 3, 1))
    kw.setdefault("precision", "estimated")
    kw.setdefault("confidence", 0.6)
    return FirmDate.objects.create(firm=firm, **kw)


def _page(client, firm):
    res = client.get(f"/firms/{firm.slug}/")
    assert res.status_code == 200
    return res.content.decode()


# ---------------------------------------------------------------------------
# 1. A pill that looks like a citation has to be one
# ---------------------------------------------------------------------------
def test_a_real_url_still_renders_as_a_link(client):
    firm = _firm()
    _date(firm, source_url="https://www.goldmansachs.com/careers/students/")
    body = _page(client, firm)
    assert 'href="https://www.goldmansachs.com/careers/students/"' in body
    assert ">source</a>" in body


def test_a_seed_token_never_becomes_an_anchor(client):
    """The live defect: "seed:historical-pattern" is truthy, so it was
    emitted as href="seed:historical-pattern" — an unknown URI scheme the
    browser parses as absolute, so target=_blank opened nothing."""
    firm = _firm()
    _date(firm, source_url="seed:historical-pattern")
    body = _page(client, firm)
    assert "seed:historical-pattern" not in body
    assert 'href="seed' not in body
    # ...and the provenance is still SAID, in words, as plain text.
    assert "from past cycles" in body


def test_the_demo_seed_row_says_it_is_a_placeholder(client):
    """Apollo's single row wore CONFIRMED on a hard past date and offered
    "seed:demo" as its proof. Suppressing the pill would leave it looking
    like ordinary confirmed data; naming it is the honest fix."""
    firm = _firm(slug="apollo", name="Apollo")
    _date(firm, source_url="seed:demo", precision="day", confidence=1.0,
          date=dt.date(2026, 8, 2), event_kind="app_close")
    body = _page(client, firm)
    assert "seed:demo" not in body
    assert "sample data" in body


@pytest.mark.parametrize("token,phrase", [
    ("research:hongkong", "from the prior HK cycle"),
    ("research:us-ib-calendar", "from measured US lead times"),
])
def test_a_research_estimate_says_what_it_was_estimated_from(client, token, phrase):
    """The re-dated seeds cite a dated research pass rather than a firm page,
    because a firm that has not opened its cycle has published nothing to
    cite. Unmapped, they would have landed on "unverified" — which is where a
    token with no provenance behind it goes, and is the wrong word for the
    best-evidenced estimates in the table."""
    firm = _firm()
    _date(firm, source_url=token)
    body = _page(client, firm)
    assert token not in body
    assert 'href="research' not in body
    assert phrase in body


def test_an_unrecognised_non_url_is_marked_unverified(client):
    firm = _firm()
    _date(firm, source_url="internal:analyst-note")
    body = _page(client, firm)
    assert "internal:analyst-note" not in body
    assert "unverified" in body


def test_an_empty_source_shows_no_pill_at_all(client):
    firm = _firm()
    _date(firm, source_url="")
    body = _page(client, firm)
    for token in ("from past cycles", "sample data", "unverified", ">source<"):
        assert token not in body


# ---------------------------------------------------------------------------
# The market is named once
#
# `cycle_label` expanded a REGION suffix ("sa2028_hk" -> "SA 2028 · Hong Kong")
# into the slot that otherwise holds a TRACK, and the template then appended
# the row's own region again, so seven rows read "SA 2028 · HONG KONG · HK"
# directly beneath rows reading "SA 2028 · HK".
#
# Migration 0014 removed the cause rather than the symptom: a cycle can no
# longer carry a market at all, because `firm_dates_cycle_vocabulary` refuses
# anything but a season+year slug and the desk half moved to `track`. What is
# tested here is therefore split — the constraint that makes the fused
# spelling unwritable, and the formatter that still reads one safely if a row
# written before 0014 ever reaches it.
# ---------------------------------------------------------------------------
def test_a_cycle_can_no_longer_carry_a_market_at_all(client):
    """The stored value that produced the duplication is now unwritable."""
    firm = _firm()
    with pytest.raises(IntegrityError), transaction.atomic():
        _date(firm, cycle="sa2028_hk", track="", region="hk")


def test_the_market_is_printed_once_from_the_region_column(client):
    """With no suffix left to expand there is one source for the market, so
    there is nothing to print twice."""
    firm = _firm()
    _date(firm, cycle="sa2028", track="", region="hk")
    body = _page(client, firm)
    assert "SA 2028 · hk" in body
    assert "Hong Kong" not in body


def test_a_track_keeps_both_the_desk_and_the_market(client):
    """The middle slot means TRACK. `track="ib"` in `us` says two different
    things and must keep saying both."""
    firm = _firm()
    _date(firm, cycle="sa2028", track="ib", region="us")
    body = _page(client, firm)
    assert "SA 2028 · IB · us" in body


def test_a_legacy_region_suffix_still_reads_as_a_cycle_if_one_reaches_the_page():
    """`cycle_label` keeps its suffix branch on purpose. No row can store this
    any more, but a fixture or a pre-0014 backup can still hand one over, and
    printing `SA2028_HK` in the product's own body copy is the failure this
    function exists to prevent."""
    from directory.views import cycle_label, cycle_region
    assert cycle_label("sa2028_hk") == "SA 2028"
    assert cycle_region("sa2028_hk") == "hk"


# ---------------------------------------------------------------------------
# 3. A cycle may not close before it opens
#
# Live on /firms/hsbc/, /firms/ubs/, /firms/ms/ and /firms/jpm/: a dated
# `app_close` and a `seed:historical-pattern` `app_open` estimated ten to
# thirteen months LATER, printing the identical scope "SA 2028 · hk" because
# the two rows store two spellings of one cycle ("SA 2028" and "sa2028_hk")
# that `cycle_label` collapses into one. Sorted by date the close lands first,
# so HSBC's page asserted the HK cycle opens ~Sep 2027 while listing a role
# under that same cycle closing in 76 days.
# ---------------------------------------------------------------------------
def test_an_estimated_opening_after_a_close_in_the_same_scope_is_dropped(client):
    """The live hsbc shape: a dated close and an estimated open, one scope.
    Before 0014 the two rows stored two spellings of that scope; now they
    store the same one, which is the point — the ambiguity was never real."""
    firm = _firm("hsbc", "HSBC")
    _date(firm, cycle="sa2028", track="", region="hk", event_kind="app_close",
          date=dt.date(2026, 10, 30), precision="", confidence=1.0,
          source_url="https://apply.careers.hsbc.com/emergingtalent/job/1365767957/")
    _date(firm, cycle="sa2028", track="", region="hk", event_kind="app_open",
          date=dt.date(2027, 9, 1), source_url="seed:historical-pattern")
    body = _page(client, firm)
    assert "Oct 30, 2026" in body           # the dated close survives
    assert "~ Sep 2027" not in body         # the contradicted estimate does not
    assert body.count("SA 2028 · hk") == 1


def test_the_rumored_close_shape_is_covered_too(client):
    """ubs, ms and jpm carry a RUMORED close, not a confirmed one. The
    estimate is contradicted either way — one is a date on file for this
    cycle and market, the other is a guess about it."""
    firm = _firm("ubs", "UBS")
    _date(firm, cycle="sa2028", track="", region="hk", event_kind="app_close",
          date=dt.date(2026, 8, 3), precision="", confidence=0.3)
    _date(firm, cycle="sa2028", track="", region="hk", event_kind="app_open",
          date=dt.date(2027, 9, 1), source_url="seed:historical-pattern")
    body = _page(client, firm)
    assert "Aug 3, 2026" in body
    assert "~ Sep 2027" not in body


def test_an_estimated_opening_survives_when_nothing_contradicts_it(client):
    """gs holds the same HK estimate with no HK close on file. No
    contradiction, so there is nothing to suppress."""
    firm = _firm()
    _date(firm, cycle="sa2028", track="", region="hk", event_kind="app_open",
          date=dt.date(2027, 9, 1), source_url="seed:historical-pattern")
    body = _page(client, firm)
    assert "~ Sep 2027" in body


def test_a_close_in_a_different_market_does_not_suppress_the_estimate(client):
    """Scope is cycle AND market. A US deadline says nothing about when the
    Hong Kong cycle opens, and must not silence it."""
    firm = _firm()
    _date(firm, cycle="sa2028", track="", region="us", event_kind="app_close",
          date=dt.date(2026, 10, 30), precision="", confidence=1.0)
    _date(firm, cycle="sa2028", track="", region="hk", event_kind="app_open",
          date=dt.date(2027, 9, 1))
    body = _page(client, firm)
    assert "~ Sep 2027" in body


def test_a_close_in_a_different_cycle_does_not_suppress_the_estimate(client):
    """The REAL hsbc shape, once the six mislabelled Hong Kong closes carry
    the cycle they belong to. HSBC's 30 Oct 2026 deadline is the SA 2027 Hong
    Kong intake (Grade A, scratchpad/research-hongkong.md §1); the ~Sep 2027
    estimate is about SA 2028. Two dates in two cycles cannot contradict each
    other, and suppressing the second one took the only SA 2028 Hong Kong
    date Coverage holds off the page."""
    firm = _firm("hsbc", "HSBC")
    _date(firm, cycle="sa2027", track="", region="hk", event_kind="app_close",
          date=dt.date(2026, 10, 30), precision="", confidence=1.0,
          source_url="https://apply.careers.hsbc.com/emergingtalent/job/1365767957/")
    _date(firm, cycle="sa2028", track="", region="hk", event_kind="app_open",
          date=dt.date(2027, 9, 1), source_url="seed:historical-pattern")
    body = _page(client, firm)
    assert "Oct 30, 2026" in body
    assert "~ Sep 2027" in body


def test_a_close_with_no_cycle_on_file_contradicts_nothing(client):
    """"Not stated" is not a scope two rows can share. Two of the live rows
    (gs id 48, jpm id 47) carry no cycle, no market and no source at all; a
    row that says nothing about which cycle it belongs to is not evidence
    that another cycle's opening is wrong."""
    firm = _firm()
    _date(firm, cycle="", track="", region="", event_kind="app_close",
          date=dt.date(2026, 9, 22), precision="", confidence=1.0)
    _date(firm, cycle="sa2028", track="", region="hk", event_kind="app_open",
          date=dt.date(2027, 9, 1), source_url="seed:historical-pattern")
    body = _page(client, firm)
    assert "~ Sep 2027" in body


def test_an_opening_before_its_close_is_a_coherent_cycle_and_stays(client):
    """The ordinary shape — opens, then closes — must be untouched."""
    firm = _firm()
    _date(firm, cycle="sa2028", region="us", event_kind="app_open",
          date=dt.date(2026, 3, 1))
    _date(firm, cycle="sa2028", region="us", event_kind="app_close",
          date=dt.date(2026, 10, 30), precision="", confidence=1.0)
    body = _page(client, firm)
    assert "~ Mar 2026" in body
    assert "Oct 30, 2026" in body


def test_a_confirmed_opening_after_a_close_is_left_visible(client):
    """Two hard dates that disagree is a data conflict, not an over-eager
    estimate. Hiding it would hide the bug."""
    firm = _firm()
    _date(firm, cycle="sa2028", region="us", event_kind="app_close",
          date=dt.date(2026, 10, 30), precision="", confidence=1.0)
    _date(firm, cycle="sa2028", region="us", event_kind="app_open",
          date=dt.date(2027, 9, 1), precision="", confidence=1.0)
    body = _page(client, firm)
    assert "Sep 1, 2027" in body


# ---------------------------------------------------------------------------
# 4. Two contradicted closes must not render side by side
#
# The live shape: jpm carried an Aug 30 close and a Sep 3 close for the same
# printed cycle, both badged "confirmed", nothing on the page telling a student
# which to believe. The old uniqueness constraint was keyed on the STORED
# `cycle`, so two spellings of one scope ("SA 2028" and "sa2028_hk", which
# print identically) sailed straight past it.
#
# Migration 0014 closed that door from the other side. With one spelling per
# cycle and the desk in its own column, two rows that PRINT the same scope now
# necessarily collide on `uniq_firm_dates_firm_cycle_track_region_event` and
# the second cannot be written at all. So the DB-level reproduction below is
# now a CONSTRAINT test, and the read-path guard — which is kept, because a
# constraint only covers writers that go through the ORM — is exercised
# directly against the rows it takes.
# ---------------------------------------------------------------------------
def test_the_second_close_for_one_scope_can_no_longer_be_written(client):
    """The jpm shape, at the layer that now stops it."""
    firm = _firm("jpm", "J.P. Morgan")
    _date(firm, cycle="sa2028", track="", region="hk", event_kind="app_close",
          date=dt.date(2026, 8, 30), precision="", confidence=1.0)
    with pytest.raises(IntegrityError), transaction.atomic():
        _date(firm, cycle="sa2028", track="", region="hk", event_kind="app_close",
              date=dt.date(2026, 9, 3), precision="", confidence=1.0)


def test_two_closes_in_one_scope_are_flagged_conflicting_not_both_confirmed():
    """The read-path guard, on the rows it would receive. Neither date goes
    missing — hiding one is how a student misses the real deadline — but
    neither gets to claim the unqualified "confirmed" badge alone."""
    from directory.views import _flag_conflicting_closes
    rows = [
        {"event_kind": "app_close", "date": dt.date(2026, 8, 30),
         "cycle": "SA 2028", "region": "hk", "state": "confirmed"},
        {"event_kind": "app_close", "date": dt.date(2026, 9, 3),
         "cycle": "SA 2028", "region": "hk", "state": "confirmed"},
    ]
    out = _flag_conflicting_closes(rows)
    assert [r["state"] for r in out] == ["conflicting", "conflicting"]
    assert all(r["conflict"]["label"] == "conflicting dates on file" for r in out)
    assert {r["date"] for r in out} == {dt.date(2026, 8, 30), dt.date(2026, 9, 3)}


def test_a_lone_close_is_still_confirmed(client):
    """The ordinary, non-conflicting shape must be untouched by the check —
    one dated close still earns the plain "confirmed" badge."""
    firm = _firm()
    _date(firm, cycle="sa2028", region="us", event_kind="app_close",
          date=dt.date(2026, 10, 30), precision="", confidence=1.0)
    body = _page(client, firm)
    assert ">confirmed<" in body
    assert "conflicting dates on file" not in body


def test_two_identical_close_dates_are_not_a_conflict():
    """Two rows that happen to agree on the date are redundant data, not a
    disagreement — nothing here for a student to be warned about."""
    from directory.views import _flag_conflicting_closes
    rows = [
        {"event_kind": "app_close", "date": dt.date(2026, 10, 30),
         "cycle": "SA 2028", "region": "hk", "state": "confirmed"},
        {"event_kind": "app_close", "date": dt.date(2026, 10, 30),
         "cycle": "SA 2028", "region": "hk", "state": "confirmed"},
    ]
    assert all(r["state"] == "confirmed" for r in _flag_conflicting_closes(rows))


# ---------------------------------------------------------------------------
# 6. `cycle_months` (the onboarding/Settings deadline band) must use the SAME
#    confirmed bar the timeline right above it does, not a `confidence`-only
#    check that ignores `precision`.
# ---------------------------------------------------------------------------

def _months_at(out, year, month):
    # `cycle_months` labels each slot with "%b" only (no year), which is not
    # enough to address a slot directly — walk the same year/month sequence
    # the function itself builds instead.
    today = timezone.localdate()
    y, m = today.year, today.month
    for r in out:
        if (y, m) == (year, month):
            return r["count"]
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    raise AssertionError(f"{year}-{month} is outside the {len(out)}-month window")


def test_cycle_months_excludes_an_estimated_date_even_at_full_confidence():
    """PINS A FIXED BUG: `cycle_months` filtered `FirmDate` on
    `confidence=1.0` alone, the exact bar `_firm_date_row` (the timeline
    right below this band on the same firm page) was written to replace,
    because `precision="estimated"` is a month-level GUESS that can sit at
    any confidence — nothing in `import_firm_dates` ties the two together
    (see `_CONFIRMED_FIRM_DATE_PRECISIONS`'s comment). No live row pairs
    1.0 with "estimated" today, but a band that would count one if it
    existed is not actually reading the same "confirmed" this page claims
    to show everywhere else."""
    from directory.views import cycle_months

    firm = _firm()
    today = timezone.localdate()
    target = today.replace(day=1) + dt.timedelta(days=40)
    target = target.replace(day=1)  # a later month, comfortably inside the window
    _date(firm, event_kind="app_close", date=target,
          precision="estimated", confidence=1.0)

    out = cycle_months(months=12)
    assert _months_at(out, target.year, target.month) == 0


def test_cycle_months_counts_a_genuinely_confirmed_close():
    """The positive case: a day-precision, full-confidence close still
    lands in its month — the fix narrows the filter, it does not silence it."""
    from directory.views import cycle_months

    firm = _firm()
    today = timezone.localdate()
    target = (today.replace(day=1) + dt.timedelta(days=40)).replace(day=1)
    _date(firm, event_kind="app_close", date=target,
          precision="", confidence=1.0)

    out = cycle_months(months=12)
    assert _months_at(out, target.year, target.month) == 1


# ---------------------------------------------------------------------------
# 5. A date that has already gone says so
#
# This page had no date cutoff of any kind. 10 of the 41 live rows are in the
# past — Morgan Stanley's 6 Aug insight deadline, BlackRock's 31 Aug close,
# Goldman's 15 Aug opening — and every one rendered in exactly the type the
# dates still ahead wear. The row STAYS, because the timeline is the only
# record of what a firm's last cycle did, but it reads as history.
#
# Asserted on the <li>'s own class list, not on the bare token: the page
# inlines its own stylesheet, which names `.tl-row.is-past` in a rule, so a
# substring search for "is-past" is satisfied by the CSS on every page.
# ---------------------------------------------------------------------------
PAST_ROW = 'class="tl-row confirmed is-past"'
LIVE_ROW = 'class="tl-row confirmed"'


def test_a_past_date_is_marked_and_muted(client):
    firm = _firm()
    _date(firm, event_kind="app_close",
          date=timezone.localdate() - dt.timedelta(days=2),
          precision="", confidence=1.0)
    body = _page(client, firm)
    assert PAST_ROW in body
    assert ">past<" in body


def test_a_date_still_ahead_is_not_marked_past(client):
    firm = _firm()
    _date(firm, event_kind="app_close",
          date=timezone.localdate() + dt.timedelta(days=2),
          precision="", confidence=1.0)
    body = _page(client, firm)
    assert LIVE_ROW in body
    assert PAST_ROW not in body


def test_todays_date_is_not_past(client):
    """A deadline is live right up to its own day."""
    firm = _firm()
    _date(firm, event_kind="app_close", date=timezone.localdate(),
          precision="", confidence=1.0)
    assert PAST_ROW not in _page(client, firm)


def test_a_row_with_no_date_on_file_is_not_past_either(client):
    """"Date to be confirmed" is a future event nobody has placed yet, not a
    date that has been and gone."""
    firm = _firm()
    _date(firm, event_kind="app_close", date=None, precision="", confidence=1.0)
    body = _page(client, firm)
    assert "date to be confirmed" in body
    assert PAST_ROW not in body


def test_the_past_row_keeps_its_own_confidence_treatment(client):
    """"Past" is orthogonal to confirmed/rumored — a past row is still a
    sourced fact about the firm and keeps saying which it was."""
    firm = _firm()
    _date(firm, event_kind="app_close",
          date=timezone.localdate() - dt.timedelta(days=2),
          precision="", confidence=1.0)
    body = _page(client, firm)
    assert PAST_ROW in body
    assert "confirmed" in body


# ---------------------------------------------------------------------------
# D-19: the closing hour, where the firm published one.
# ---------------------------------------------------------------------------

def test_the_stated_closing_hour_reaches_the_page(client, settings):
    """Rendered rather than asserted on the view dict, for this file's own
    reason: the bug lives in the seam. `settings.TIME_ZONE` stands in for the
    reader here — a signed-out visitor gets the project default, which is
    exactly the "your time" the template prints."""
    settings.TIME_ZONE = "America/Los_Angeles"
    firm = _firm()
    _date(firm, event_kind="app_close", date=dt.date(2026, 10, 30),
          precision="day", confidence=1.0,
          close_time=dt.time(23, 59), close_tz="Asia/Hong_Kong")

    assert "23:59 HKT, 08:59 your time" in _page(client, firm)


def test_a_row_with_no_stated_hour_prints_no_hour(client):
    """Which is most rows, and the reason nothing here is derived: a time on
    a date the product estimated would be precision it has no source for."""
    firm = _firm()
    _date(firm, event_kind="app_close", date=dt.date(2026, 10, 30),
          precision="day", confidence=1.0)

    body = _page(client, firm)

    assert "your time" not in body
    assert "23:59" not in body
