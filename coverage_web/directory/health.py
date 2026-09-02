"""Scrape-health checks: the failures that were happening in silence.

Two blind spots, both observed on the live system before this existed:

1. REPEAT FAILURES. Evercore's board timed out in 5 of 14 daily runs and the
   only trace was a line inside `ScrapeRun.stats["errors"]` — a JSON blob
   nothing read. A board that fails quietly presents stale data as fresh:
   the rows keep their old `last_verified`, the feed keeps rendering them,
   and nobody knows the firm stopped being checked.

2. CONFIGURED-BUT-NEVER-YIELDS. Jefferies has a board entry in the catalog
   and zero rows ever. That is indistinguishable, from the outside, from
   "Jefferies has no openings" — but it is actually "the board URL is wrong
   or lists nothing", which is a configuration bug wearing an empty feed as
   a disguise.

Pure functions over already-recorded data (ScrapeRun rows + the board
catalog); the `refresh` command prints their findings at the end of every
run, so the place a failure would hide is the place it now gets announced.
"""

from __future__ import annotations

from coverage_connectors.http import BOT_BLOCK_PREFIX
from django.db.models import Count

from . import estimates
from .boards import BOARDS
from .models import Firm, Opportunity, ScrapeRun

# A firm failing in this many consecutive scrape runs is a pattern, not a
# blip. The transient retry (coverage_connectors.fetch_with_retry) already
# absorbed one network hiccup per run before the failure was recorded.
CONSECUTIVE_FAILURES = 3

# How many runs back to walk when dating a bot wall's streak. Two runs a day
# means this covers a month — plenty to say "since when" without an unbounded
# scan.
WALL_STREAK_SCAN_RUNS = 60

# Where the standing "bot-walled" line points for its evidence. A wall is the
# one health line that never resolves and that nobody here can fix, so the
# reader's only reasonable question is "says who, and when was that checked?".
# Before this, the answer lived in two research files that flatly contradicted
# each other about whether tal.net was reachable at all. The probe recorded in
# this document settled it — four of five tal.net boards answer the project's
# own user agent with a real board, Nomura serves an Oleeo Protect challenge to
# every user agent and cookie jar tried, and no user-agent choice changes that.
# Citing the file keeps the line falsifiable: re-run the probe, update the
# document, and the line is either still true or visibly out of date.
WALL_EVIDENCE_DOC = "docs/talnet-2026-09.md"


# Not every entry in `stats["errors"]` is a failure. Two of them are the
# ingest's own safety guards REPORTING THEMSELVES: the truncation guard
# ("board reported more results than one fetch returns") and the suspicious-
# wipe guard ("fetch ok but 0 rows while postings are open"). In both cases
# the fetch SUCCEEDED — `boards_ok` was incremented — and the guard simply
# declined to auto-close on a list it knew was incomplete, which is the
# correct and desired behaviour.
#
# Measured 2026-08-18: J.P. Morgan and Lazard (both oracle, both genuinely
# healthy — 146 and 73 open rows, none stale) had been tripping the
# "failing in each of the last 3 scrape runs / stale data being presented as
# fresh" alarm every single run, because `repeat_failures` read every
# `errors` entry as a failure. That is the exact trap this module already
# documents for bot walls: an alarm that fires daily on a working system
# teaches the reader to ignore the line that exists to catch real breakage.
#
# Matched on the message text rather than a new flag on purpose —
# `walled_boards` walks 60 historical runs, and a flag written from today
# forward would misread every `ScrapeRun` already in the table.
#
# NARROWED 2026-09-01, from "skipped auto-close" to the partial-list guard
# alone. Both of ingest's guards contain the shorter phrase, and the wipe
# guard — "fetch ok but 0 rows while postings are open" — is emphatically NOT
# a healthy board. It is what a vacated Greenhouse token trips (`{"jobs":[]}`
# on a firm that still holds open rows), and it was being printed under
# "auto-close skipped on a partial list (board is healthy …)". Sixth Street's
# token 404ing on 20 open rows and Marshall Wace's board going silent both
# landed in that reassuring line. The longer marker matches only the guard
# that really does mean "this board is fine, its list was just incomplete",
# and reads every historical run correctly because ingest has always spelled
# the two messages differently.
_GUARD_NOTICE_MARKER = "skipped auto-close (partial list)"

