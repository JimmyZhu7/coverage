"""Unit tests for the ingest upsert / closed-detection / reopen logic.

No live network: `directory.ingest.fetch_many` is monkeypatched to return
crafted `FetchResult`s built from real connector `Opportunity` dataclasses, so
these exercise the DB upsert path exactly as a real scrape would drive it.
"""

from __future__ import annotations

from datetime import date

import pytest

from coverage_connectors import FetchResult, GreenhouseBoard, WorkdayBoard
from coverage_connectors.models import Opportunity as ConnOpp
from coverage_connectors.workday import _normalize as workday_normalize

from directory import ingest
from directory.models import Firm, Opportunity, OpportunityChange, ScrapeRun

BOARD = GreenhouseBoard(firm="William Blair", token="williamblair")
U1 = "https://boards.greenhouse.io/williamblair/jobs/1"
U2 = "https://boards.greenhouse.io/williamblair/jobs/2"


def _opp(url, *, title="Summer Analyst", location="Chicago", deadline=None, firm="William Blair"):
    return ConnOpp(firm=firm, title=title, location=location, url=url, source="greenhouse", deadline=deadline)


def _result(opps, *, board=BOARD, ok=True, error=None):
    return FetchResult(board=board, ok=ok, opportunities=list(opps), raw_count=len(list(opps)), error=error)


def _patch(monkeypatch, results):
    monkeypatch.setattr(ingest, "fetch_many", lambda *a, **k: results)


@pytest.mark.django_db
def test_first_scrape_creates_rows_and_records_run(monkeypatch):
    _patch(monkeypatch, [_result([_opp(U1), _opp(U2)])])
    run = ingest.ingest_boards([BOARD], label="greenhouse")

    assert Opportunity.objects.count() == 2
    assert run.status == "ok"
    assert run.stats["created"] == 2
    assert run.finished is not None
    assert ScrapeRun.objects.count() == 1
    o = Opportunity.objects.get(url=U1)
    assert o.status == "open"
    assert o.source == "greenhouse"
    assert o.content_hash != ""
    assert o.last_verified is not None and o.last_checked is not None
    # Ingest derives the role bucket from the title (default _opp title is
    # "Summer Analyst") — this is the classification seam the calendar's
    # role filter depends on.
    assert o.bucket == "internship"


@pytest.mark.django_db
def test_ingest_stamps_bucket_and_cohort(monkeypatch):
    _patch(
        monkeypatch,
        [_result([
            _opp(U1, title="2027 Summer Analyst Program"),
            _opp(U2, title="Vice President, Fund Finance"),
        ])],
    )
    ingest.ingest_boards([BOARD], label="greenhouse")

    campus_role = Opportunity.objects.get(url=U1)
    assert campus_role.bucket == "internship"
    assert campus_role.cohort == "2027"     # derived from the title, connector gave none

    experienced = Opportunity.objects.get(url=U2)
    assert experienced.bucket == "other"
    assert experienced.cohort == ""

    # …and neither row gets a class year, because neither title stated one.
    # A 2027 programme is not a "Class of 2027" role.
    assert campus_role.class_year == ""
    assert experienced.class_year == ""


@pytest.mark.django_db
def test_ingest_stamps_class_year_only_when_stated(monkeypatch):
    """`class_year` is the graduation year a posting names outright, and it is
    a separate column from `cohort` for a reason: this real board title
    carries a 2027 programme year AND a 2028 class year at once."""
    _patch(
        monkeypatch,
        [_result([
            _opp(U1, title="2027 Summer Intern, Markets (Class of 2028)"),
            _opp(U2, title="2027 Summer Analyst Program"),
        ])],
    )
    ingest.ingest_boards([BOARD], label="greenhouse")

    stated = Opportunity.objects.get(url=U1)
    assert stated.cohort == "2027"       # programme/intake year
    assert stated.class_year == "2028"   # stated graduation year — never derived

    programme_only = Opportunity.objects.get(url=U2)
    assert programme_only.cohort == "2027"
    assert programme_only.class_year == ""


@pytest.mark.django_db
def test_campus_board_promotes_neutral_titles(monkeypatch):
    # On a campus-scoped board (token says "students"), a plain Analyst
    # posting classifies as entry_level; on a general board it stays other.
    campus_board = GreenhouseBoard(firm="Solomon Partners", token="solomonpartnersstudentsgraduates")
    url = "https://boards.greenhouse.io/solomonpartners/jobs/9"
    _patch(monkeypatch, [_result(
        [_opp(url, title="Investment Banking Analyst", firm="Solomon Partners")],
        board=campus_board,
    )])
    ingest.ingest_boards([campus_board], label="greenhouse")
    assert Opportunity.objects.get(url=url).bucket == "entry_level"

    _patch(monkeypatch, [_result([_opp(U1, title="Investment Banking Analyst")])])
    ingest.ingest_boards([BOARD], label="greenhouse")
    assert Opportunity.objects.get(url=U1).bucket == "other"


