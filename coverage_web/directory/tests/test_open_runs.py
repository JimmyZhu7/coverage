"""Elapsed openness — the one duration fact this data supports, and the
guard rails that keep it from becoming the one it does not.

WHAT IS BEING DEFENDED. `directory.open_runs`' docstring records the
measurement that decided the shape of this feature: a per-posting open-to-
close duration IS computable (340 paired postings on live data), and is still
not shippable, because a 39-day observation window can only pair the postings
that finished inside it. 83% of live post-onboarding rows are censored, and
77% of the still-open ones have already outlived the median that pairing
produces. Goldman Sachs is the sharpest case — 8 closed pairs give a median
of 11 days while 73 of the firm's 79 live postings have been open longer.

So the rules pinned here are not stylistic:

  1. NO PREDICTION, ANYWHERE. Nothing in the rendered product may claim how
     long a posting WILL stay open. `test_no_surface_predicts_a_duration`
     asserts against the vocabulary such a claim would have to use.
  2. AN UNWATCHED OPEN IS NOT AN OPEN. A posting from a firm's onboarding
     batch has a `first_seen` that records when Coverage arrived, not when
     the posting did. Those rows get NOTHING — not "at least N days", which
     is a different claim, and not a zero.
  3. ONE CUTOFF, ONE DEFINITION. `build_cycle_observations` and the two live
     surfaces have to agree about which day is a firm's onboarding day, so
     they must literally share the function.
  4. THE GATE IS AN EMPTY STATE, NOT A HEDGE. Below the sample floor the
     firm-level line renders nothing at all, the same silence
     `_cycle_observed` keeps for a below-threshold window.
  5. THE ROW'S TRUNCATION CONTRACT SURVIVES. The feed row's meta line orders
     facts by decisiveness so the ellipsis eats the least decisive tail
     first; a new fact must go last and must not bring a second, competing
     shrink rule with it.

Assertions run against rendered HTML wherever a claim reaches a student,
matching `test_firm_cycle_observed.py`'s posture: the seam between a view
dict and a template is exactly where an honesty bug hides.
"""

from __future__ import annotations

import datetime as dt
import inspect
import re
from pathlib import Path

import pytest
from django.utils import timezone

from directory.models import Firm, Opportunity
from directory.open_runs import (
    CYCLE_OBSERVATION_MIN_SAMPLE, firm_open_runs, onboarding_cutoffs,
    open_run_days,
)

pytestmark = pytest.mark.django_db

ROLECARD = (
    Path(__file__).resolve().parents[2] / "templates" / "directory" / "_rolecard.html"
)
ROLE_STYLES = (
    Path(__file__).resolve().parents[2] / "templates" / "directory" / "_styles.html"
)

# The REAL current date, not a hardcoded one. Most tests below pass this
# straight into the pure functions (`open_run_days(..., TODAY, ...)`,
# `firm_open_runs([...], TODAY)`), which is self-consistent whatever it is --
# but the two that render `/opportunities/` do NOT get to choose: the view
# measures elapsed openness against `timezone.localdate()`, the real clock.
# Hardcoded to 2026-08-31, this file passed the day it was written and broke
# the next morning: the fixture backdated first_seen 12 days before Aug 31
# while the view measured against Sep 1, so the page honestly rendered
# "Open 13d" against an assertion of "Open 12d". Same date-fragility that
# `test_closing_soon.py` was fixed for earlier (a `timedelta(days=62)` offset
# read against a hardcoded "2 months"). One clock, shared by fixture and view.
TODAY = timezone.localdate()


def _firm(slug="gs", name="Goldman Sachs"):
    return Firm.objects.create(slug=slug, name=name)


def _opp(firm, *, days_ago, status="open", bucket="internship", title=None,
         deadline=None, url=None):
    """A posting first seen `days_ago` days before TODAY.

    `first_seen` is `auto_now_add`, so backdating is an `.update()` after the
    fact — exactly what real time passing does to the column.
    """
    o = Opportunity.objects.create(
        firm=firm,
        title=title or f"Summer Analyst {days_ago}",
        bucket=bucket,
        status=status,
        deadline=deadline,
        url=url or f"https://example.test/{firm.slug}/{days_ago}/{status}",
    )
    # Offset from the real clock, NOT a fixed hour on a calendar day. The two
    # things a feed row says about age are measured differently: `seen_days`
    # is a TIMESTAMP difference (`(now - first_seen).days`, views.py) while
    # `open_run_days` is a DATE difference. Pinned at 09:00 UTC, the pair
    # disagreed by a day whenever the suite ran before 09:00 -- at 03:22 UTC
    # a row 12 calendar days old reported `seen_days` 11 and `open_run_days`
    # 12, and the undated-row test asserting "first seen 12d ago" failed on
    # the clock rather than on the code. Same instant-of-day as `now` keeps
    # both readings equal to `days_ago` at any hour.
    stamp = timezone.now() - dt.timedelta(days=days_ago)
    Opportunity.objects.filter(pk=o.pk).update(first_seen=stamp)
    o.refresh_from_db()
    return o


