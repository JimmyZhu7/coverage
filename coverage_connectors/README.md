# coverage_connectors

The deterministic ATS fetch/verify engine — a pure Python, multi-tenant-ready
library for three applicant-tracking systems:

- **Greenhouse** (`boards-api.greenhouse.io` JSON)
- **Lever** (`api.lever.co` JSON)
- **Workday** (the `wday/cxs/.../jobs` paginated JSON endpoint — the
  dominant provider in the original codebase's own board list)

Extracted from a single-user codebase's `sources.py` / `verify_rows.py`
(see `docs/build-plan.md` §4/§7 and `docs/existing-system.md` §5 for the
design this ports). Every board fetch is user-agnostic — a JPMorgan
Workday board is identical for every caller — so this package ingests
broadly and runs from cron, not from any per-request path; the shared-cache
design is in `docs/build-plan.md`'s "one-fetch-serves-all-users" section.

## What this package is

A pure `fetch(board) -> list[Opportunity]` / `verify(url) -> VerificationResult`
library. No board list of its own, no state, no filtering, no filesystem
paths, no database driver:

```python
from coverage_connectors import GreenhouseBoard, WorkdayBoard, LeverBoard, fetch_many, verify

boards = [
    GreenhouseBoard(firm="TPG", token="tpgcareers"),
    WorkdayBoard(firm="Citi", tenant_host="citi.wd5",
                 site="Citi_Early_Careers_Events_Site"),
    LeverBoard(firm="Palantir", org="palantir"),
]
for result in fetch_many(boards):
    if not result.ok:
        print(f"{result.board.firm}: fetch failed — {result.error}")
        continue
    for opp in result.opportunities:
        print(opp.firm, opp.title, opp.url)

status = verify(opp.url)  # "verified-open" | "closed" | "unreachable" | "needs-verification"
```

The `Connector` protocol (see `models.py`) is what every provider module
conforms to — a module-level `name`, `fetch(board) -> FetchResult`, and
`verify(url) -> VerificationResult`. A future connector (Oracle Recruiting
Cloud, tal.net, HSBC's sitemap, ...) registers in `__init__.py`'s
`CONNECTORS` dict and plugs in the same way, with no base class to
subclass.

## What this package deliberately does NOT do

- **No board list of its own.** Boards come in as an explicit argument
  (`GreenhouseBoard` / `LeverBoard` / `WorkdayBoard`). No `_ATS_BOARDS`-
  equivalent constant, no YAML/JSON firm directory read from disk.
- **No state, no dedup, no "what's new since last time."** Every call is a
  fresh network fetch. No `*_state.json`, no first-seen ledger. Persistence
  and diffing belong to Coverage's ingest layer.
- **No role/region/taxonomy filtering.** `fetch()` returns every posting
  the board reports; a caller filters the returned list itself.
- **No absolute filesystem paths, anywhere** — see
  `tests/test_no_state_or_paths.py`, which greps the package for exactly
  this and fails the build if it finds one.
- **No Django, no Flask, no DB driver.** Just `urllib` and stdlib `json`.

## Two real bugs found and fixed while re-verifying live

Both documented in `workday.py`'s module docstring and covered by
regression tests:

1. The original built posting URLs as `tenant_host + externalPath`,
   omitting the career-site slug — every Workday-sourced URL 404s.
2. The original's URL classifier truncated a job path at its first internal
   slash, causing a live posting's own detail-endpoint lookup to come back
   empty — a **false "closed" verdict on a genuinely live posting**.

Neither is carried forward. See the extraction report (delivered alongside
this package) for the live evidence.

## Testing

```
uv run pytest tests/                    # 39 offline unit tests, no network
RUN_LIVE=1 uv run pytest tests/ -m live # + 6 live smoke tests, real boards
```

Unit tests run against captured JSON fixtures in `tests/fixtures/` (real
responses, saved once) so they're deterministic and never touch the
network. Live smoke tests hit `tpgcareers` (Greenhouse), `palantir`
(Lever), and `citi.wd5` (Workday) — real, currently-active public boards —
and skip cleanly when `RUN_LIVE` is unset, so CI without network still
passes.

## Not ported (follow-up, not built here)

Oracle Recruiting Cloud (J.P. Morgan), tal.net (BofA, Morgan Stanley),
HSBC's sitemap, and the Amazon/Tencent/ByteDance company-specific
fetchers, plus the discovery/change-monitor channels
(`discovery.py`-equivalent). See the extraction report for why these three
providers were prioritized.