@pytest.mark.django_db
def test_reclassify_backfills_existing_rows(monkeypatch):
    from django.core.management import call_command

    _patch(monkeypatch, [_result([
        _opp(U1, title="Graduate Analyst Programme 2026"),
        _opp(U2, title="Analyst Programme 2026 (Class of 2027)"),
    ])])
    ingest.ingest_boards([BOARD], label="greenhouse")
    # Simulate a pre-classifier row: blank out the derived fields.
    Opportunity.objects.filter(url__in=[U1, U2]).update(bucket="", cohort="", class_year="")

    call_command("reclassify")
    o = Opportunity.objects.get(url=U1)
    assert o.bucket == "entry_level"
    assert o.cohort == "2026"
    assert o.class_year == ""     # a programme year is not a class year
    # `class_year` is re-derived too, so a row scraped before the column
    # existed picks up a stated class year without waiting for the posting to
    # change (which is what would otherwise re-trigger an ingest write).
    assert Opportunity.objects.get(url=U2).class_year == "2027"


@pytest.mark.django_db
def test_rerun_is_idempotent_no_duplicates(monkeypatch):
    _patch(monkeypatch, [_result([_opp(U1), _opp(U2)])])
    ingest.ingest_boards([BOARD], label="greenhouse")
    first = Opportunity.objects.get(url=U1)
    first_seen, last_checked = first.first_seen, first.last_checked

    run2 = ingest.ingest_boards([BOARD], label="greenhouse")

    assert Opportunity.objects.count() == 2  # no duplication on the (firm, url) key
    first.refresh_from_db()
    assert first.first_seen == first_seen  # stamped once, never moves
    assert first.last_checked >= last_checked  # refreshed every run
    assert run2.stats["created"] == 0
    assert run2.stats["unchanged"] == 2
    assert run2.stats["closed"] == 0


@pytest.mark.django_db
def test_disappeared_posting_is_closed_then_reopened(monkeypatch):
    both = [_result([_opp(U1), _opp(U2)])]
    _patch(monkeypatch, both)
    ingest.ingest_boards([BOARD], label="greenhouse")

    _patch(monkeypatch, [_result([_opp(U1)])])  # U2 no longer returned
    run = ingest.ingest_boards([BOARD], label="greenhouse")
    assert Opportunity.objects.get(url=U2).status == "closed"
    assert Opportunity.objects.get(url=U1).status == "open"
    assert run.stats["closed"] == 1

    _patch(monkeypatch, both)  # U2 reappears
    run3 = ingest.ingest_boards([BOARD], label="greenhouse")
    assert Opportunity.objects.get(url=U2).status == "open"
    assert run3.stats["reopened"] == 1
    assert run3.stats["closed"] == 0


@pytest.mark.django_db
def test_failed_board_never_closes_its_postings(monkeypatch):
    _patch(monkeypatch, [_result([_opp(U1), _opp(U2)])])
    ingest.ingest_boards([BOARD], label="greenhouse")

    _patch(monkeypatch, [_result([], ok=False, error="HTTP 503 from boards-api")])
    run = ingest.ingest_boards([BOARD], label="greenhouse")

    assert Opportunity.objects.get(url=U1).status == "open"
    assert Opportunity.objects.get(url=U2).status == "open"
    assert run.stats["closed"] == 0
    assert run.status == "error"  # the only board failed
    assert "HTTP 503" in run.error


@pytest.mark.django_db
def test_empty_but_successful_board_does_not_mass_close(monkeypatch):
    """A 200-OK fetch that suddenly returns ZERO rows for a firm with live
    postings is treated as a suspected shape change, not a mass closing:
    nothing is auto-closed, and the anomaly is recorded in stats["errors"].
    (`reverify` liveness-checks the URLs individually, so a genuine mass
    closing still converges to closed without this wipe.)"""
    _patch(monkeypatch, [_result([_opp(U1), _opp(U2)])])
    ingest.ingest_boards([BOARD], label="greenhouse")

    _patch(monkeypatch, [_result([])])  # live board, lists nothing now
    run = ingest.ingest_boards([BOARD], label="greenhouse")

    assert Opportunity.objects.filter(status="closed").count() == 0
    assert run.stats["closed"] == 0
    assert any("suspected shape change" in e["error"] for e in run.stats["errors"])


@pytest.mark.django_db
def test_partial_shrink_still_closes_the_missing_rows(monkeypatch):
    """The wipe guard only triggers on a FULL zero-row fetch. A board that
    still returns some rows closes the ones it no longer lists, as before."""
    _patch(monkeypatch, [_result([_opp(U1), _opp(U2)])])
    ingest.ingest_boards([BOARD], label="greenhouse")

    _patch(monkeypatch, [_result([_opp(U1)])])  # U2 disappeared
    run = ingest.ingest_boards([BOARD], label="greenhouse")

    assert Opportunity.objects.get(url=U2).status == "closed"
    assert Opportunity.objects.get(url=U1).status == "open"
    assert run.stats["closed"] == 1


@pytest.mark.django_db
def test_content_change_updates_in_place_and_bumps_hash(monkeypatch):
    _patch(monkeypatch, [_result([_opp(U1, title="Summer Analyst")])])
    ingest.ingest_boards([BOARD], label="greenhouse")
    h1 = Opportunity.objects.get(url=U1).content_hash

    _patch(monkeypatch, [_result([_opp(U1, title="Summer Analyst — IBD")])])
    run = ingest.ingest_boards([BOARD], label="greenhouse")

    o = Opportunity.objects.get(url=U1)
    assert o.title == "Summer Analyst — IBD"
    assert o.content_hash != h1
    assert run.stats["updated"] == 1
    assert run.stats["unchanged"] == 0
    assert Opportunity.objects.count() == 1


