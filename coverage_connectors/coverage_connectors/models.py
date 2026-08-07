"""Typed data shapes shared by every provider connector.

This module defines the seam every connector plugs into: a per-provider
`BoardConfig` the caller constructs and passes in, a `Connector` protocol each
provider module conforms to structurally (no base class required), and the
`Opportunity` / `FetchResult` / `VerificationResult` value objects a fetch or
verify call returns.

Deliberately absent from `Opportunity`, and why:

- No `id`, `firm_id`, `first_seen`, `last_verified`, `last_checked`, or
  `content_hash`. Those are database bookkeeping columns on the shared
  `opportunities` table (see `docs/build-plan.md` §2) that Coverage's ingest
  layer stamps on write — a pure fetch→normalize function has no database
  connection and no notion of "since when have I been ingesting this row",
  so it cannot honestly produce them.
- No `deadline_precision` or `confidence`. The original codebase computes
  those only in `confidence.py`, which scores *source trustworthiness* for
  facts an LLM research pass extracted from prose — a different pipeline
  entirely, upstream of this package. Greenhouse/Lever/Workday's own APIs
  either give you a real ISO date or they don't; this package reports
  exactly what came back and never invents a confidence band for it.
- `region` exists as a field (the build-plan schema has one) but this
  package never populates it. None of the three providers' normalize logic
  in the original computed a region from role/location text — that was a
  *filter* (`_REGION_RE`, applied to decide whether to keep a row at all),
  not a stored classification. Turning "matched a filter" into "is HK" is a
  judgment call Coverage's layer can make deliberately; guessing it here
  would be exactly the kind of unrequested precision this extraction is
  told to avoid.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


# --------------------------------------------------------------- board configs

@dataclass(frozen=True, slots=True)
class GreenhouseBoard:
    """One Greenhouse board. `token` is the path segment in
    `boards-api.greenhouse.io/v1/boards/{token}/jobs` — e.g. "williamblair"
    for https://boards-api.greenhouse.io/v1/boards/williamblair/jobs."""

    firm: str
    token: str
    provider: str = field(default="greenhouse", init=False)


@dataclass(frozen=True, slots=True)
class LeverBoard:
    """One Lever board. `org` is the path segment in
    `jobs.lever.co/{org}` / `api.lever.co/v0/postings/{org}` — e.g.
    "palantir" for https://jobs.lever.co/palantir."""

    firm: str
    org: str
    provider: str = field(default="lever", init=False)


@dataclass(frozen=True, slots=True)
class WorkdayBoard:
    """One Workday career site. `tenant_host` is "{tenant}.{datacenter}"
    (e.g. "citi.wd5" — the tenant is normally its first dot-segment); `site`
    is the career-site slug Workday calls a "site" (e.g.
    "Citi_Early_Careers_Events_Site"). `search_text` is optional
    server-side full-text filtering, ported verbatim from the original's
    per-board `search_text` config — pass "" for a board that's already
    scoped to campus/early-careers postings and needs no further filter.

    `tenant` overrides the cxs tenant when it isn't the first label of
    `tenant_host` (e.g. Golub is host "wd501" but tenant "golubcapital").
    `domain` covers tenants hosted on Workday's newer "myworkdaysite.com"
    rather than the classic "myworkdayjobs.com". Both default to the
    common case, so every existing board is unchanged."""

    firm: str
    tenant_host: str
    site: str
    search_text: str = ""
    tenant: str = ""
    domain: str = "myworkdayjobs.com"
    provider: str = field(default="workday", init=False)


@dataclass(frozen=True, slots=True)
class TalentsoftBoard:
    """A Talentsoft tenant's server-rendered all-offers list page."""

    firm: str
    origin: str          # e.g. "https://jobs.ca-cib.com"
    list_url: str        # the ?all=1 English list page
    provider: str = field(default="talentsoft", init=False)


@dataclass(frozen=True, slots=True)
class SocGenBoard:
    """Société Générale's Quantum search behind careers.societegenerale.com.
    One firm-wide board; everything else is fixed in the connector."""

    firm: str
    provider: str = field(default="socgen", init=False)


