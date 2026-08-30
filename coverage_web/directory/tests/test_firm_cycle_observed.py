"""The "Observed Activity" block on /firms/<slug>/ — `FirmCycleObservation`
surfaced honestly (see that model's docstring and `directory.views
._cycle_observed`).

Three things this has to get right, matching the feature's own non-negotiable
rules:

1. A count below `CYCLE_OBSERVATION_MIN_SAMPLE` is not a window (see that
   constant's comment) and must render NOTHING for that side — not a hedged
   "not enough data yet" sentence.
2. An "honest zero" row (a firm Coverage has watched but seen nothing move
   on: `opened_count == closed_count == 0`) must produce no claim at all,
   which per (1) falls out of the same gate rather than needing its own
   branch — but is worth asserting directly since it is the model's own
   named failure mode ("must not read as '0 postings closed'").
3. A row with a real sample on both sides must render its actual numbers and
   dates, verbatim, with no invented figure.

These assert against RENDERED HTML (the page a student actually reads), the
same posture `test_firm_timeline.py` takes for the neighbouring Cycle Dates
section, because the seam between the view dict and the template is exactly
where an honesty bug would hide.
"""

from __future__ import annotations

import datetime as dt

import pytest

from directory.models import Firm, FirmCycleObservation

pytestmark = pytest.mark.django_db


def _firm(slug="tdsec", name="TD Securities"):
    return Firm.objects.create(slug=slug, name=name)


def _observation(firm, **kw):
    kw.setdefault("region", "")
    return FirmCycleObservation.objects.create(firm=firm, **kw)


def _page(client, firm):
    res = client.get(f"/firms/{firm.slug}/")
    assert res.status_code == 200
    return res.content.decode()


def test_a_healthy_window_renders_its_real_numbers(client):
    """Mirrors the live TD Securities/other row named in the feature spec:
    opened 99 (Aug 9-29), closed 77 (Aug 18-30). Every number and date in the
    assertion is the one the fixture states — nothing here is invented."""
    firm = _firm()
    _observation(
        firm, region="other",
        opened_count=99,
        open_window_first=dt.date(2026, 8, 9),
        open_window_last=dt.date(2026, 8, 29),
        closed_count=77,
        close_window_first=dt.date(2026, 8, 18),
        close_window_last=dt.date(2026, 8, 30),
    )
    html = _page(client, firm)

    assert "Observed Activity" in html
    assert "Opened 99 postings, Aug 9 to Aug 29." in html
    assert "Closed 77, Aug 18 to Aug 30." in html
    # "other" is a real, if imprecise, region — the label must read as one of
    # classify.REGION_LABELS's words, never the raw code.
    assert "Other Markets" in html
    assert ">other<" not in html.lower()


def test_a_thin_close_count_renders_no_close_claim(client):
    """`closed_count=1` (or 2) is a single data point wearing a "window"
    shape only because `close_window_first == close_window_last` by
    construction — see `CYCLE_OBSERVATION_MIN_SAMPLE`'s comment. The opened
    side clears the gate independently and must still render: the two sides
    carry different trust filters and are gated separately on purpose."""
    firm = _firm(slug="thinclose", name="Thin Close Bank")
    _observation(
        firm, region="us",
        opened_count=12,
        open_window_first=dt.date(2026, 8, 1),
        open_window_last=dt.date(2026, 8, 20),
        closed_count=1,
        close_window_first=dt.date(2026, 8, 25),
        close_window_last=dt.date(2026, 8, 25),
    )
    html = _page(client, firm)

    assert "Observed Activity" in html
    assert "Opened 12 postings" in html
    # No hedge, no number: the close side says nothing, silently.
    assert "Closed 1" not in html
    assert "not enough data" not in html.lower()
    assert "Aug 25" not in html