@pytest.mark.django_db
def test_firm_resolved_to_existing_row_not_forked(monkeypatch):
    firm = Firm.objects.create(slug="williamblair", name="William Blair")
    _patch(monkeypatch, [_result([_opp(U1, deadline="2027-01-15")])])
    ingest.ingest_boards([BOARD], label="greenhouse")

    assert Firm.objects.count() == 1  # matched by name, not duplicated
    o = Opportunity.objects.get(url=U1)
    assert o.firm_id == firm.id
    assert o.deadline == date(2027, 1, 15)
    assert o.deadline_precision == "day"


@pytest.mark.django_db
def test_firm_name_collision_resolves_to_the_same_row_every_run(monkeypatch):
    """`Firm.name` carries no DB-level uniqueness — the live bug this guards:
    two rows named "TD Securities" (ids 199/207) existed simultaneously
    because a test fixture minted a second one directly against the dev DB,
    and every scrape after that landed its postings on whichever row
    `Firm.objects.filter(name__iexact=...).first()` happened to return with
    no explicit ordering — undefined per-run, so 1,338 open postings ended
    up split across both rows and rendered as duplicate cards.

    Simulates the collision directly (bypassing the resolver, the same way
    the real duplicate was minted) and asserts every subsequent scrape run
    resolves to the SAME row — the lower id — rather than alternating."""
    older = Firm.objects.create(slug="williamblair", name="William Blair")
    newer = Firm.objects.create(slug="williamblair-dup", name="William Blair")
    assert older.id < newer.id

    for _ in range(3):
        _patch(monkeypatch, [_result([_opp(U1)])])
        ingest.ingest_boards([BOARD], label="greenhouse")

    assert Firm.objects.count() == 2  # the collision itself is not merged here
    assert Opportunity.objects.count() == 1
    o = Opportunity.objects.get(url=U1)
    assert o.firm_id == older.id  # always the older row, never `newer`
    assert Opportunity.objects.filter(firm=newer).count() == 0


@pytest.mark.django_db
def test_null_deadline_stored_as_null(monkeypatch):
    _patch(monkeypatch, [_result([_opp(U1, deadline=None)])])
    ingest.ingest_boards([BOARD], label="greenhouse")
    o = Opportunity.objects.get(url=U1)
    assert o.deadline is None
    assert o.deadline_precision == ""


@pytest.mark.django_db
def test_malformed_deadline_fails_loudly_instead_of_becoming_no_deadline(monkeypatch):
    """C11: a provider date that doesn't parse must not silently become the
    same `None` used for "the provider stated no deadline at all" —
    `views.deadline_marker` renders a null deadline as the affirmative claim
    "No date posted" (standardized 2026-09-01 to match the feed's own
    wording; it read "No deadline posted" here before that), which would
    misrepresent a parse FAILURE as a stated fact about the posting. The
    failure must be counted AND surfaced in `ScrapeRun.error`."""
    _patch(monkeypatch, [_result([_opp(U1, deadline="not-a-real-date")])])
    run = ingest.ingest_boards([BOARD], label="greenhouse")

    o = Opportunity.objects.get(url=U1)
    # Still degrades to a null deadline (never a fabricated date) — but the
    # degradation is now loud, not silent.
    assert o.deadline is None
    assert run.stats["deadline_parse_failed"] == 1
    assert any("unparseable deadline" in e["error"] for e in run.stats["errors"])
    assert "unparseable deadline" in (run.error or "")


@pytest.mark.django_db
def test_unseeded_firm_is_autocreated(monkeypatch):
    _patch(monkeypatch, [_result([_opp(U1)])])
    run = ingest.ingest_boards([BOARD], label="greenhouse")
    assert Firm.objects.filter(name="William Blair").exists()
    assert run.stats["created_firms"] == ["william-blair"]


@pytest.mark.django_db
def test_per_row_error_does_not_close_an_existing_open_row(monkeypatch):
    """A transient upsert error on a row the fetch DID return live must not
    make closed-detection flip that existing open row to closed — the url
    stays in `seen` (regression for the inverted-discard bug)."""
    _patch(monkeypatch, [_result([_opp(U1), _opp(U2)])])
    ingest.ingest_boards([BOARD], label="greenhouse")
    assert Opportunity.objects.filter(status="open").count() == 2

    # Next run returns both again, but U2's upsert raises mid-apply.
    real_apply = ingest._apply_opportunity

    def flaky_apply(firm, opp, now, stats, **kw):
        if opp.url == U2:
            raise RuntimeError("transient")
        return real_apply(firm, opp, now, stats, **kw)

    monkeypatch.setattr(ingest, "_apply_opportunity", flaky_apply)
    _patch(monkeypatch, [_result([_opp(U1), _opp(U2)])])
    run = ingest.ingest_boards([BOARD], label="greenhouse")

    # U2 errored but was returned live → must remain open, not closed.
    assert Opportunity.objects.get(url=U2).status == "open"
    assert run.stats["closed"] == 0
    assert any("row failed" in e["error"] for e in run.stats["errors"])