@dataclass(frozen=True, slots=True)
class LumesseBoard:
    """One Lumesse TalentLink FO-REST board. `host` is the datacenter FO
    host ("au01-foc.lumessetalentlink.com"); `tech_id` is the site's
    `lumesse-site-tech-id` from its careers page markup."""

    firm: str
    host: str
    tech_id: str
    provider: str = field(default="lumesse", init=False)


@dataclass(frozen=True, slots=True)
class OracleBoard:
    """One Oracle Recruiting Cloud career site (public, unauthenticated
    REST). `host` is the tenant host (e.g. "jpmc.fa.oraclecloud.com"),
    `site_number` the CandidateExperience site (e.g. "CX_1001"), and
    `keywords` the per-term searches to run — Oracle's finder has no
    OR-of-terms syntax that reliably widens results (the site's own search
    box works the same way), so one request per keyword, deduped by
    requisition Id. Ported from the radar's `_fetch_oracle`."""

    firm: str
    host: str
    site_number: str
    keywords: tuple[str, ...]
    provider: str = field(default="oracle", init=False)


@dataclass(frozen=True, slots=True)
class TalnetBoard:
    """One tal.net server-rendered HTML board. `board_url` is the full
    jobboard listing URL (".../candidate/jobboard/vacancy/1/adv/" for a
    firm's jobs board, ".../vacancy/2/adv/" for its events board — the two
    working paths; a `pl/2` variant 404s on every firm probed). `kind`
    labels which of the two it is, for callers that care. Ported from the
    radar's `_talnet_parse`/`_talnet_normalize`."""

    firm: str
    board_url: str
    kind: str = "jobs"
    provider: str = field(default="talnet", init=False)


@dataclass(frozen=True, slots=True)
class SitemapBoard:
    """A sitemap.xml-backed board for career sites that are JS shells with
    nothing fetchable — but whose sitemap lists every posting URL. Rows are
    kept only when the URL contains `path_filter` (e.g. HSBC's dedicated
    "/emergingtalent/job/" campus path). Ported from the radar's
    `_fetch_hsbc_sitemap`."""

    firm: str
    sitemap_url: str
    path_filter: str = "/job/"
    provider: str = field(default="sitemap", init=False)


@dataclass(frozen=True, slots=True)
class McKinseyBoard:
    """McKinsey's careers gateway JSON search (see mckinsey.py). One search
    per keyword via the API's own `q=` parameter, deduped by jobID; an
    empty keywords tuple fetches the whole board."""

    firm: str
    keywords: tuple[str, ...] = ()
    provider: str = field(default="mckinsey", init=False)


@dataclass(frozen=True, slots=True)
class PhenomBoard:
    """A Phenom People platform career site (see phenom.py). `host` is the
    site's own domain (e.g. "careers.bcg.com"); `keywords` feeds the
    refineSearch payload the site's own UI sends."""

    firm: str
    host: str
    keywords: str = ""
    locale: str = "en_global"
    provider: str = field(default="phenom", init=False)


@dataclass(frozen=True, slots=True)
class GoldmanSachsBoard:
    """Goldman Sachs' higher.gs.com careers GraphQL (see goldmansachs.py).
    Fixed campus-roles query; only the display firm name varies."""

    firm: str = "Goldman Sachs"
    provider: str = field(default="goldmansachs", init=False)


@dataclass(frozen=True, slots=True)
class TalentGatewayBoard:
    """IBM BrassRing TalentGateway board (see talentgateway.py). `partner_id`
    + `site_id` identify the specific board (a firm may run separate
    experienced and graduate boards — pass the campus one)."""

    firm: str
    partner_id: int
    site_id: int
    provider: str = field(default="talentgateway", init=False)


@dataclass(frozen=True, slots=True)
class EightfoldBoard:
    """An Eightfold.ai talent-platform career site (see eightfold.py). `host`
    is the careers API host (e.g. "career.mlp.com"); `domain` is the tenant
    key the `/api/apply/v2/jobs` endpoint filters on (e.g. "mlp.com")."""

    firm: str
    host: str
    domain: str
    provider: str = field(default="eightfold", init=False)