# The wipe guard's own text, so the two can be told apart deliberately rather
# than by one accidentally not matching the other.
_WIPE_GUARD_MARKER = "skipped auto-close (suspected shape change)"


def _is_walled(err: dict) -> bool:
    return (err.get("error") or "").startswith(BOT_BLOCK_PREFIX)


def _is_guard_notice(err: dict) -> bool:
    """A guard declining to act on incomplete data — not a fetch failure.

    The PARTIAL-LIST guard only. The wipe guard shares the phrase "skipped
    auto-close" and means the opposite thing (see `_GUARD_NOTICE_MARKER`);
    it counts as a real failure and is reported as one.
    """
    return _GUARD_NOTICE_MARKER in (err.get("error") or "")


def _is_wipe_guard(err: dict) -> bool:
    """A board that fetched, returned nothing, and holds open rows."""
    return _WIPE_GUARD_MARKER in (err.get("error") or "")


def _is_real_failure(err: dict) -> bool:
    return not _is_walled(err) and not _is_guard_notice(err)


def repeat_failures(limit_runs: int = CONSECUTIVE_FAILURES) -> list[str]:
    """Firm names that appear in the errors of EVERY one of the last N scrape
    runs. Consecutive, not cumulative: a firm that failed once last week and
    once today is having bad luck; one failing every run is down.

    Bot-walled boards are excluded: a wall the operator put up deliberately
    fails every run by definition, and letting it trip this alarm daily
    teaches the reader to ignore the line that exists to catch fixable
    breakage. Walls get their own report (`walled_boards`).

    Only FULL scrapes (connector="all") count. The old exclude-reverify
    filter let two things poison the window: `enrich`/`extract` rows carry
    no errors at all, so with them interleaved (one each per refresh) the
    three-run intersection was permanently empty and this alarm could never
    fire; and a `firm:x`-scoped run says nothing about the other ninety
    boards yet would have reset every other firm's streak."""
    runs = list(
        ScrapeRun.objects.filter(connector="all")
        .order_by("-started")[:limit_runs]
    )
    if len(runs) < limit_runs:
        return []
    per_run = [
        {e.get("firm", "") for e in (r.stats or {}).get("errors", [])
         if _is_real_failure(e)}
        for r in runs
    ]
    always = set.intersection(*per_run) - {""}
    return sorted(always)


def walled_boards() -> list[dict]:
    """Firms whose latest scrape hit a bot-protection interstitial, each with
    the streak's start and how many open rows are frozen behind the wall.

    A wall is a standing condition, not an outage: the operator decided the
    board needs a human check, so it will "fail" every run until they change
    their mind, and no config edit on this side fixes it. The report keeps it
    visible (frozen rows age silently otherwise) without letting it wear the
    alarm styling that belongs to fixable failures.

    `is_new` is True only while the latest run is the streak's FIRST — the
    one moment a wall deserves to be loud. Every run after that it is known
    history.

    Full scrapes only, same reasoning as `repeat_failures`: a `firm:hps`
    run that fetched cleanly must not read as "the wall came down"."""
    runs = list(
        ScrapeRun.objects.filter(connector="all")
        .order_by("-started")[:WALL_STREAK_SCAN_RUNS]
    )
    if not runs:
        return []
    latest_walled = {
        e.get("firm", "") for e in (runs[0].stats or {}).get("errors", [])
        if _is_walled(e)
    } - {""}
    if not latest_walled:
        return []

    open_rows = dict(
        Opportunity.objects.exclude(status="closed")
        .filter(firm__name__in=latest_walled)
        .values_list("firm__name")
        .annotate(n=Count("id"))
        .values_list("firm__name", "n")
    )

    out = []
    for firm in sorted(latest_walled):
        since = runs[0].started
        streak = 1
        for r in runs[1:]:
            if firm in {e.get("firm", "") for e in (r.stats or {}).get("errors", [])
                        if _is_walled(e)}:
                since = r.started
                streak += 1
            else:
                break
        out.append({
            "firm": firm,
            "since": since,
            "is_new": streak == 1,
            "open_rows": open_rows.get(firm, 0),
        })
    return out