# ---------------------------------------------------------------------------
# closed_at + raw: the two pieces of evidence ingest used to throw away.
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_a_close_is_timestamped_and_a_reopen_clears_it(monkeypatch):
    """The invariant: `status == "closed"` iff `closed_at` is set.

    The scraper had been flipping rows closed daily and recording nothing —
    the one dataset that could turn this cycle's observations into next
    cycle's deadline estimates ("this firm's postings actually die in
    mid-September") was measured and discarded every run.
    """
    _patch(monkeypatch, [_result([_opp(U1), _opp(U2)])])
    ingest.ingest_boards([BOARD], label="greenhouse")

    # Next fetch: U2 is gone -> closed, with the moment recorded.
    _patch(monkeypatch, [_result([_opp(U1)])])
    ingest.ingest_boards([BOARD], label="greenhouse")
    gone = Opportunity.objects.get(url=U2)
    assert gone.status == "closed"
    assert gone.closed_at is not None

    # It comes back -> open again, and the close timestamp clears with it.
    _patch(monkeypatch, [_result([_opp(U1), _opp(U2)])])
    ingest.ingest_boards([BOARD], label="greenhouse")
    back = Opportunity.objects.get(url=U2)
    assert back.status == "open"
    assert back.closed_at is None


@pytest.mark.django_db
def test_the_providers_raw_json_is_stored_verbatim(monkeypatch):
    """What the API said, kept as evidence — extraction can now be improved
    and re-run over rows fetched long ago instead of being lost at fetch
    time (sponsorship text, Workday's real city list, posted dates)."""
    rich = ConnOpp(
        firm="William Blair", title="Summer Analyst", location="Chicago",
        url=U1, source="greenhouse", posted_at="2026-07-30",
        raw={"id": 123, "content": "We are unable to sponsor visas."},
    )
    _patch(monkeypatch, [_result([rich])])
    ingest.ingest_boards([BOARD], label="greenhouse")

    o = Opportunity.objects.get(url=U1)
    assert o.raw == {"id": 123, "content": "We are unable to sponsor visas."}
    assert o.posted_at == "2026-07-30"


@pytest.mark.django_db
def test_an_api_deadline_sets_full_confidence(monkeypatch):
    """`confidence` was a dead field — 0.0 on all 4,319 scraped rows. It now
    means one thing: how sure are we of the DATE. A deadline from the
    provider's own API field is the firm's own statement -> 1.0. A row with
    no date keeps 0.0, which renders as unrated, honestly."""
    _patch(monkeypatch, [_result([_opp(U1, deadline="2026-09-15"), _opp(U2)])])
    ingest.ingest_boards([BOARD], label="greenhouse")

    assert Opportunity.objects.get(url=U1).confidence == 1.0
    assert Opportunity.objects.get(url=U2).confidence == 0.0


@pytest.mark.django_db
def test_a_weaker_prose_reading_does_not_inherit_the_old_apis_confidence(monkeypatch):
    """Once a real API deadline is on file (confidence 1.0), a later run whose
    API field has gone silent must not relabel this run's own 0.6-confidence
    PROSE guess as if it were still the firm's own API statement just because
    the row once held a 1.0. `existing.confidence = max(existing.confidence,
    final_confidence)` keeps the OLD row's 1.0 even when the DATE it is
    stamped on just changed to a completely different value read out of prose
    this run, not the provider's API field."""
    from coverage_connectors.models import Opportunity as ConnOpp

    _patch(monkeypatch, [_result([_opp(U1, deadline="2026-09-30")])])
    ingest.ingest_boards([BOARD], label="greenhouse")
    assert Opportunity.objects.get(url=U1).confidence == 1.0

    reread = ConnOpp(
        firm="William Blair", title="Summer Analyst", location="Chicago",
        url=U1, source="greenhouse", deadline=None,
        raw={"content": "Applications close November 16, 2026."},
    )
    _patch(monkeypatch, [_result([reread])])
    ingest.ingest_boards([BOARD], label="greenhouse")

    o = Opportunity.objects.get(url=U1)
    assert o.deadline == date(2026, 11, 16), "sanity: the prose reading did land"
    assert o.confidence == 0.6, (
        "the date now on the row came from THIS run's prose reading, not "
        "the provider's API field -- confidence must say so"
    )


# ---------------------------------------------------------------------------
# WHAT A RE-SCRAPE MUST NOT DESTROY.
#
# These pin the most expensive bug this app has shipped. `enrich_postings`
# visits each posting's own page — one HTTP request apiece, ~20 minutes for a
# full pass — and banks the description, the deadline read out of it, and the
# facts derived from that text. Ingest then overwrote `raw` wholesale on every
# scrape, so the next nightly run erased all of it: 854 descriptions to 0, 854
# fact sets to 0, 121 deadlines to 29, silently, six hours after the first
# enrichment run finished.
#
# The rule under test: a scrape may add and may correct, but silence in a list
# payload may never erase an answer. Those endpoints have never carried a
# deadline, so "no deadline in the payload" is not evidence of anything.
# ---------------------------------------------------------------------------