@dataclass(frozen=True, slots=True)
class BeisenBoard:
    """A Beisen (北森) recruiting SPA (e.g. CICC at cicc.zhiye.com) — the
    browser tier. Its job list 403s on raw HTTP because it only loads after
    the site's own JS bootstraps a session, so beisen.py drives a headless
    browser and captures the site's own GetJobAdPageList API response. `host`
    is the zhiye.com host; `pages` are the /custom/<slug> boards to sweep
    (campus / summer / project intern boards)."""

    firm: str
    host: str
    pages: tuple[str, ...] = ("campus", "summer", "project")
    provider: str = field(default="beisen", init=False)


@dataclass(frozen=True, slots=True)
class AvatureBoard:
    """An Avature-hosted career site's RSS feed (see avature.py). `feed_url`
    is the full `.../SearchJobs/feed/` URL; the connector paginates it via a
    `folderOffset` query param it appends itself."""

    firm: str
    feed_url: str
    provider: str = field(default="avature", init=False)


BoardConfig = (
    GreenhouseBoard | LeverBoard | WorkdayBoard | OracleBoard | TalnetBoard | SitemapBoard
    | McKinseyBoard | PhenomBoard | GoldmanSachsBoard | TalentGatewayBoard | EightfoldBoard
    | BeisenBoard | AvatureBoard
)


# ------------------------------------------------------------------ results

@dataclass(frozen=True, slots=True)
class Opportunity:
    """One normalized job posting. Every field is either read directly off
    the provider's own JSON or left as the type's zero value (`""` / `None`)
    — nothing here is inferred, guessed, or filled in from a taxonomy."""

    firm: str
    title: str
    location: str
    url: str
    source: str                      # "greenhouse" | "lever" | "workday"
    status: str = "open"             # present in a live board fetch right now;
                                      # see verify() for a real liveness check
    region: str | None = None        # never populated by this package — see
                                      # module docstring
    deadline: str | None = None      # ISO date, only when the provider's own
                                      # API exposes a real deadline field
                                      # (Greenhouse's application_deadline;
                                      # Lever and Workday's listing APIs
                                      # expose none, ever)
    posted_at: str | None = None     # ISO-ish date/text the provider reports
                                      # as "posted" / "updated" / "created" —
                                      # evidence only, never a deadline stand-in
    cohort: str | None = None        # not derivable from these three APIs;
                                      # always None from this package
    sponsorship: str | None = None   # ditto
    raw: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)


@dataclass(frozen=True, slots=True)
class FetchResult:
    """The outcome of one board fetch: either a list of opportunities, or
    an error — never both, and never a partial list silently standing in
    for a failure. Mirrors the original's per-board `{"firm": ..., "ok":
    ..., "jobs": ...}` / `{"ok": False, "error": ...}` shape from
    `sources.py`'s `ats_candidates()`, just typed."""

    board: BoardConfig
    ok: bool
    opportunities: list[Opportunity]
    raw_count: int
    error: str | None = None


@dataclass(frozen=True, slots=True)
class VerificationResult:
    """The outcome of one liveness check, ported from `verify_rows.py`'s
    per-provider verifiers. `result` is one of:

    - "verified-open"       the posting (or, for Lever, the board) is live
    - "closed"              a definitive signal it is gone (404 / empty
                            listing / job-detail endpoint returned nothing)
    - "unreachable"         a network/HTTP error prevented a determination
    - "needs-verification"  the URL doesn't carry enough to check a specific
                            item deterministically (renamed from the
                            original's "needs-llm" — this package has no LLM
                            step, so that name would be misleading here)

    `deadline_dates` holds only genuine deadline-type dates the provider's
    verify endpoint exposed (currently: none, for all three of these
    providers — see the module docstring on `Opportunity.deadline`).
    `posted_date` is evidence-only, exactly as in the original."""

    provider: str
    url: str
    result: str
    evidence: str
    deadline_dates: list[str]
    posted_date: str | None = None


class Connector(Protocol):
    """Structural protocol every provider module conforms to. There is no
    base class to subclass — a new connector (Oracle, tal.net, ...) just
    needs a module-level `name`, `fetch`, and `verify` with these shapes,
    and it plugs into `coverage_connectors.fetch_many` /
    `coverage_connectors.verify` the same way Greenhouse/Lever/Workday do."""

    name: str

    def fetch(self, board: BoardConfig) -> FetchResult: ...

    def verify(self, url: str) -> VerificationResult: ...
