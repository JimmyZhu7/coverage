"""Where a stated firm date and the scraper's own observations meet.

Two tables hold two different kinds of fact about when a firm recruits, and
until this module they never spoke to each other.

`FirmDate` is an ASSERTION: somebody read a claim somewhere, or a seed file
guessed a month from past cycles, and the row carries a confidence and a
`found_on`. `FirmCycleObservation` is a MEASUREMENT: N postings, each with its
own first-seen or closed-at timestamp, rebuilt from `OpportunityChange` and
`ScrapeRun` (see that model's docstring for why it is deliberately not a
second `FirmDate`).

The defect this closes (`audit-calendar-firmdates.md §6` and D10): twenty-five
estimated rows all carry `found_on` 2026-07-03, nothing ever re-checks them,
and the firm page renders a stale guess forever as "rumored, from past cycles"
with no date on it at all. Meanwhile no surface joins an observation to a
declared date, so a declared date the scraper flatly contradicts is never
flagged. Two live cases as of 2026-09-01: Nomura HK declares `app_open`
2026-09-01 against six postings observed opening 3 to 20 August, and UBS HK
declares a close of 2026-08-03 against seven trusted closes observed 19 to 26
August.

THE TRIGGER IS THE FIRM'S OWN POSTING, NEVER A CALENDAR (P9, E4). A hardcoded
recruiting month is wrong for at least one firm-role pair within twelve
months: McKinsey's undergraduate deadline moved 3.5 months between consecutive
cycles while its full-time deadline moved the other way, and BCG's moved three
weeks (`research-consulting-forums.md §7`, Grade A/B; `SYNTHESIS-PLAN.md`
Part C item 6). So what re-checks an estimate is move-detection: the postings
turning up. There is no month named anywhere in this file.

NOTHING HERE WRITES (P1). An observation never overwrites an estimate and
never edits a row. The two facts are returned side by side, each with its own
provenance, and the surfaces render both. A guess that turns out to be wrong
is evidence about the guess; deleting it would destroy the only record that it
was ever made.
"""

from __future__ import annotations

from datetime import date, timedelta

from .models import FirmCycleObservation, FirmDate
from .open_runs import CYCLE_OBSERVATION_MIN_SAMPLE

# `FirmDate.precision` for a row a seed file guessed from past cycles rather
# than read off a firm's own page. Spelled once here so the two rules below
# cannot drift apart on it.
ESTIMATED = "estimated"

# The two event kinds that have an observable counterpart. An `insight_deadline`
# or an `interview` date is not something a job board's open/close timestamps
# can speak to at all, and this module stays silent about them rather than
# comparing a date against evidence that is about something else.
OPEN_KIND = "app_open"
CLOSE_KIND = "app_close"

# How far an observed opening wave may sit from an estimated open date and
# still count as THE SAME COHORT.
#
# WHAT IT ENCODES: "did the thing we guessed at actually happen, early or
# late", as distinct from "we are watching a different cycle's wave".
#
# WHY 90 DAYS. The upper bound has to be wide enough that a firm moving its
# opening by a full quarter is still recognised as the same wave — the largest
# documented single-cycle move for one firm-role pair is 3.5 months
# (`research-consulting-forums.md §7`) — and tight enough that the PREVIOUS
# cycle's wave, which sits roughly a year away, can never reach across.
#
# MEASURED ON TODAY'S DATA, 2026-09-02: all 25 estimated rows on file are
# 2026-12 to 2027-10 dates, and the nearest observed opening wave to any of
# them is 102 days away (Blackstone US, estimate 2026-12-01, observations
# starting 2026-08-06). Every other pair is 187 to 371 days apart. So at 90
# nothing is superseded today, the boundary is not sitting on a live case, and
# the first thing to trip it will be a real one.
#
# WHAT WOULD CHANGE IT: an estimate superseded by a wave that turns out to
# belong to the previous cycle (widen the gap between cohorts, or start
# storing a cycle on the observation), or a genuine same-cycle wave landing
# outside the band (raise it). Both are visible from the firm page, which is
# why the two windows are always shown side by side rather than collapsed.
COHORT_BAND_DAYS = 90