def _enriched(url, **extra):
    """A row as `enrich_postings` + `extract_facts` leave it."""
    o = Opportunity.objects.get(url=url)
    o.raw = {**(o.raw or {}),
             "detail_text": "Applications close on 30 September 2026.",
             "detail_fetched": True,
             "facts": {"gpa": {"value": "3.5", "phrase": "minimum GPA of 3.5"}},
             "facts_at": "2026-08-05T12:00:00+00:00"}
    for k, v in extra.items():
        setattr(o, k, v)
    o.save()
    return o


@pytest.mark.django_db
def test_a_rescrape_keeps_the_description_it_did_not_fetch(monkeypatch):
    _patch(monkeypatch, [_result([_opp(U1)])])
    ingest.ingest_boards([BOARD], label="greenhouse")
    _enriched(U1)

    # The identical posting comes back tomorrow: same title, same everything,
    # and a payload that has never carried a description.
    _patch(monkeypatch, [_result([_opp(U1)])])
    ingest.ingest_boards([BOARD], label="greenhouse")

    raw = Opportunity.objects.get(url=U1).raw
    assert raw["detail_text"].startswith("Applications close")
    assert raw["facts"]["gpa"]["value"] == "3.5"
    assert raw["detail_fetched"] is True


@pytest.mark.django_db
def test_a_rescrape_keeps_a_deadline_the_payload_never_carried(monkeypatch):
    from datetime import date

    _patch(monkeypatch, [_result([_opp(U1)])])
    ingest.ingest_boards([BOARD], label="greenhouse")
    _enriched(U1, deadline=date(2026, 9, 30), deadline_precision="day", confidence=0.6)

    _patch(monkeypatch, [_result([_opp(U1)])])
    ingest.ingest_boards([BOARD], label="greenhouse")

    o = Opportunity.objects.get(url=U1)
    assert o.deadline == date(2026, 9, 30), "silence is not a withdrawal"
    assert o.confidence == 0.6


@pytest.mark.django_db
def test_a_rescrape_keeps_a_sponsorship_answer_read_from_the_posting(monkeypatch):
    _patch(monkeypatch, [_result([_opp(U1)])])
    ingest.ingest_boards([BOARD], label="greenhouse")
    _enriched(U1, sponsorship="no")

    _patch(monkeypatch, [_result([_opp(U1)])])
    ingest.ingest_boards([BOARD], label="greenhouse")

    assert Opportunity.objects.get(url=U1).sponsorship == "no"


@pytest.mark.django_db
def test_a_changed_posting_drops_our_reading_of_the_old_one(monkeypatch):
    """The other half of the contract. When the posting's own content moves,
    a description we cached describes something else and a deadline we read
    out of it is unverified — so both go, and the row returns to the
    enrichment queue (whose queue is exactly "rows with no detail_text")."""
    from datetime import date

    _patch(monkeypatch, [_result([_opp(U1)])])
    ingest.ingest_boards([BOARD], label="greenhouse")
    _enriched(U1, deadline=date(2026, 9, 30), deadline_precision="day", confidence=0.6)

    _patch(monkeypatch, [_result([_opp(U1, title="Off-Cycle Analyst")])])
    ingest.ingest_boards([BOARD], label="greenhouse")

    o = Opportunity.objects.get(url=U1)
    assert "detail_text" not in o.raw
    assert o.deadline is None
    assert o.confidence == 0.0


@pytest.mark.django_db
def test_a_changed_posting_keeps_a_deadline_the_provider_states(monkeypatch):
    """A provider's own field is not our reading of anything, so a change to
    the posting is no reason to drop it — it is restated by this very fetch."""
    from datetime import date

    _patch(monkeypatch, [_result([_opp(U1, deadline="2026-09-15")])])
    ingest.ingest_boards([BOARD], label="greenhouse")
    _patch(monkeypatch, [_result([_opp(U1, title="Renamed", deadline="2026-09-15")])])
    ingest.ingest_boards([BOARD], label="greenhouse")

    o = Opportunity.objects.get(url=U1)
    assert o.deadline == date(2026, 9, 15)
    assert o.confidence == 1.0


# ---------------------------------------------------------------------------
# CONFIDENCE BELONGS TO THE DATE IN THE COLUMN.
#
# `confidence` answers one question — how sure are we that the STORED day is
# the one the firm holds — so when the stored day is replaced, the answer is
# replaced with it. The upsert used `max(existing, incoming)` for both cases,
# which is right for one of them and a laundering machine for the other: a
# 0.6 prose reading that supersedes a 1.0 board-published date inherited the
# 1.0, and `deadline_provenance` / `crm.calendar_views._is_reported` / the
# feed's "(reported)" tag read nothing but that number.
#
# No live row shows the symptom today (all 225 rows at 1.0 carry a real
# provider deadline field in `raw`), which is why it needed finding rather
# than reporting.
# ---------------------------------------------------------------------------

def _prose(url, text, *, title="Summer Analyst"):
    """A payload with no deadline FIELD, whose text states one anyway. This
    is the shape every list endpoint has: none of them carry a deadline."""
    return ConnOpp(firm="William Blair", title=title, location="Chicago",
                   url=url, source="greenhouse", deadline=None,
                   raw={"content": text})