def duplicate_firms() -> list[dict]:
    """Firm rows sharing a name, with the postings stranded behind each
    non-canonical copy.

    THE INCIDENT. Ids 199 (slug `td`) and 207 (slug `td-closed`) both carry
    the name "TD Securities" — one seeded, one minted by a test fixture run
    directly against the dev database on 2026-08-15. Measured 2026-08-18:
    1,475 URLs exist under both firms and 1,258 of them are open on BOTH
    sides right now, which is 1,258 duplicate cards in a feed whose whole
    pitch is that it is more trustworthy than the spreadsheet it replaces.

    WHY IT IS WORSE THAN COSMETIC. The board catalog keys on slug, and
    `ingest._FirmResolver` resolves a board's firm NAME to the lowest-id
    match. So every scrape lands on 199, and nothing ever resolves to 207:
    its rows are unreachable by any board, so they are never re-fetched,
    never re-verified, and never closed. They sit open forever at whatever
    they said the day the split happened. Staleness elsewhere in this
    pipeline self-heals on the next pass; this does not, because there is
    no next pass for those rows.

    WHY THIS CHECK EXISTS. `_FirmResolver`'s `.order_by("id")` fix stopped a
    collision from WIDENING, and `manage.py merge_duplicate_firms` can clean
    one up — but between them nothing ever said a collision was there. The
    live one went unnoticed for three days across roughly a dozen scheduled
    scrapes. That is exactly this module's subject: a failure whose only
    symptom is data quietly going wrong while every count stays green.

    Cheap in the healthy case, which is the one that runs nightly: one
    aggregate query returning nothing. The per-group work only happens when
    a collision actually exists.
    """
    from .firm_merge import find_duplicate_firm_groups

    groups = find_duplicate_firm_groups()
    if not groups:
        return []

    # Only the non-canonical rows strand postings — the canonical (lowest-id)
    # row is the one every board already resolves to.
    stranded_ids = [f.id for group in groups for f in group[1:]]
    frozen = dict(
        Opportunity.objects.exclude(status="closed")
        .filter(firm_id__in=stranded_ids)
        .values_list("firm_id")
        .annotate(n=Count("id"))
        .values_list("firm_id", "n")
    )
    return [
        {
            "name": group[0].name,
            "canonical": group[0].slug,
            "duplicates": [f.slug for f in group[1:]],
            "stranded_rows": sum(frozen.get(f.id, 0) for f in group[1:]),
        }
        for group in groups
    ]


def guarded_boards() -> list[str]:
    """Firms whose latest full scrape hit a guard that declined to auto-close.

    Not an alarm, and deliberately a quiet ·-line: these boards are working.
    But the operator does lose something real — for as long as a board
    reports a partial list, closed-detection never runs on it, so a posting
    the firm took down stays open until `reverify` reaches that URL on its
    own and closes it from evidence. That is slower than the nightly sweep,
    and it is worth knowing which firms are on the slow path.

    Replaces, rather than merely removes, what these boards used to
    contribute: before `_is_guard_notice` they were being announced every
    run as failing and serving stale data, which was false on both counts.
    """
    latest = (ScrapeRun.objects.filter(connector="all")
              .order_by("-started").first())
    if latest is None:
        return []
    return sorted({
        e.get("firm", "") for e in (latest.stats or {}).get("errors", [])
        if _is_guard_notice(e)
    } - {""})


def boards_that_never_yield() -> dict[str, list[str]]:
    """Catalog entries whose firm has never produced a single row, split by
    what the scrape logs say about WHY.

    The old version lumped them together and called every one "a config bug,
    not a market fact". Checked by hand 2026-08-05, that was wrong for both
    of its live hits: HPS's Greenhouse token resolves and returns
    `{"jobs": [], "total": 0}` (their hiring moved under BlackRock after the
    acquisition), and jefferies.tal.net serves a full board page containing
    zero vacancy links. Both boards are live and genuinely empty — exactly
    the "market fact" the message insisted they weren't.

    The scrape runs already record which firms ERRORED, so the two cases are
    distinguishable from data on hand:

    - "broken": never yielded AND erroring in the most recent run — the URL
      is wrong or the fetch is failing. A real configuration bug.
    - "walled": never yielded and the latest error is a bot-protection
      interstitial. Neither of the other two messages is true for these:
      the config is fine (nothing to fix) and the board is NOT empty (it is
      unreadable — jefferies.tal.net sat in "empty" for weeks reading as
      "Jefferies has no openings" while Oleeo Protect was eating the page).
    - "empty": never yielded and fetching cleanly — the board just has
      nothing on it. Worth a quiet eye (an empty board and a silently
      wrong-but-resolving URL look identical from here), not an alarm.
    """
    producing_firm_ids = set(
        Opportunity.objects.values_list("firm_id", flat=True).distinct()
    )
    producing_slugs = set(
        Firm.objects.filter(id__in=producing_firm_ids).values_list("slug", flat=True)
    )
    configured = {slug for slug, _ in BOARDS}
    silent = configured - producing_slugs

    latest = (ScrapeRun.objects.filter(connector="all")
              .order_by("-started").first())
    latest_errors = (latest.stats or {}).get("errors", []) if latest else []
    erroring_firms = {
        e.get("firm", "").lower() for e in latest_errors if _is_real_failure(e)
    }
    walled_firms = {
        e.get("firm", "").lower() for e in latest_errors if _is_walled(e)
    }
    firm_names = dict(
        Firm.objects.filter(slug__in=silent).values_list("slug", "name")
    )
    walled = {s for s in silent
              if firm_names.get(s, s).lower() in walled_firms}
    broken = {s for s in silent - walled
              if firm_names.get(s, s).lower() in erroring_firms}
    return {"broken": sorted(broken), "walled": sorted(walled),
            "empty": sorted(silent - broken - walled)}