def observations_for(firm_ids) -> dict[tuple[int, str], FirmCycleObservation]:
    """`{(firm_id, region): row}` for the given firms — THE reader (P5).

    Every consumer of measured cycle activity goes through this one query
    rather than reaching for `firm.cycle_observations` in its own way, so
    "which observations count" is decided once. `region` is lower-cased and
    stripped because the column is deliberately not vocabulary-restricted (it
    holds "", "global", "other", "cn", "jp" alongside us/hk) and a caller
    comparing against `FirmDate.region` must not miss on whitespace.
    """
    ids = [i for i in firm_ids if i]
    if not ids:
        return {}
    return {
        (row.firm_id, (row.region or "").strip().lower()): row
        for row in FirmCycleObservation.objects.filter(firm_id__in=ids)
    }


def _window(first: date | None, last: date | None) -> str:
    """`Aug 9 to Aug 29`, or `Aug 9` for a single day. Words, not a dash: the
    founder's copy rule (P7), and the same phrasing `directory.views.
    _window_text` already uses so two surfaces cannot describe one window in
    two voices."""
    if first is None or last is None:
        return ""
    if first == last:
        return f"{first:%b} {first.day}"
    return f"{first:%b} {first.day} to {last:%b} {last.day}"


def _has_opens(obs) -> bool:
    return bool(obs) and obs.opened_count >= CYCLE_OBSERVATION_MIN_SAMPLE


def _has_closes(obs) -> bool:
    return bool(obs) and obs.closed_count >= CYCLE_OBSERVATION_MIN_SAMPLE


def superseded_by(firm_date: FirmDate, obs) -> dict | None:
    """The observed opening wave that has overtaken this ESTIMATE, or None.

    Only ever fires on an `app_open` whose `precision` is "estimated": a
    confirmed date read off a firm's own page is not superseded by anything,
    it is either right or contradicted (see `contradicted_by`).

    Returns the two facts and both provenances, never a replacement:

        {"observed": "Jul 6 to Jul 22", "observed_count": 14,
         "estimated": date(2027, 9, 1), "found_on": date(2026, 7, 3)}

    The caller renders them side by side. This function cannot and must not
    decide which one is true.
    """
    if firm_date.event_kind != OPEN_KIND:
        return None
    if (firm_date.precision or "") != ESTIMATED or firm_date.date is None:
        return None
    if not _has_opens(obs) or obs.open_window_first is None:
        return None
    band = timedelta(days=COHORT_BAND_DAYS)
    if not (firm_date.date - band <= obs.open_window_first <= firm_date.date + band):
        return None
    return {
        "observed": _window(obs.open_window_first, obs.open_window_last),
        "observed_count": obs.opened_count,
        "estimated": firm_date.date,
        "found_on": firm_date.found_on.date() if firm_date.found_on else None,
    }