@pytest.mark.django_db
def test_a_prose_deadline_replacing_a_stated_one_drops_to_reported(monkeypatch):
    """The bug. Run 1: the board publishes 2026-09-15 in its own field ->
    1.0. Run 2: the field is gone and the description states a DIFFERENT
    day, which our regex reads. The row now holds a date nobody published,
    and `max()` kept the 1.0 label from the date it replaced."""
    _patch(monkeypatch, [_result([_opp(U1, deadline="2026-09-15")])])
    ingest.ingest_boards([BOARD], label="greenhouse")
    assert Opportunity.objects.get(url=U1).confidence == 1.0

    _patch(monkeypatch, [_result([
        _prose(U1, "Applications close on 30 October 2026.")])])
    ingest.ingest_boards([BOARD], label="greenhouse")

    o = Opportunity.objects.get(url=U1)
    assert o.deadline == date(2026, 10, 30), "the new date is the prose one"
    assert o.confidence == 0.6, (
        "a date we read out of a paragraph may not wear the label of the "
        "board-published date it replaced")
    # The reader that made this a lie on the page, exercised directly.
    from directory.views import deadline_provenance
    assert deadline_provenance(o) is not None
    assert deadline_provenance(o)["label"] == "reported"


@pytest.mark.django_db
def test_seeing_the_same_stated_date_again_in_prose_keeps_full_confidence(monkeypatch):
    """The case `max()` was written for, and it has to survive the fix. The
    board's field goes quiet but the description states the SAME day: the
    date has not moved, so neither has how sure we are of it. Downgrading
    here would walk every stated date to 0.6 on the next scrape."""
    _patch(monkeypatch, [_result([_opp(U1, deadline="2026-09-15")])])
    ingest.ingest_boards([BOARD], label="greenhouse")

    _patch(monkeypatch, [_result([
        _prose(U1, "Applications close on 15 September 2026.")])])
    ingest.ingest_boards([BOARD], label="greenhouse")

    o = Opportunity.objects.get(url=U1)
    assert o.deadline == date(2026, 9, 15)
    assert o.confidence == 1.0
    from directory.views import deadline_provenance
    assert deadline_provenance(o) is None


@pytest.mark.django_db
def test_a_stated_date_replacing_a_prose_one_is_promoted(monkeypatch):
    """The other direction, which `max()` also got right and which must
    still work: the board starts publishing the field, so the row stops
    being our reading of anything."""
    _patch(monkeypatch, [_result([
        _prose(U1, "Applications close on 30 October 2026.")])])
    ingest.ingest_boards([BOARD], label="greenhouse")
    assert Opportunity.objects.get(url=U1).confidence == 0.6

    _patch(monkeypatch, [_result([_opp(U1, deadline="2026-09-15")])])
    ingest.ingest_boards([BOARD], label="greenhouse")

    o = Opportunity.objects.get(url=U1)
    assert o.deadline == date(2026, 9, 15)
    assert o.confidence == 1.0


@pytest.mark.django_db
def test_reingesting_the_same_posting_does_not_drift_confidence(monkeypatch):
    """Idempotence, over both bands. Ingest is a command and may write; what
    it may not do is move a number a little further every pass."""
    for payload, want_date, want_conf in (
        (lambda: _opp(U1, deadline="2026-09-15"), date(2026, 9, 15), 1.0),
        (lambda: _prose(U2, "Applications close on 30 October 2026."),
         date(2026, 10, 30), 0.6),
    ):
        for _ in range(3):
            _patch(monkeypatch, [_result([payload()])])
            ingest.ingest_boards([BOARD], label="greenhouse")
        o = Opportunity.objects.get(url=payload().url)
        assert (o.deadline, o.confidence, o.deadline_precision) == (
            want_date, want_conf, "day")


# ---------------------------------------------------------------------------
# A TRUNCATED fetch must not close anything.
#
# Closed-detection infers "gone from the board" from "absent from the fetch",
# which is only valid when the fetch WAS the board. Workday caps its paging,
# so on boards reporting 186-1,371 results every row past the cap looked
# closed — and two sampled from that population came back verified-open from
# the firms' own sites while the database called them closed.
# ---------------------------------------------------------------------------


def _truncated(opps, *, board=BOARD):
    """A successful fetch that knows it did not read the whole board."""
    return FetchResult(board=board, ok=True, opportunities=list(opps),
                       raw_count=999, truncated=True)


@pytest.mark.django_db
def test_a_truncated_fetch_never_closes_the_rows_it_could_not_see(monkeypatch):
    _patch(monkeypatch, [_result([_opp(U1), _opp(U2)])])
    ingest.ingest_boards([BOARD], label="greenhouse")
    assert Opportunity.objects.filter(status="open").count() == 2

    # Next run reads only the first page. U2 is still live on the board; the
    # fetch simply never got that far.
    _patch(monkeypatch, [_truncated([_opp(U1)])])
    run = ingest.ingest_boards([BOARD], label="greenhouse")

    assert Opportunity.objects.get(url=U2).status == "open"
    assert run.stats["closed"] == 0
    # And it is recorded rather than silently skipped: a board that stops
    # being fully readable is an operational fact worth seeing.
    assert any("partial list" in e.get("error", "") for e in run.stats["errors"])