def board_health() -> list[dict]:
    """One row per catalog board, with what the latest full scrape did to it.

    WHY PER BOARD. Every check above this line keys on the FIRM, and the
    catalog holds 127 boards under 110 slugs. A firm with two boards on one
    provider gets one verdict between them, and it is the optimistic one:
    Moelis and Perella Weinberg each gained two tal.net campus boards on
    2026-09-01, both of which fetch cleanly and return zero, and both of which
    were invisible behind a producing Workday board. Marshall Wace's
    Greenhouse board has answered `{"jobs":[]}` since August — no error, no
    guard, no line anywhere. Sixth Street's token started 404ing two runs ago
    holding 20 open rows and `repeat_failures` will not say so for another run
    because it needs three consecutive.

    Each row carries:

      slug, firm, provider, board   which board this is (`boards.board_key`)
      state                         one of the states below
      rows                          what the latest run fetched from it
      open_rows                     what the firm holds open on this provider
      ever                          rows this (firm, provider) has EVER held
      error                         the run's own message, if any

    The states, in the order they are decided:

      "failed"        the latest run reported this board `ok=False` — a 404,
                      an unreadable body, a network error.
      "walled"        `ok=False` because of a bot-protection interstitial.
                      Separated from "failed" because nothing on this side
                      fixes it and `walled_boards()` already says it once.
      "wiped"         fetched clean, returned zero, and the firm still holds
                      open rows on this provider. The wipe guard's case.
      "silent"        fetched clean, returned zero, and this (firm, provider)
                      has produced rows at some point in the past. The
                      audit's "produced before, zero now" — Marshall Wace
                      exactly: nothing open to protect, so no guard fires and
                      nothing was ever said.
      "empty"         fetched clean, returned zero, and never produced a row.
                      Plausible; the registered-and-empty tal.net events
                      boards live here on purpose.
      "ok"            returned rows.

    A run recorded before `stats["boards"]` existed carries no per-board data.
    Rather than print nothing, this falls back to attributing the run's
    firm-level errors to every board that firm has on that provider, and says
    so in `attribution` — an honest "one of these two" beats silence, and the
    next full run replaces it with the real thing.
    """
    from .boards import board_key

    latest = (ScrapeRun.objects.filter(connector="all")
              .order_by("-started").first())
    if latest is None:
        # No full scrape has ever run. Every board would read "empty", which
        # is a verdict about the boards rather than about the silence — say
        # nothing instead.
        return []
    stats = (latest.stats or {}) if latest else {}
    per_board = {
        (b.get("firm", ""), b.get("provider", ""), b.get("board", "")): b
        for b in stats.get("boards", [])
    }
    attribution = "board" if per_board else "firm"
    firm_errors: dict[tuple[str, str], str] = {}
    if not per_board:
        for e in stats.get("errors", []):
            firm_errors.setdefault(
                ((e.get("firm") or "").lower(), e.get("provider") or ""),
                e.get("error") or "",
            )

    firms = {f.slug: f for f in Firm.objects.filter(slug__in={s for s, _ in BOARDS})}
    open_by_pair = {
        (fid, src): n for fid, src, n in
        Opportunity.objects.exclude(status="closed")
        .values_list("firm_id", "source").annotate(n=Count("id"))
    }
    # No status filter: "ever" means every row this pair has held, closed
    # ones included. That is the whole point of the "silent" state — a board
    # that produced in the past and holds nothing open now has no live rows
    # to protect, so no guard fires and nothing was ever said about it.
    ever_by_pair = {
        (fid, src): n for fid, src, n in
        Opportunity.objects.values_list("firm_id", "source").annotate(n=Count("id"))
    }

    rows: list[dict] = []
    for slug, board in BOARDS:
        firm = firms.get(slug)
        provider = board.provider
        key = board_key(board)
        pair = (firm.id if firm else None, provider)
        open_rows = open_by_pair.get(pair, 0)
        ever = ever_by_pair.get(pair, 0)
        entry = per_board.get(((firm.name if firm else board.firm), provider, key))
        if entry is None and per_board:
            # The board did not run (a --provider or --firm scoped run, or a
            # catalog entry added since). Not a verdict.
            continue
        if entry is not None:
            ok, fetched = entry.get("ok", True), entry.get("rows", 0)
            error = entry.get("error", "")
        else:
            error = firm_errors.get(((firm.name if firm else board.firm).lower(), provider), "")
            ok = not error or _is_guard_notice({"error": error})
            fetched = open_rows if ok else 0
            if _is_wipe_guard({"error": error}):
                # The old runs' own name for "fetched clean, returned zero,
                # rows still open". Read as the wipe it is rather than as a
                # generic failure, so a historical run classifies the same way
                # the next one will.
                ok, fetched = True, 0

        if not ok and _is_walled({"error": error}):
            # A wall is a standing condition the operator chose, not a
            # failure to fix — `walled_boards()` already reports it once,
            # loudly, on the run it appears. Kept in the table (the frozen
            # rows are real) and out of the ⚠ line.
            state = "walled"
        elif not ok:
            state = "failed"
        elif fetched:
            state = "ok"
        elif open_rows:
            state = "wiped"
        elif ever:
            state = "silent"
        else:
            state = "empty"
        rows.append({
            "slug": slug, "firm": firm.name if firm else board.firm,
            "provider": provider, "board": key, "state": state,
            "rows": fetched, "open_rows": open_rows, "ever": ever,
            "error": error, "attribution": attribution,
        })
    return rows