# ---------------------------------------------------------------------------
# Rule 2 — an unwatched open is not an open.
# ---------------------------------------------------------------------------

def test_the_onboarding_batch_gets_no_duration_at_all():
    """The oldest posting at a firm defines that firm's onboarding day, and
    every posting sharing it says "Coverage arrived", not "this opened".

    The honest statement for those rows would be "open AT LEAST N days" — a
    materially different claim with a different shape — so the module says
    nothing rather than quietly printing the wrong one. Asserted as `is None`
    specifically: a 0 here would render as "Open today" on the newest-looking
    posting in the firm, which is the exact inversion of the truth.
    """
    firm = _firm()
    onboarding_a = _opp(firm, days_ago=30, url="https://example.test/a")
    onboarding_b = _opp(firm, days_ago=30, url="https://example.test/b")
    watched = _opp(firm, days_ago=12, url="https://example.test/c")
    cutoffs = onboarding_cutoffs([firm.id])

    assert open_run_days(onboarding_a, TODAY, cutoffs) is None
    assert open_run_days(onboarding_b, TODAY, cutoffs) is None
    assert open_run_days(watched, TODAY, cutoffs) == 12


def test_a_closed_posting_carries_no_open_run():
    """"Has been open N days" is not what a closed row is doing. The elapsed
    figure is only ever about a posting that is open RIGHT NOW — which is
    also what makes it immune to the censoring that sinks the open-to-close
    median (see the module docstring)."""
    firm = _firm()
    _opp(firm, days_ago=40, url="https://example.test/onboard")
    closed = _opp(firm, days_ago=10, status="closed", url="https://example.test/x")
    cutoffs = onboarding_cutoffs([firm.id])

    assert open_run_days(closed, TODAY, cutoffs) is None


def test_a_posting_first_seen_today_is_zero_days_not_absent():
    """Zero is a real measurement and must not collapse into the `None` that
    means "we do not hold this fact" — callers render it as "today"."""
    firm = _firm()
    _opp(firm, days_ago=40, url="https://example.test/onboard")
    fresh = _opp(firm, days_ago=0, url="https://example.test/fresh")
    cutoffs = onboarding_cutoffs([firm.id])

    assert open_run_days(fresh, TODAY, cutoffs) == 0


# ---------------------------------------------------------------------------
# Rule 3 — one cutoff, one definition.
# ---------------------------------------------------------------------------

def test_build_cycle_observations_shares_the_one_cutoff_function():
    """The command used to define `_onboarding_cutoff` privately. Two live
    surfaces now depend on the same rule, and three copies of a rule three
    places have to agree on is how they stop agreeing — so this asserts they
    are literally the same object, not merely similar."""
    from directory.management.commands import build_cycle_observations as cmd

    assert cmd._onboarding_cutoff is onboarding_cutoffs


def test_the_cutoff_is_firm_wide_not_campus_only():
    """`build_cycle_observations`' docstring makes this call explicitly:
    onboarding is a FETCH-level event, so a firm's non-campus postings define
    the cutoff too. A campus-only cutoff would read a firm's first campus
    posting as a watched open when it merely happened to be the first campus
    row in a batch that also brought 200 retail reqs."""
    firm = _firm()
    _opp(firm, days_ago=30, bucket="other", url="https://example.test/retail")
    same_day_campus = _opp(firm, days_ago=30, url="https://example.test/campus")
    cutoffs = onboarding_cutoffs([firm.id])

    assert cutoffs[firm.id] == TODAY - dt.timedelta(days=30)
    assert open_run_days(same_day_campus, TODAY, cutoffs) is None