def test_a_thin_open_count_renders_no_open_claim(client):
    """The opened side gated the same way, independently of the close side —
    a firm with 2 observed opens and a real close window must show the
    close claim and nothing for opens."""
    firm = _firm(slug="thinopen", name="Thin Open Bank")
    _observation(
        firm, region="eu",
        opened_count=2,
        open_window_first=dt.date(2026, 8, 5),
        open_window_last=dt.date(2026, 8, 6),
        closed_count=10,
        close_window_first=dt.date(2026, 8, 10),
        close_window_last=dt.date(2026, 8, 28),
    )
    html = _page(client, firm)

    assert "Observed Activity" in html
    assert "Closed 10, Aug 10 to Aug 28." in html
    assert "Opened 2" not in html


def test_an_honest_zero_row_renders_no_claim_at_all(client):
    """Bain Capital/us in the live data: onboarded, opened=0, closed=0. The
    section must not appear at all — not with "0 postings closed", not with
    any other sentence. Coverage watched and saw nothing move; that is the
    honest answer, and the model's own docstring names this exact row shape
    as the failure mode to avoid."""
    firm = _firm(slug="bain", name="Bain Capital")
    _observation(
        firm, region="us",
        opened_count=0, closed_count=0,
        onboarded_at=dt.date(2026, 7, 23),
    )
    html = _page(client, firm)

    assert "Observed Activity" not in html
    assert "0 postings" not in html
    assert "closed 0" not in html.lower()


def test_a_firm_with_no_observation_row_gets_no_section(client):
    """A firm Coverage has never scraped a cycle-observation pass for
    (nothing in `FirmCycleObservation` at all) is a different, and more
    common, silence — the section must not appear, and never as an empty
    shell with a heading over nothing."""
    firm = _firm(slug="neverscraped", name="Never Scraped LLC")
    html = _page(client, firm)

    assert "Observed Activity" not in html


def test_excluded_suspect_closes_only_shown_alongside_a_real_close_claim(client):
    """`excluded_suspect_closes` is context for a close claim that already
    cleared the sample gate (see the model's docstring: "not a footnote"),
    never a stand-in for one that got suppressed. A firm whose TRUSTED closes
    are too thin to claim a window says nothing about the excluded count
    either, even when that count is large."""
    firm = _firm(slug="mixedtrust", name="Mixed Trust Bank")
    _observation(
        firm, region="sg",
        opened_count=0, closed_count=1, excluded_suspect_closes=8,
        close_window_first=dt.date(2026, 8, 15),
        close_window_last=dt.date(2026, 8, 15),
    )
    html = _page(client, firm)

    assert "Observed Activity" not in html
    assert "excluded" not in html.lower()


def test_a_qualifying_close_claim_states_its_excluded_suspect_count(client):
    firm = _firm(slug="honesttrust", name="Honest Trust Bank")
    _observation(
        firm, region="hk",
        opened_count=0, closed_count=5, excluded_suspect_closes=3,
        close_window_first=dt.date(2026, 8, 1),
        close_window_last=dt.date(2026, 8, 20),
    )
    html = _page(client, firm)

    assert "Closed 5, Aug 1 to Aug 20." in html
    assert "3 more closes excluded as unreliable" in html


def test_unstated_region_omits_the_qualifier_entirely(client):
    """`region == ""` must never print as a raw empty string or a literal
    market name — the honest move is to drop the qualifier and state the
    fact firm-wide."""
    firm = _firm(slug="unstated", name="Unstated Region Bank")
    _observation(
        firm, region="",
        opened_count=6,
        open_window_first=dt.date(2026, 8, 1),
        open_window_last=dt.date(2026, 8, 15),
    )
    html = _page(client, firm)

    assert "Opened 6 postings, Aug 1 to Aug 15." in html
    assert 'class="cyc-obs-region"' not in html


def test_global_region_renders_its_own_label(client):
    firm = _firm(slug="globalfirm", name="Global Firm")
    _observation(
        firm, region="global",
        opened_count=4,
        open_window_first=dt.date(2026, 8, 3),
        open_window_last=dt.date(2026, 8, 3),
    )
    html = _page(client, firm)

    assert "Global / Virtual" in html
    assert "Opened 4 postings, Aug 3." in html