#: The board states `health_report` raises a ⚠ for. "silent" and "empty" get
#: quiet ·-lines: neither has live rows at risk, and an alarm that fires every
#: night on a registered-and-empty events board is an alarm nobody reads.
_ALARMING_BOARD_STATES = ("failed", "wiped")


def board_health_table() -> str:
    """`board_health()` rendered as a fixed-width table, worst first. What an
    operator reads when the one-line summary says something is wrong."""
    rows = board_health()
    if not rows:
        return "no full scrape recorded yet"
    order = {"failed": 0, "wiped": 1, "walled": 2, "silent": 3, "empty": 4, "ok": 5}
    rows.sort(key=lambda r: (order.get(r["state"], 9), r["slug"], r["board"]))
    width = max(len(r["board"]) for r in rows)
    out = [f"{'slug':<18} {'provider':<15} {'board':<{width}} {'state':<7} "
           f"{'rows':>5} {'open':>5} {'ever':>5}  error"]
    for r in rows:
        out.append(
            f"{r['slug']:<18} {r['provider']:<15} {r['board']:<{width}} "
            f"{r['state']:<7} {r['rows']:>5} {r['open_rows']:>5} {r['ever']:>5}  "
            f"{(r['error'] or '')[:90]}"
        )
    return "\n".join(out)