def test_scoping_the_cutoff_to_some_firms_matches_the_unscoped_answer():
    """The feed scopes the aggregate to the firms on the page and the rail
    scopes it to four. A scoped cutoff that disagreed with the unscoped one
    would mean the same posting reads differently depending on which page
    you found it on."""
    a, b = _firm(), _firm(slug="ms", name="Morgan Stanley")
    _opp(a, days_ago=30, url="https://example.test/a1")
    _opp(b, days_ago=20, url="https://example.test/b1")

    full = onboarding_cutoffs()
    assert onboarding_cutoffs([a.id]) == {a.id: full[a.id]}
    assert onboarding_cutoffs([]) == {}


# ---------------------------------------------------------------------------
# Rule 4 — the firm-level gate is an empty state.
# ---------------------------------------------------------------------------

def test_a_firm_below_the_sample_floor_returns_nothing():
    """Two watched postings is not a programme. The floor is
    `CYCLE_OBSERVATION_MIN_SAMPLE`, shared with the firm page's window
    sentences rather than re-picked here."""
    firm = _firm()
    _opp(firm, days_ago=40, url="https://example.test/onboard")
    for i in range(CYCLE_OBSERVATION_MIN_SAMPLE - 1):
        _opp(firm, days_ago=5 + i, url=f"https://example.test/w{i}")

    assert firm_open_runs([firm.id], TODAY) == {}


def test_a_firm_at_the_floor_reports_its_census_and_its_longest_run():
    """The count is exact (a census, not a sample) and the longest run is one
    real posting's real elapsed time — never a mean, which would drift
    downward every time the firm posts something new."""
    firm = _firm()
    _opp(firm, days_ago=40, url="https://example.test/onboard")
    _opp(firm, days_ago=22, url="https://example.test/w1")
    _opp(firm, days_ago=9, url="https://example.test/w2")
    _opp(firm, days_ago=3, url="https://example.test/w3")

    assert firm_open_runs([firm.id], TODAY) == {
        firm.id: {"count": 3, "longest_days": 22}
    }


def test_onboarding_and_closed_rows_do_not_pad_a_firm_to_the_floor():
    """The floor has to be reached by postings whose opening was actually
    watched. Rows excluded for honesty must not be quietly counted back in to
    unlock the very line that honesty gate exists to withhold."""
    firm = _firm()
    _opp(firm, days_ago=40, url="https://example.test/onb1")
    _opp(firm, days_ago=40, url="https://example.test/onb2")
    _opp(firm, days_ago=40, url="https://example.test/onb3")
    _opp(firm, days_ago=10, status="closed", url="https://example.test/cl1")
    _opp(firm, days_ago=8, status="closed", url="https://example.test/cl2")
    _opp(firm, days_ago=6, url="https://example.test/w1")

    assert firm_open_runs([firm.id], TODAY) == {}


def test_non_campus_volume_never_counts_toward_a_recruiting_line():
    """Matching `build_cycle_observations`: a firm's retail-branch reqs can
    dwarf its campus postings, and counting them under a recruiting deadline
    would make the number describe the wrong thing entirely."""
    firm = _firm(slug="td", name="TD Securities")
    _opp(firm, days_ago=40, url="https://example.test/onboard")
    for i in range(6):
        _opp(firm, days_ago=5 + i, bucket="other",
             url=f"https://example.test/retail{i}")
    _opp(firm, days_ago=4, url="https://example.test/campus1")

    assert firm_open_runs([firm.id], TODAY) == {}


# ---------------------------------------------------------------------------
# Rule 5 — the feed row.
# ---------------------------------------------------------------------------

def _feed(client):
    res = client.get("/opportunities/")
    assert res.status_code == 200
    return res.content.decode()


def test_the_feed_row_prints_the_elapsed_figure_for_a_watched_dated_role(client):
    firm = _firm()
    _opp(firm, days_ago=40, url="https://example.test/onboard")
    _opp(firm, days_ago=12, title="Summer Analyst Programme",
         deadline=timezone.localdate() + dt.timedelta(days=20),
         url="https://example.test/watched")

    html = _feed(client)
    assert "Open 12d" in html