@pytest.mark.django_db
def test_a_truncated_fetch_still_upserts_the_rows_it_did_see(monkeypatch):
    """The fetch succeeded. Its rows are good — only the inference from
    absence is off-limits."""
    _patch(monkeypatch, [_truncated([_opp(U1, title="Summer Analyst 2028")])])
    ingest.ingest_boards([BOARD], label="greenhouse")
    assert Opportunity.objects.get(url=U1).title == "Summer Analyst 2028"


@pytest.mark.django_db
def test_a_complete_fetch_still_closes_what_it_dropped(monkeypatch):
    """The guard must not disarm ordinary closed-detection — a board that
    read to the end and no longer lists a posting has genuinely dropped it."""
    _patch(monkeypatch, [_result([_opp(U1), _opp(U2)])])
    ingest.ingest_boards([BOARD], label="greenhouse")

    _patch(monkeypatch, [_result([_opp(U1)])])
    run = ingest.ingest_boards([BOARD], label="greenhouse")

    assert Opportunity.objects.get(url=U2).status == "closed"
    assert run.stats["closed"] == 1


# ---------------------------------------------------------------------------
# ...AND WHERE THE POSTING IS.
#
# The same rule as the block above, on the two location columns, pinned after
# it cost 514 rows. Workday's list payload states each posting's place in
# `locationsText`, and a tenant is free not to send the key at all: Raymond
# James's early-careers site sends `title`, `externalPath`, `postedOn` and
# `bulletFields`, and nothing else. Ingest read `opp.location` as "", derived
# `normalize_region("")` -> "", and wrote both over the stored values on every
# re-scrape — 225 of 225 open Raymond James rows went from `us` to blank in
# one pass, with nothing in `OpportunityChange` to say it had happened.
#
# The fixtures below are that payload, read through the real Workday connector
# so the shape under test is the provider's rather than one this test invented.
# ---------------------------------------------------------------------------

RJ_BOARD = WorkdayBoard(firm="Raymond James", tenant_host="raymondjames.wd1",
                        site="RaymondJamesEarlyCareers", search_text="")

#: A Raymond James posting exactly as its CxS search endpoint files it: no
#: `locationsText` key anywhere in the job dict.
_RJ_JOB = {
    "title": "2027 Equity Research Associate",
    "externalPath": "/job/Saint-Petersburg-Florida---United-States/"
                    "XMLNAME-2027-Equity-Research-Associate_R-0012398",
    "postedOn": "Posted 3 Days Ago",
    "bulletFields": ["R-0012398"],
}
#: The same posting on a day the tenant does state its place.
_RJ_JOB_STATED = {**_RJ_JOB,
                  "locationsText": "Saint Petersburg Florida   United States"}
#: What the connector's normalizer makes of that run: the double space is the
#: field boundary Workday leaves behind for an empty slot, the single one is
#: part of the city's own name.
_RJ_STATED_LOCATION = "Saint Petersburg Florida, United States"


def _workday_opp(job: dict, board=RJ_BOARD):
    """The connector's own reading of one job dict — including its silence."""
    return workday_normalize(job, board)


@pytest.mark.django_db
def test_a_workday_tenant_that_sends_no_location_keeps_the_region_it_had(monkeypatch):
    """The Raymond James case. The first fetch states the place; the second is
    the same tenant on a day its payload carries no `locationsText` at all. An
    empty answer from one run is not evidence that the previous run's real
    answer was wrong."""
    _patch(monkeypatch, [_result([_workday_opp(_RJ_JOB_STATED)], board=RJ_BOARD)])
    ingest.ingest_boards([RJ_BOARD], label="workday")

    o = Opportunity.objects.get(url__contains="R-0012398")
    assert o.region == "us", "sanity: the stated location placed the row"

    _patch(monkeypatch, [_result([_workday_opp(_RJ_JOB)], board=RJ_BOARD)])
    ingest.ingest_boards([RJ_BOARD], label="workday")

    assert Opportunity.objects.get(pk=o.pk).region == "us"


@pytest.mark.django_db
def test_the_same_silence_does_not_blank_the_location_text(monkeypatch):
    """`location` carried the identical unconditional overwrite. It is the
    field the student actually reads, and the one any later repair of `region`
    has to work back from."""
    _patch(monkeypatch, [_result([_workday_opp(_RJ_JOB_STATED)], board=RJ_BOARD)])
    ingest.ingest_boards([RJ_BOARD], label="workday")

    o = Opportunity.objects.get(url__contains="R-0012398")
    assert o.location == _RJ_STATED_LOCATION

    _patch(monkeypatch, [_result([_workday_opp(_RJ_JOB)], board=RJ_BOARD)])
    ingest.ingest_boards([RJ_BOARD], label="workday")

    assert Opportunity.objects.get(pk=o.pk).location == _RJ_STATED_LOCATION