def firms_without_campus_board() -> list[dict]:
    """Catalog firms whose registered board is the experienced-hire site.

    `boards.NO_CAMPUS_BOARD` states which firms those are and why (each one
    measured, none guessed). This re-checks the claim against the database
    every run and drops any firm that has since produced a campus row, so the
    marker cannot outlive the fact it describes — a stale "no campus board"
    on a firm that now has one would be its own quiet lie.

    Reported because the alternative is worse than silence: these firms are
    not empty, they are full of the wrong rows, and a student reading a firm
    page with 273 Ares postings and no internship has no way to know the
    programme exists somewhere Coverage never looks.
    """
    from .boards import NO_CAMPUS_BOARD
    from .classify import TARGET_BUCKETS

    firms = {f.slug: f for f in Firm.objects.filter(slug__in=NO_CAMPUS_BOARD)}
    if not firms:
        return []
    campus_by_firm = dict(
        Opportunity.objects.filter(bucket__in=TARGET_BUCKETS,
                                   firm_id__in=[f.id for f in firms.values()])
        .values_list("firm_id").annotate(n=Count("id")).values_list("firm_id", "n")
    )
    open_by_firm = dict(
        Opportunity.objects.exclude(status="closed")
        .filter(firm_id__in=[f.id for f in firms.values()])
        .values_list("firm_id").annotate(n=Count("id")).values_list("firm_id", "n")
    )
    out = []
    for slug, note in sorted(NO_CAMPUS_BOARD.items()):
        firm = firms.get(slug)
        if firm is None or campus_by_firm.get(firm.id):
            continue
        out.append({"slug": slug, "firm": firm.name, "note": note,
                    "open_rows": open_by_firm.get(firm.id, 0)})
    return out


# How long the detail-page pass may be silent before that silence is itself
# the finding. It runs inside `refresh`, so on a healthy cadence there is a
# run every night.
ENRICH_SILENT_DAYS = 3


def enrichment_health() -> list[str]:
    """Warnings about the pass that reads postings' own pages.

    It earns its own checks because its failure is invisible in every other
    signal on the board: the scrape still succeeds, the counts still look
    right, and the roles still list. What quietly stops is the deadlines. The
    first version of this pipeline lost a full enrichment run to an overnight
    scrape and nothing anywhere went red.
    """
    from django.utils import timezone

    from .models import ScrapeRun

    lines: list[str] = []
    latest = ScrapeRun.objects.filter(connector="enrich").order_by("-started").first()
    if latest is None:
        return ["· the detail-page pass has never run — deadlines come only "
                "from list endpoints, which mostly do not carry one"]

    age = (timezone.now() - latest.started).days
    if age >= ENRICH_SILENT_DAYS:
        lines.append(
            f"⚠ the detail-page pass has not run in {age} days — deadlines and "
            "sponsorship answers are going stale silently")

    stats = latest.stats or {}
    queued, fetched = stats.get("queued", 0), stats.get("fetched", 0)
    unreachable = stats.get("unreachable", 0)
    # A run that reached nothing it queued is a broken run wearing a green
    # status: it exits 0, writes no rows, and reports "0 pages read".
    if queued and not fetched:
        lines.append(
            f"⚠ the last detail-page pass queued {queued} pages and read none "
            "(host blocking, or every URL shape unrecognised)")
    elif fetched and unreachable > fetched:
        lines.append(
            f"⚠ the last detail-page pass failed on more pages than it read "
            f"({unreachable} unreachable vs {fetched} read)")
    return lines