def contradicted_by(firm_date: FirmDate, obs) -> str | None:
    """One sentence naming an observation that contradicts a DECLARED date.

    "Declared" means any row whose precision is not "estimated": somebody
    asserted this date. An estimate is not contradicted by evidence, it is
    superseded by it, which is a different and gentler claim.

    Two contradictions are detectable from the two windows and only two:

      - a declared OPEN that the WHOLE observed opening wave predates. The
        firm says applications open on 2026-09-01 and Coverage watched all
        six of its postings appear between 3 and 20 August. Nomura HK, live.
      - a declared CLOSE that the WHOLE observed closing wave postdates. The
        firm says applications closed on 2026-08-03 and Coverage watched all
        seven postings close between the 19th and the 26th. UBS HK, live.

    THE WHOLE WAVE, NOT ITS FIRST POSTING, and that is the load-bearing part.
    One requisition appearing a fortnight before a stated opening is ordinary:
    a firm's board carries roles that are not the programme the date is about,
    and an early single posting is a fact about that requisition, not about
    the declaration. It is only when every posting Coverage watched sits on
    the wrong side of the stated day that the date is describing something
    other than what this board did. Measured 2026-09-02 on all 9 declared
    dates with a matching observation: the loose "first posting" form flagged
    four rows, two of which are exactly that noise — Goldman US declares
    2026-08-15 against a wave running Jul 31 to Aug 26 (the wave straddles the
    date, so the declaration is inside it), and PIMCO US declares 2026-08-15
    against a wave running Aug 14 to Aug 25 (one day early). The whole-wave
    form flags Nomura HK and UBS HK and nothing else.

    Both are strictly one-sided on purpose. A posting appearing AFTER a
    declared open is the declaration coming true; a posting closing BEFORE a
    declared close is one requisition filling early, which is ordinary. Only
    the reverse is evidence that the stated date is describing something else.
    """
    if (firm_date.precision or "") == ESTIMATED or firm_date.date is None:
        return None
    if firm_date.event_kind == OPEN_KIND:
        if not _has_opens(obs) or obs.open_window_last is None:
            return None
        if obs.open_window_last >= firm_date.date:
            return None
        return (
            f"declares applications open {firm_date.date.isoformat()}, but "
            f"{obs.opened_count} postings were observed opening "
            f"{_window(obs.open_window_first, obs.open_window_last)}"
        )
    if firm_date.event_kind == CLOSE_KIND:
        if not _has_closes(obs) or obs.close_window_first is None:
            return None
        if obs.close_window_first <= firm_date.date:
            return None
        return (
            f"declares applications close {firm_date.date.isoformat()}, but "
            f"{obs.closed_count} postings were observed closing "
            f"{_window(obs.close_window_first, obs.close_window_last)}"
        )
    return None


def annotate(firm) -> dict[int, dict]:
    """`{firm_date_id: {...}}` for one firm's timeline.

    Each value carries at most three keys — `found_on`, `superseded` and
    `contradicted` — and a `FirmDate` with nothing to say about it is absent
    from the mapping entirely rather than present with three blanks, so a
    template's `{% if %}` is asking about a fact and not about a shape.

    `found_on` is on EVERY estimated row, which is the smaller half of this
    module and the half that was simply missing: the firm page called 25 rows
    "rumored, from past cycles" and printed no date, so nothing on the page
    said the guess was two months old and nothing said it was the same guess
    on all 25.
    """
    rows = list(firm.firm_dates.all())
    obs = observations_for({firm.id})
    out: dict[int, dict] = {}
    for fd in rows:
        entry: dict = {}
        region = (fd.region or "").strip().lower()
        row_obs = obs.get((firm.id, region))
        if (fd.precision or "") == ESTIMATED and fd.found_on:
            entry["found_on"] = fd.found_on.date()
        note = superseded_by(fd, row_obs)
        if note:
            entry["superseded"] = note
        clash = contradicted_by(fd, row_obs)
        if clash:
            entry["contradicted"] = clash
        if entry:
            out[fd.id] = entry
    return out


def contradiction_report() -> list[str]:
    """Every declared date the scraper's own observations contradict, as
    operator-readable lines for `directory.health.health_report`.

    One query for the dates, one for the observations. Sorted by firm slug
    then region so the report is stable between runs and a diff between two
    days means something changed rather than that Postgres returned rows in
    a different order.
    """
    dates = list(
        FirmDate.objects.filter(event_kind__in=(OPEN_KIND, CLOSE_KIND))
        .exclude(date=None)
        .exclude(precision=ESTIMATED)
        .select_related("firm")
    )
    obs = observations_for({fd.firm_id for fd in dates})
    out = []
    for fd in dates:
        region = (fd.region or "").strip().lower()
        clash = contradicted_by(fd, obs.get((fd.firm_id, region)))
        if clash:
            out.append((
                fd.firm.slug, region,
                f"{fd.firm.name} ({region or 'no region'}) {clash}",
            ))
    return [line for _, _, line in sorted(out)]