@pytest.mark.django_db
def test_a_payload_that_states_a_place_still_moves_the_row(monkeypatch):
    """The other half of the contract: keeping is for SILENCE only. A posting
    that genuinely relocates says so, and a stated place always wins."""
    _patch(monkeypatch, [_result([_workday_opp(_RJ_JOB_STATED)], board=RJ_BOARD)])
    ingest.ingest_boards([RJ_BOARD], label="workday")

    moved = {**_RJ_JOB, "locationsText": "London  United Kingdom"}
    _patch(monkeypatch, [_result([_workday_opp(moved)], board=RJ_BOARD)])
    ingest.ingest_boards([RJ_BOARD], label="workday")

    o = Opportunity.objects.get(url__contains="R-0012398")
    assert o.location == "London, United Kingdom"
    assert o.region == "eu"


@pytest.mark.django_db
def test_a_move_between_markets_is_written_to_the_change_log(monkeypatch):
    """Region and location were absent from `OpportunityChange`, which is why
    the wipe stayed invisible for as long as it did. A move is now a row."""
    _patch(monkeypatch, [_result([_workday_opp(_RJ_JOB_STATED)], board=RJ_BOARD)])
    ingest.ingest_boards([RJ_BOARD], label="workday")

    moved = {**_RJ_JOB, "locationsText": "London  United Kingdom"}
    _patch(monkeypatch, [_result([_workday_opp(moved)], board=RJ_BOARD)])
    ingest.ingest_boards([RJ_BOARD], label="workday")

    o = Opportunity.objects.get(url__contains="R-0012398")
    region_move = OpportunityChange.objects.get(opportunity=o, field="region")
    assert (region_move.old_value, region_move.new_value) == ("us", "eu")
    location_move = OpportunityChange.objects.get(opportunity=o, field="location")
    assert location_move.old_value == _RJ_STATED_LOCATION
    assert location_move.new_value == "London, United Kingdom"


@pytest.mark.django_db
def test_a_silent_payload_records_no_move_at_all(monkeypatch):
    """A kept value is not a change and must not be reported as one — a change
    row per silent re-scrape would be a second false trail beside the one this
    fix exists to end."""
    _patch(monkeypatch, [_result([_workday_opp(_RJ_JOB_STATED)], board=RJ_BOARD)])
    ingest.ingest_boards([RJ_BOARD], label="workday")

    _patch(monkeypatch, [_result([_workday_opp(_RJ_JOB)], board=RJ_BOARD)])
    ingest.ingest_boards([RJ_BOARD], label="workday")

    assert not OpportunityChange.objects.filter(
        field__in=("region", "location")).exists()


# ---------------------------------------------------------------------------
# The office the provider sent and we never read. Measured 2026-09-02: 1,417
# of 19,315 stored Workday rows carry no `locationsText` key at all, and 429
# OPEN rows sat at `location=""` beside a `bulletFields` list that named the
# office — Raymond James 210, Accenture 162, Fidelity International 57. The
# fill is vetted by `normalize_region`, so a requisition code can never be
# stored as an office.
# ---------------------------------------------------------------------------

#: The same tenant, on a posting whose bullets DO name the office. This is
#: the majority shape at Raymond James: place first, requisition second.
_RJ_JOB_BULLETS = {
    **_RJ_JOB,
    "bulletFields": ["Saint Petersburg, Florida - United States", "R-0012398"],
}


@pytest.mark.django_db
def test_a_workday_bullet_field_fills_a_location_the_tenant_left_out(monkeypatch):
    """No `locationsText` anywhere in the payload, but the office is right
    there beside the requisition code. Before this the row stored a blank
    location and answered no Region filter at all."""
    _patch(monkeypatch,
           [_result([_workday_opp(_RJ_JOB_BULLETS)], board=RJ_BOARD)])
    ingest.ingest_boards([RJ_BOARD], label="workday")

    o = Opportunity.objects.get(url__contains="R-0012398")
    assert o.location == "Saint Petersburg, Florida - United States"
    assert o.region == "us"


@pytest.mark.django_db
def test_a_requisition_code_is_never_stored_as_an_office(monkeypatch):
    """The negative, and it is what makes reading a display list safe at all.
    `_RJ_JOB`'s bullets hold nothing but "R-0012398". A rule that took the
    first entry would have printed a requisition number on the card where the
    city goes; the vetting means the row stays honestly blank."""
    _patch(monkeypatch, [_result([_workday_opp(_RJ_JOB)], board=RJ_BOARD)])
    ingest.ingest_boards([RJ_BOARD], label="workday")

    o = Opportunity.objects.get(url__contains="R-0012398")
    assert o.location == ""


@pytest.mark.django_db
def test_the_tenants_own_location_field_still_wins(monkeypatch):
    """Fill-only, in the same posture as the no-downgrade rules above: the
    bullets are read where the connector reported nothing, never over the top
    of what it did report."""
    stated = {**_RJ_JOB_STATED,
              "bulletFields": ["London - United Kingdom", "R-0012398"]}
    _patch(monkeypatch, [_result([_workday_opp(stated)], board=RJ_BOARD)])
    ingest.ingest_boards([RJ_BOARD], label="workday")

    o = Opportunity.objects.get(url__contains="R-0012398")
    assert o.location == _RJ_STATED_LOCATION
    assert o.region == "us"