def health_report() -> list[str]:
    """Human-readable warning lines; empty when everything is healthy.

    PER BOARD FIRST, then the per-firm rollup. The firm-level checks below
    are all still here and still worth reading — a firm failing three runs
    running is a different finding from one board failing once — but they
    cannot see a second board behind a producing first one, and that is where
    tonight's whole silent-failure class was hiding. The board lines come
    first because they are the ones with a specific thing to fix.
    """
    lines: list[str] = []
    boards = board_health()
    alarming = [b for b in boards if b["state"] in _ALARMING_BOARD_STATES]
    if alarming:
        # A board that fetched cleanly and returned nothing while the firm
        # holds open rows is NOT the same finding as one that errored, and
        # they are worth separating: the first is usually a token or a URL
        # that has quietly moved, the second is usually the network or a wall.
        failed = [b for b in alarming if b["state"] == "failed"]
        wiped = [b for b in alarming if b["state"] == "wiped"]
        if failed:
            lines.append(
                "⚠ boards failing in the latest run: " + ", ".join(
                    f"{b['slug']}/{b['board']} ({b['open_rows']} open rows)"
                    for b in failed)
                + " — `manage.py board_health` for the messages"
            )
        if wiped:
            lines.append(
                "⚠ boards that fetched cleanly and returned zero while the firm "
                "still holds open rows (a moved token or a changed page reads "
                "exactly like this): " + ", ".join(
                    f"{b['slug']}/{b['board']} ({b['open_rows']} open)"
                    for b in wiped)
            )
    silent_boards = [b for b in boards if b["state"] == "silent"]
    if silent_boards:
        # THE WIPE GUARD'S BLIND SPOT. A board with nothing open left trips no
        # guard at all — there is nothing to refuse to close — so "produced
        # before, zero now" reads as perfectly healthy. Marshall Wace's single
        # row closed on 2026-08-11 and its board has answered `{"jobs":[]}`
        # ever since, unremarked.
        lines.append(
            "· produced rows before, zero now (nothing open, so no guard fires "
            "— check the board still points somewhere): " + ", ".join(
                f"{b['slug']}/{b['board']} ({b['ever']} ever)"
                for b in silent_boards)
        )
    no_campus = firms_without_campus_board()
    if no_campus:
        lines.append(
            "· no campus board registered (the board we scrape is the "
            "experienced-hire site, so these firms' rows are real and none of "
            "them is for a student): " + ", ".join(
                f"{f['slug']} ({f['open_rows']} open, {f['note']})"
                for f in no_campus)
        )
    # First, and always a ⚠ rather than a standing ·-line: unlike a bot wall,
    # this one is fixable from here by a single command, and the rows behind
    # it never self-heal. It should keep nagging until somebody runs it.
    for dupe in duplicate_firms():
        stranded = (f"{dupe['stranded_rows']} postings stranded"
                    if dupe["stranded_rows"] else "no postings stranded")
        lines.append(
            f"⚠ duplicate firm rows for {dupe['name']!r} "
            f"({', '.join(dupe['duplicates'])} alongside {dupe['canonical']}; "
            f"{stranded} — no board resolves to them, so they never refresh "
            f"or close, and they render as duplicate cards): fix with "
            f"`manage.py merge_duplicate_firms --apply`"
        )
    lines += enrichment_health()
    failing = repeat_failures()
    if failing:
        lines.append(
            f"⚠ failing in each of the last {CONSECUTIVE_FAILURES} scrape runs "
            f"(stale data being presented as fresh): {', '.join(failing)}"
        )
    walled = walled_boards()
    if walled:
        # ⚠ exactly once — the run that first hits the wall — then the
        # standing ·-line. The daily pipeline notifies on ⚠ lines, so a new
        # wall reaches the owner without every later run re-ringing the bell.
        parts = []
        for w in walled:
            frozen = (f"{w['open_rows']} open rows frozen" if w["open_rows"]
                      else "no rows held")
            parts.append(f"{w['firm']} ({frozen}, since {w['since']:%Y-%m-%d})")
        marker = "⚠" if any(w["is_new"] for w in walled) else "·"
        lines.append(
            f"{marker} bot-walled (operator requires a human check; not a "
            f"config bug and not an empty board — check by hand): "
            f"{', '.join(parts)}; evidence in {WALL_EVIDENCE_DOC}"
        )
    guarded = guarded_boards()
    if guarded:
        lines.append(
            "· auto-close skipped on a partial list (board is healthy; these "
            "firms' dead postings close via reverify instead of the nightly "
            f"sweep, so they clear more slowly): {', '.join(guarded)}"
        )
    silent = boards_that_never_yield()
    if silent["broken"]:
        lines.append(
            "⚠ never produced a row AND erroring in the latest run (bad board "
            f"URL or failing fetch — fix the config): {', '.join(silent['broken'])}"
        )
    if silent["empty"]:
        lines.append(
            "· never produced a row but fetches cleanly (board is live and "
            "empty — plausible market fact, worth a manual look now and then): "
            f"{', '.join(silent['empty'])}"
        )
    # A DECLARED DATE THE SCRAPER'S OWN OBSERVATIONS CONTRADICT.
    #
    # Every other line in this report is about a board that stopped working.
    # This one is about a board that worked perfectly and disagreed with a
    # date somebody wrote down, which no surface in the product could see
    # before: `FirmDate` and `FirmCycleObservation` had no join between them
    # (`audit-calendar-firmdates.md` D10). ⚠ rather than a standing ·-line
    # because a contradiction is a fact somebody has to go and check against
    # the firm's own page; it does not clear on its own and it does not decay
    # into background noise the way an empty board does.
    #
    # Nothing here edits a row. `directory.estimates` cannot write, and the
    # resolution is a human reading the firm's page and correcting the date or
    # the region on it.
    for line in estimates.contradiction_report():
        lines.append(
            f"⚠ a stated date the board contradicts (check the firm's own "
            f"page): {line}"
        )
    return lines