def test_the_feed_row_stays_silent_on_an_onboarding_batch_role(client):
    """66% of live campus rows carry this fact and 34% correctly do not. The
    silent third must be silent in the RENDERED page, not merely `None` in a
    dict a template could still coerce into something."""
    firm = _firm()
    _opp(firm, days_ago=40, title="Summer Analyst Programme",
         deadline=timezone.localdate() + dt.timedelta(days=20),
         url="https://example.test/onboard")

    html = _feed(client)
    assert "Summer Analyst Programme" in html
    assert not re.search(r"Open \d+d", html)
    # And specifically not the "open at least N days" phrasing, which is the
    # tempting wrong answer for exactly these rows: it is true, but it is a
    # different claim in the same slot, and a student reading a column of
    # "Open 12d" figures will not notice one of them silently means something
    # else. Matched as the whole phrase because "at least" on its own appears
    # in the page's inlined stylesheet comments.
    assert not re.search(r"[Oo]pen at least", html)


def test_an_undated_row_says_the_age_once_not_twice(client):
    """An undated row's `.rr-undated` span already ends with "first seen 12d
    ago" — the same measurement in wording pinned by another test. Printing
    `Open 12d` alongside it would be the row saying one fact twice."""
    firm = _firm()
    _opp(firm, days_ago=40, url="https://example.test/onboard")
    _opp(firm, days_ago=12, title="Rolling Analyst Role",
         url="https://example.test/undated")

    html = _feed(client)
    assert "first seen 12d ago" in html
    assert "Open 12d" not in html


def test_the_new_fact_sits_last_in_the_meta_line():
    """ORDER IS THE TRUNCATION POLICY. Whatever renders last is what a
    student never reads when the ~165px line overflows, so a descriptive
    firm-history fact belongs there and nothing decisive may end up behind
    it. Asserted on source position because that ordering IS the contract —
    a rendered page with a short meta line would pass either way.
    """
    src = ROLECARD.read_text()
    open_at = src.index('class="rr-open"')
    for decisive in ('class="rr-vd', "{% for f in r.facts %}", 'class="rr-loc',
                     'class="rr-kind"', 'class="rr-cls"'):
        assert src.index(decisive) < open_at, decisive


def test_the_new_fact_adds_no_second_truncation_rule():
    """A first pass at this row let several spans shrink independently and
    produced FOUR mid-word ellipses instead of one clean cut. `.rr-open` must
    behave like every other sibling — `flex: none`, and truncation left to
    the single rule that owns it.

    That rule is `.rr-loc`'s stated max-width now, not `.rr-meta >
    *:last-child` (see `test_the_feed_row_has_exactly_one_truncation_point`).
    The change matters to THIS span specifically: being rendered last used to
    make it the thing that gave way, and measured live on the founder's board
    it was giving way all the way to 0px — the figure was on the page and
    invisible. The assertions below are unchanged, because what they pin is
    that this span never grows a shrink rule of its own, and that is true
    under either owner."""
    css = ROLE_STYLES.read_text()
    rule = re.search(r"\.rr-open\s*\{([^}]*)\}", css)
    assert rule, ".rr-open must be styled explicitly, not left to defaults"
    body = rule.group(1)
    assert "flex: none" in body
    assert "text-overflow" not in body
    assert "flex-shrink" not in body


# ---------------------------------------------------------------------------
# Rule 1 — no prediction, anywhere.
# ---------------------------------------------------------------------------

def test_no_surface_predicts_a_duration(client):
    """The claim this feature deliberately refuses to make.

    Any of these words in the rendered product would mean the page had
    started forecasting a close off a 39-day observation window, which the
    module docstring's censoring measurement rules out. The elapsed figure
    says "has been open", never "stays open" or "closes in about".
    """
    firm = _firm()
    _opp(firm, days_ago=40, url="https://example.test/onboard")
    for i, d in enumerate((22, 9, 3)):
        _opp(firm, days_ago=d,
             deadline=timezone.localdate() + dt.timedelta(days=20),
             url=f"https://example.test/w{i}")

    html = _feed(client).lower()
    for forecast in ("typically open", "usually open", "stays open",
                     "on average", "expected to close", "closes in about",
                     "estimated close"):
        assert forecast not in html, forecast


def test_the_module_never_reads_a_close_event():
    """The structural guarantee behind rule 1. `open_runs` is about postings
    that are still open; the moment it started joining `OpportunityChange`
    closes it would be computing the censored open-to-close duration this
    feature exists to refuse. Asserted on the source so the refusal survives
    a future edit that "just adds the close side back"."""
    import directory.open_runs as mod

    src = inspect.getsource(mod)
    body = src.split('"""', 2)[-1]  # everything after the module docstring
    for banned in ("OpportunityChange", "classify_closes", "closed_at"):
        assert banned not in body, banned
