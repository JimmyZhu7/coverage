"""Documented board catalog — the source of the ATS board identifiers the
`scrape` command feeds to `coverage_connectors.fetch_many`.

WHY THIS FILE EXISTS (a schema gap, reported deliberately)
----------------------------------------------------------
The shared `firms` table (directory/models.py) stores a firm's *identity*
(slug, name, domains, regions, tracks, sponsors, status) but has **no column
for its ATS board identifiers** — a Greenhouse `token`, a Lever `org`, or a
Workday `tenant_host`/`site`/`search_text`. The founder's `firms.yaml` doesn't
carry them either; in the original single-user system those tokens lived only
in `recruiting-radar/dashboard/src/dashboard/sources.py`'s `_ATS_BOARDS`
constant. Coverage's `firms` schema is owned by another workstream and must not
be altered by this one, so until a `firm_boards` table (or a JSONB `boards`
column on `firms`) is added, the board→firm mapping lives here as documented
config rather than in the database.

PROVENANCE
----------
Every entry below is transcribed from the founder's own live-verified
`_ATS_BOARDS` list (`sources.py`, read read-only 2026-07-23) or was
live-verified by direct connector fetch on the date noted beside it. All six
implemented providers are represented: greenhouse / lever / workday plus the
ported oracle (J.P. Morgan), talnet (Bank of America, Morgan Stanley), and
sitemap (HSBC) connectors. Tech firms are deliberately absent — the founder
cut tech from the app's scope on 2026-07-23.

The four boards marked `# live-verified` are the ones the connector package's
own `tests/test_live_smoke.py` exercises against the real network, so they are
the safe choice for a smoke run of the ingest path.

`firm_slug` is the `firms.yaml` id (== `Firm.slug` after seeding); `firm_name`
is the display name the connector's `Opportunity.firm` gets stamped with and is
also how `ingest` resolves the board back to its `Firm` row (case-insensitive
name match), so it must equal the seeded `Firm.name`. Where a board's firm is
not in the seed set (e.g. Palantir), `ingest` auto-creates the firm — the
"ingest broadly" posture of build-plan §4.
"""

from __future__ import annotations

from coverage_connectors import (
    AvatureBoard, BeisenBoard, BoardConfig, EightfoldBoard, GoldmanSachsBoard,
    GreenhouseBoard, IcimsBoard, LeverBoard, LumesseBoard, McKinseyBoard, OracleBoard,
    PhenomBoard, SocGenBoard,
    SuccessFactorsBoard, TalentsoftBoard,
    SitemapBoard, TalentGatewayBoard, TalnetBoard, WorkdayBoard,
)

# (firm_slug, BoardConfig). board.firm carries the display name.
BOARDS: list[tuple[str, BoardConfig]] = [
    # ---- Société Générale (Quantum search via the site's own proxy) ----
    # Deferred July as "3-step CSRF+token+proxy — complex but pure HTTP";
    # cracked 2026-08-07. ~640 EN-language postings incl. GRADUATE_JOB /
    # INTERNSHIP / VIE contract types. See the connector's docstring for the
    # three steps and why the language filter exists.
    ("socgen", SocGenBoard(firm="Société Générale")),  # live-verified 2026-08-07

    # ---- Talentsoft (server-rendered all-offers list) ----
    # The `?all=1` page skips ASP.NET ViewState paging entirely — the reason
    # this board was deferred in July. ~100 offers, one request.
    ("creditagricole", TalentsoftBoard(firm="Crédit Agricole CIB",
                              origin="https://jobs.ca-cib.com",
                              list_url="https://jobs.ca-cib.com/job/list-of-all-jobs.aspx?LCID=2057&all=1")),  # live-verified 2026-08-07

    # ---- Lumesse TalentLink FO-REST (recruitmentplatform.com widget API) ----
    # Deferred July as "needs live URL verification"; cracked 2026-08-07 —
    # guest auth is two literal headers (see the connector's docstring), and
    # every posting carries applicationUrl AND a structured DPOSTINGEND
    # deadline. tech_id read from boci.recruitmentplatform.com's own markup.
    ("boci", LumesseBoard(firm="BOCI", host="au01-foc.lumessetalentlink.com",
                          tech_id="Q7WFK026203F3VBQBLOV7F624")),  # live-verified 2026-08-07

    # ---- Greenhouse (boards-api.greenhouse.io) ----
    ("williamblair", GreenhouseBoard(firm="William Blair", token="williamblair")),   # live-verified
    ("tpg", GreenhouseBoard(firm="TPG", token="tpgcareers")),                          # live-verified
    ("solomonpartners",
     GreenhouseBoard(firm="Solomon Partners", token="solomonpartnersstudentsgraduates")),

    # ---- Lever (api.lever.co) ----
    # (No Lever boards in the catalog right now: the only live-verified Lever
    # board was Palantir, removed 2026-07-23 when the founder cut tech from
    # the app's scope. The connector stays — a finance/consulting firm on
    # Lever can be added the moment one is found.)

    # ---- Workday — campus / early-careers sites (already scoped, no search filter) ----
    ("blackstone", WorkdayBoard(firm="Blackstone", tenant_host="blackstone.wd1", site="Blackstone_Campus_Careers")),
    ("pjt", WorkdayBoard(firm="PJT Partners", tenant_host="pjtpartners.wd1", site="Students")),
    ("guggenheim", WorkdayBoard(firm="Guggenheim", tenant_host="guggenheim.wd1", site="Guggenheim_Careers_Campus")),
    ("hl", WorkdayBoard(firm="Houlihan Lokey", tenant_host="hl.wd1", site="campus")),
    ("rbc", WorkdayBoard(firm="RBC Capital Markets", tenant_host="rbc.wd3", site="RBCEARLYTALENT1")),
    ("wellington", WorkdayBoard(firm="Wellington", tenant_host="wellington.wd5", site="Campus")),
    ("citi", WorkdayBoard(firm="Citi", tenant_host="citi.wd5", site="Citi_Early_Careers_Events_Site")),  # live-verified
    ("huatai", WorkdayBoard(firm="Huatai", tenant_host="htsc.wd102", site="Huatai_Careers")),
    ("mizuho", WorkdayBoard(firm="Mizuho", tenant_host="mizuhogroup.wd102", site="External")),

    # ---- Workday — firm-wide dumps, filtered server-side via search_text ----
    # ---- Consulting (live-verified 2026-07-23 by direct connector fetch —
    # see the project rule above: nothing enters this catalog on a guess.
    # MBB run custom ATSes with no implemented connector, so the consulting
    # column starts with the firms that ARE reachable through these three
    # providers.) ----
    ("brattle", GreenhouseBoard(firm="The Brattle Group", token="thebrattlegroup")),   # live-verified
    # Oliver Wyman lives inside Marsh McLennan's group-wide Workday tenant;
    # the brand+intern search_text scopes the fetch to OW campus postings
    # (verified live: returns "Oliver Wyman - Intern - ..." rows, not Marsh/
    # Mercer noise; the unscoped board is group-wide and mislabels).
    ("oliverwyman", WorkdayBoard(firm="Oliver Wyman", tenant_host="mmc.wd1", site="MMC",
                                 search_text="oliver wyman intern")),                  # live-verified
    ("pwc", WorkdayBoard(firm="PwC", tenant_host="pwc.wd3", site="Global_Campus_Careers")),  # live-verified
    # MBB (live-verified 2026-07-23). McKinsey: the careers gateway's own
    # public JSON search, discovered from the site's network traffic and
    # re-verified from a clean client; q=intern narrows 595 postings to the
    # campus set. BCG: the Phenom People refineSearch POST its site runs on.
    # Bain: NOT coverable — bain.com sits behind an active bot-detection
    # gate; Coverage will not bypass detection, so Bain stays cycle-dates-
    # only until they expose a public feed.
    ("mckinsey", McKinseyBoard(firm="McKinsey & Company",
                               keywords=("intern", "summer", "sophomore"))),
    ("bcg", PhenomBoard(firm="BCG", host="careers.bcg.com", keywords="intern")),
    # Goldman Sachs — higher.gs.com's careers GraphQL, cracked 2026-07-23 by
    # reading the GetCampusRoles operation out of the site's own JS bundle;
    # experiences:["CAMPUS"] returns ~150 campus roles, unauthenticated.
    ("gs", GoldmanSachsBoard(firm="Goldman Sachs")),
    # UBS — IBM BrassRing TalentGateway graduate board (partner 25008 / site
    # 5131). Cracked 2026-07-24: the search API is session-gated, but the
    # board's FEATURED jobs are embedded in the page HTML (an entity-encoded
    # <input id="searchResults"> JSON blob), readable by a plain GET. Returns
    # the featured graduate subset; the full board would need a browser tier
    # (see talentgateway.py SCOPE).
    ("ubs", TalentGatewayBoard(firm="UBS", partner_id=25008, site_id=5131)),

    # ---- Big Four (probed + live-verified 2026-08-19). The consulting
    # column had MBB and PwC but none of EY/Deloitte/KPMG; three were
    # probed, two are now boards and one is documented below as not
    # buildable. ----
    #
    # Accenture — Workday, and the board is the whole firm: ~2,000+ reqs
    # dominated by non-campus delivery roles in every geography. Two things
    # forced the scoping, not just feed hygiene:
    #   * the tenant reports `total: 2000` for ANY search broad enough to
    #     reach it, the unfiltered board included — "intern", "graduate",
    #     "graduate programme" and "strategy consulting intern" all report
    #     exactly 2000. That is a ceiling, not a count.
    #   * workday.py sets `truncated` from `total > len(jobs)`, so a fetch
    #     that reads all 2000 rows the ceiling admits reports
    #     truncated=False — a truncation that looks complete, which would
    #     hand ingest's closed-detection a partial list it believes is whole.
    # So the two search_texts below are chosen to sit well under the
    # ceiling AND to avoid a hardcoded intake year: "internship" -> 205 rows
    # (2026-08-19), "early careers" -> 114. Rejected for the record:
    # "intern" (2000, and matches Internal/International), "2027 graduate
    # program" (9 rows and precise, but stale the moment the cycle turns).
    ("accenture", WorkdayBoard(firm="Accenture", tenant_host="accenture.wd103",
                               site="AccentureCareers", search_text="internship")),  # live-verified 2026-08-19
    ("accenture", WorkdayBoard(firm="Accenture", tenant_host="accenture.wd103",
                               site="AccentureCareers", search_text="early careers")),  # live-verified 2026-08-19
    #
    # Deloitte US — Avature, same connector as Bain/Macquarie. The feed path
    # matters twice over:
    #   * `/careers/SearchJobs/feed/` (the path the site links) ignores EVERY
    #     query param — search, jobRecordsPerPage, jobOffset, folderOffset —
    #     and its 20 most-recent rows were, when probed, entirely
    #     experienced-hire tax/cyber manager reqs: a board of pure noise for
    #     a student feed.
    #   * the `/en_US/careers/SearchJobs/feed/` path honours the site's own
    #     `3_5_3` job-level facet, and 477,478,480 is the trio behind its
    #     "Entry level" nav link (read from apply.deloitte.com's own markup).
    #     Same 20 rows' worth of feed, but 20 FY28 campus analyst reqs.
    # Pagination is still ignored on both paths, so this inherits the ~20
    # most-recent limit already accepted for Bain and Macquarie below — here
    # it costs less, because the facet spends those 20 slots on campus rows.
    ("deloitte", AvatureBoard(
        firm="Deloitte",
        feed_url="https://apply.deloitte.com/en_US/careers/SearchJobs/feed/?3_5_3=477%2C478%2C480")),  # live-verified 2026-08-19
    #
    # EY — SAP SuccessFactors RMK, the first board on the new
    # `successfactors` connector (see coverage_connectors/successfactors.py).
    # This platform had been written off twice in this file — Janus Henderson
    # 2026-07-24, GIC 2026-08-08, both noted as "SuccessFactors — no
    # connector" — on the assumption it was JS-gated. It is not: careers.ey.com
    # serves the full result table as plain HTML with an exact stated total
    # and honest `startrow` paging. Both of those firms were retested
    # 2026-08-19 and are now built below, alongside EY.
    # `q=` is relevance-ranked full text, not a filter, so it is used the way
    # OracleBoard's `keywords` already are: one search each, deduped by URL.
    # Measured 2026-08-19 — "internship" 88, "trainee" 32, "student" 59,
    # 150 rows after dedupe. Rejected: "intern" (3,852 — it matches Internal
    # Audit and International Tax), "graduate" (594, relevance decays into
    # senior roles by the second page), "campus" (13, all recruiter reqs).
    # The tail of even a good keyword decays into loosely-matched roles; that
    # is the same broad-fetch-plus-classifier posture the TD Securities note
    # below already documents, not a defect.
    ("ey", SuccessFactorsBoard(firm="EY", origin="https://careers.ey.com",
                               keywords=("internship", "trainee", "student"))),  # live-verified 2026-08-19
    # Janus Henderson — same platform, a much smaller tenant. Of 76 postings
    # total, exactly one is genuinely campus-facing: "Research Associate -
    # Early Careers Program 2027" (Denver). Measured 2026-08-19 — "internship"
    # 2, "graduate" 4, 6 rows after dedupe. Rejected: "intern" (48 — the same
    # Internal-Audit-style false-positive trap as EY's, here matching
    # "Internal Sales Consultant"), "student"/"trainee" (0), "early careers"
    # (76 — identical to the unfiltered board on this tenant, so not a real
    # filter at all).
    ("janushenderson", SuccessFactorsBoard(firm="Janus Henderson", origin="https://jobs.janushenderson.com",
                                           keywords=("internship", "graduate"))),  # live-verified 2026-08-19
    # GIC — same platform. The whole 172-posting tenant is titled by rank
    # (Associate/AVP/VP/SVP/MD) rather than by function, and nothing on it is
    # titled an internship right now — the board is honest and thin at the
    # internship rung, not broken. "associate" and "analyst" are GIC's own
    # junior full-time ranks, not keyword noise: measured 2026-08-19, 65 / 30
    # rows, 84 after dedupe, including the three "Summer 2027 Start" Private
    # Equity Associate cohort reqs. Rejected: "intern" (82 — the Internal
    # Audit trap again), "graduate"/"student"/"trainee"/"campus" (single
    # digits, body-text matches rather than title matches).
    ("gic", SuccessFactorsBoard(firm="GIC", origin="https://careers.gic.com.sg",
                                keywords=("associate", "analyst"))),  # live-verified 2026-08-19

    # ---- Ported providers (radar-verified 2026-07-22, re-verified on add) ----
    # Oracle Recruiting Cloud — J.P. Morgan's public REST. PostingEndDate is
    # a real deadline, so JPM rows land dated on the calendar.
    ("jpm", OracleBoard(firm="J.P. Morgan", host="jpmc.fa.oraclecloud.com",
                        site_number="CX_1001",
                        keywords=("summer analyst", "intern", "insight"))),
    # tal.net — each tenant runs SEVERAL numbered boards, and the numbering is
    # NOT stable between tenants, so read the board's own <title> rather than
    # assuming. Verified by direct fetch 2026-09-01 (the Atom feed at
    # `.../jobboard/vacancy/<N>/feed` names each board and is unauthenticated):
    #     bankcampuscareers  1 Global Programs (148)   2 Campus Events (18)
    #     jefferies          1 Events (0)              2 Campus Opportunities (51)
    #     evercore           1 Events (1)              2 Students and Graduates (9)
    #     moelis-careers     1 Events (7)              2 Student Opportunities (1)
    #     pwpcareers         1 Events (1)              2 Student Opportunities (5)
    # BofA inverts the usual order, which is exactly why the numbering cannot
    # be assumed. The Events sibling is where the insight-day layer lives -
    # Moelis's Virtual Discovery Series, Evercore's focus events, BofA's
    # insight days - and a connector that watches only the jobs board drops
    # that whole category silently.
    ("bofa", TalnetBoard(firm="Bank of America", kind="jobs",
                         board_url="https://bankcampuscareers.tal.net/vx/mobile-0/brand-4/candidate/jobboard/vacancy/1/adv/")),
    ("bofa", TalnetBoard(firm="Bank of America", kind="events",
                         board_url="https://bankcampuscareers.tal.net/vx/mobile-0/brand-4/candidate/jobboard/vacancy/2/adv/")),
    # Was live-verified 2026-07-23, then served Oleeo Protect's "Quick Check
    # Needed" interstitial from 2026-08-18. THE WALL IS DOWN AGAIN: probed
    # 2026-09-02, HTTP 200, 136,535 bytes, 50 vacancies, "Global Programs -
    # Morgan Stanley Campus", under the project's own user agent
    # (docs/talnet-2026-09.md). Oleeo Protect is a per-tenant
    # setting a tenant can toggle at will, which is why this board has been
    # both, and why the honest note is a date rather than a verdict about
    # tal.net. Nothing here needs a challenge-solver: see the Nomura entry
    # below for the one tenant still walled and why we leave it that way.
    ("ms", TalnetBoard(firm="Morgan Stanley", kind="jobs",
                       board_url="https://morganstanley.tal.net/vx/lang-en-GB/mobile-0/appcentre-1/brand-2/candidate/jobboard/vacancy/1/adv/")),
    ("ms", TalnetBoard(firm="Morgan Stanley", kind="events",
                       board_url="https://morganstanley.tal.net/vx/lang-en-GB/mobile-0/appcentre-1/brand-2/candidate/jobboard/vacancy/2/adv/")),
    # HSBC — the career site is a JS shell; its sitemap.xml lists every
    # posting, campus roles under the dedicated /emergingtalent/job/ path.
    ("hsbc", SitemapBoard(firm="HSBC", sitemap_url="https://apply.careers.hsbc.com/sitemap.xml",
                          path_filter="/emergingtalent/job/")),
    # Nomura's campus platform is also tal.net. Was live-verified
    # 2026-07-23 (jobs board returned off-cycle internships, events board
    # the "Insider Series" insight evenings). WALLED, and the only tal.net
    # tenant that still is: probed 2026-09-02, HTTP 200, 4,621 bytes, zero
    # vacancies, "Quick Check Needed" — identical under a browser user agent
    # and with a cookie jar, so there is no request we could send that opens
    # it (docs/talnet-2026-09.md). 48 open rows are frozen behind
    # it and age in place; `health.walled_boards()` says so once, loudly,
    # then quietly. Kept configured in case the tenant turns the check back
    # off. Deliberately NOT worth building a challenge-solver for, which
    # would cross from "read a public page" into defeating an employer's
    # anti-bot control (do-not-build register §5.2 item 13).
    #
    # The 56 rows this board delivered while it was open carry their
    # location under a "Location" column header, not the "City" the other
    # tenants use — see talnet._LOCATION_COL_LABELS.
    ("nomura", TalnetBoard(firm="Nomura", kind="jobs",
                           board_url="https://nomuracampus.tal.net/vx/mobile-0/candidate/jobboard/vacancy/1/adv/")),
    ("nomura", TalnetBoard(firm="Nomura", kind="events",
                           board_url="https://nomuracampus.tal.net/vx/mobile-0/candidate/jobboard/vacancy/2/adv/")),

    # Elite boutiques + KKR (agent-identified + live-verified 2026-07-24).
    # KKR's Greenhouse token really is the placeholder-looking "stage".
    ("kkr", GreenhouseBoard(firm="KKR", token="stage")),
    # Lazard runs two Oracle sites; CX_2 is the campus/internship one.
    ("lazard", OracleBoard(firm="Lazard", host="icbpjb.fa.ocs.oraclecloud.com",
                           site_number="CX_1", keywords=("intern", "graduate", "analyst"))),
    ("lazard", OracleBoard(firm="Lazard", host="icbpjb.fa.ocs.oraclecloud.com",
                           site_number="CX_2", keywords=("intern", "graduate", "analyst"))),
    # Was live and EMPTY, not broken, as of 2026-08-05 (the page served
    # ~108KB with zero vacancy links), then walled from 2026-08-18. Reachable
    # again: probed 2026-09-02, HTTP 200, 66,293 bytes, 9 vacancies,
    # "Students and Graduates - Evercore" (docs/talnet-2026-09.md).
    # Three states in one month on one tenant is the reason this catalog
    # records dates and measurements rather than a standing verdict.
    ("evercore", TalnetBoard(firm="Evercore", kind="jobs",
                             board_url="https://evercore.tal.net/vx/lang-en-GB/mobile-0/channel-1/appcentre-ext/brand-5/candidate/jobboard/vacancy/2/adv/")),
    ("evercore", TalnetBoard(firm="Evercore", kind="jobs",
                             board_url="https://evercore.tal.net/vx/lang-en-GB/mobile-0/channel-1/appcentre-ext/brand-5/candidate/jobboard/vacancy/3/adv/")),
    # Board 1 is "Events" (1 entry, verified 2026-09-01) - the campus focus
    # events. Evercore also mixes event registrations INTO its students board,
    # so dedupe by title rather than assuming one board owns one kind.
    ("evercore", TalnetBoard(firm="Evercore", kind="events",
                             board_url="https://evercore.tal.net/vx/mobile-0/candidate/jobboard/vacancy/1/adv/")),
    # Ingested ZERO rows from configuration until 2026-09-01 while serving
    # 50+ live vacancies, and reported every one of those fetches as a clean
    # success. Cause: this tenant renders the card grid, not the `<tr>`
    # table the other four tal.net tenants serve, so the row regex matched
    # nothing. talnet.py now parses both layouts and refuses to call a page
    # full of vacancy markup an empty board — see its module docstring.
    ("jefferies", TalnetBoard(firm="Jefferies", kind="jobs",
                              board_url="https://jefferies.tal.net/vx/lang-en-GB/mobile-0/appcentre-ext/brand-4/xf-016c915b0a67/candidate/jobboard/vacancy/2/adv/")),
    # Board 1 is "Events" and is EMPTY today (verified 2026-09-01, ok=True
    # n=0) - Jefferies' own site says "we currently have no active events".
    # Registered anyway because this is where its Insight Days, the Equity
    # Research Mentorship Program and the Stock Pitch Competition post when
    # the cycle opens (Nov-Jan), and an empty board that reports zero cleanly
    # costs nothing.
    ("jefferies", TalnetBoard(firm="Jefferies", kind="events",
                              board_url="https://jefferies.tal.net/vx/mobile-0/candidate/jobboard/vacancy/1/adv/")),
    # Solomon's students board is real but empty out of season; the
    # professionals board carries the current IB associate roles.
    ("solomonpartners", GreenhouseBoard(firm="Solomon Partners", token="solomonpartnersprofessionals")),
    ("pwp", WorkdayBoard(firm="Perella Weinberg", tenant_host="pwp.wd1",
                         site="PWP_Experienced_Opportunities", tenant="pwp", search_text="intern")),
    # The Workday site above is, as its name says, the EXPERIENCED board -
    # it was the only PWP board registered, and PWP showed 0 open campus rows
    # as a result. The students live on tal.net (verified 2026-09-01):
    # board 2 "Student Opportunities" (5, incl. the 2027 Private Funds
    # Advisory Analyst and the 2027 Advisory Off-Cycle Internship) and
    # board 1 "Events" (1).
    ("pwp", TalnetBoard(firm="Perella Weinberg", kind="jobs",
                        board_url="https://pwpcareers.tal.net/vx/mobile-0/candidate/jobboard/vacancy/2/adv/")),
    ("pwp", TalnetBoard(firm="Perella Weinberg", kind="events",
                        board_url="https://pwpcareers.tal.net/vx/mobile-0/candidate/jobboard/vacancy/1/adv/")),

    ("apollo", WorkdayBoard(firm="Apollo", tenant_host="athene.wd5", site="apollononpubliccareersite",
                            search_text="intern")),
    ("carlyle", WorkdayBoard(firm="Carlyle", tenant_host="carlyle.wd1", site="Carlyle", search_text="intern")),
    ("baincapital", WorkdayBoard(firm="Bain Capital", tenant_host="baincapital.wd1", site="External_Public", search_text="intern")),
    ("baincapital", WorkdayBoard(firm="Bain Capital", tenant_host="baincapital.wd1", site="External_Private", search_text="intern")),
    ("ares", WorkdayBoard(firm="Ares", tenant_host="aresmgmt.wd1", site="External", search_text="intern")),
    ("oaktree", WorkdayBoard(firm="Oaktree", tenant_host="oaktree.wd1", site="Oaktree", search_text="intern")),
    ("blueowl", WorkdayBoard(firm="Blue Owl", tenant_host="blueowl.wd1", site="Blueowl", search_text="intern")),
    ("brookfield", WorkdayBoard(firm="Brookfield", tenant_host="brookfield.wd5", site="Brookfield", search_text="intern")),
    ("moelis", WorkdayBoard(firm="Moelis", tenant_host="moelis.wd1", site="Experienced-Hires", search_text="intern")),
    # Same shape as PWP above: the Workday site is literally "Experienced-
    # Hires", and Moelis showed 0 open campus rows. Students and the whole
    # insight layer are on tal.net (verified 2026-09-01): board 2 "Student
    # Opportunities" (1, the 2027 London Summer Analyst) and board 1 "Events"
    # (7 - the Moelis Virtual Discovery Series, running 11 Sep to 13 Nov 2026).
    ("moelis", TalnetBoard(firm="Moelis", kind="jobs",
                           board_url="https://moelis-careers.tal.net/vx/mobile-0/candidate/jobboard/vacancy/2/adv/")),
    ("moelis", TalnetBoard(firm="Moelis", kind="events",
                           board_url="https://moelis-careers.tal.net/vx/mobile-0/candidate/jobboard/vacancy/1/adv/")),
    ("rothschild", WorkdayBoard(firm="Rothschild & Co", tenant_host="rothschildandco.wd3", site="RothschildAndCo_Lateral", search_text="intern")),
    ("baird", WorkdayBoard(firm="Baird", tenant_host="baird.wd1", site="Careers", search_text="intern")),
    ("raymondjames", WorkdayBoard(firm="Raymond James", tenant_host="raymondjames.wd1", site="raymondjamescareers",
                                  search_text="intern")),
    ("pipersandler", WorkdayBoard(firm="Piper Sandler", tenant_host="pipersandler.wd501", site="Piper_Sandler_Careers", search_text="intern")),
    ("stanchart", WorkdayBoard(firm="Standard Chartered", tenant_host="peopleplus.wd3", site="SCB_Careers", search_text="intern")),
    ("mizuho", WorkdayBoard(firm="Mizuho", tenant_host="mizuho.wd1", site="MizuhoAmericas", search_text="intern")),
    ("citi", WorkdayBoard(firm="Citi", tenant_host="citi.wd5", site="2", search_text="summer analyst")),
    # CITIC CLSA — Workday, HQ Hong Kong (agent-identified + live-verified 2026-07-23).
    ("clsa", WorkdayBoard(firm="CLSA", tenant_host="citicclsa.wd3", site="External",
                          search_text="intern")),
    # Haitong International — Workday, HQ Hong Kong (probed + live-verified
    # 2026-08-19: 2 postings, "Compliance Officer" (New York) and a PWM
    # Senior Officer role (Hong Kong - OIS)). Deliberately UNSCOPED, unlike
    # its CLSA neighbour above: the whole tenant is two rows, so a
    # search_text would filter a board that has nothing to filter and would
    # hide the summer-internship req the day it reopens. The 2026 Summer
    # Internship Program posting (R25000212) that made this firm worth adding
    # is already gone — its posting page's own postingAvailable flag reads
    # false, checked 2026-08-19 — which is exactly the state this entry
    # exists to notice changing. HK/China-heavy userbase, so a thin board
    # here is still worth the one nightly request.
    ("haitong", WorkdayBoard(firm="Haitong International", tenant_host="htisec.wd3",
                             site="hti_careers")),  # live-verified 2026-08-19
    ("barclays", WorkdayBoard(firm="Barclays", tenant_host="barclays.wd3", site="External_Career_Site_Barclays",
                              search_text="intern")),
    ("db", WorkdayBoard(firm="Deutsche Bank", tenant_host="db.wd3", site="DBWebsite", search_text="intern")),
    ("wf", WorkdayBoard(firm="Wells Fargo", tenant_host="wf.wd1", site="WellsFargoJobs", search_text="intern")),
    ("ms", WorkdayBoard(firm="Morgan Stanley", tenant_host="ms.wd5", site="External", search_text="intern")),
    ("blackrock", WorkdayBoard(firm="BlackRock", tenant_host="blackrock.wd1", site="BlackRock_Professional", search_text="intern")),
    ("invesco", WorkdayBoard(firm="Invesco", tenant_host="invesco.wd1", site="IVZ", search_text="intern")),
    ("fidelityintl", WorkdayBoard(firm="Fidelity International", tenant_host="fil.wd3", site="001", search_text="intern")),

    # Asset-management expansion (agent-identified + live-verified 2026-07-24).
    # All Workday except State Street (Phenom, server-rendered JSON). Amundi
    # (Avature) is JS-gated — backlog. Janus Henderson was also filed here as
    # "(SuccessFactors) ... JS-gated" — wrong, and never retested until now.
    # jobs.janushenderson.com is a plain RMK /search/ page, the same platform
    # as EY (see the successfactors connector and its entry above). Its own
    # board entry lives with the SuccessFactors boards below.
    ("vanguard", WorkdayBoard(firm="Vanguard", tenant_host="vanguard.wd5", site="vanguard_external", search_text="intern")),
    ("troweprice", WorkdayBoard(firm="T. Rowe Price", tenant_host="troweprice.wd5", site="TRowePrice", search_text="intern")),
    ("capitalgroup", WorkdayBoard(firm="Capital Group", tenant_host="capgroup.wd1", site="capitalgroupcareers", search_text="intern")),
    ("alliancebernstein", WorkdayBoard(firm="AllianceBernstein", tenant_host="abglobal.wd1", site="alliancebernsteincareers", search_text="intern")),
    ("franklintempleton", WorkdayBoard(firm="Franklin Templeton", tenant_host="franklintempleton.wd5", site="Primary-External-1", search_text="intern")),
    ("mangroup", WorkdayBoard(firm="Man Group", tenant_host="mangroupplc.wd3", site="Man_Group_Careers", search_text="intern")),
    ("neubergerberman", WorkdayBoard(firm="Neuberger Berman", tenant_host="nb.wd1", site="NBCareers", search_text="intern")),
    ("statestreet", PhenomBoard(firm="State Street", host="careers.statestreet.com", keywords="intern")),
    # PIMCO + Schroders (agent-identified + live-verified 2026-07-24).
    ("pimco", WorkdayBoard(firm="PIMCO", tenant_host="pimco.wd1", site="pimco-careers",
                           tenant="pimco", search_text="intern")),
    ("schroders", OracleBoard(firm="Schroders", host="ekbq.fa.em2.oraclecloud.com",
                              site_number="CX_2", keywords=("intern", "graduate"))),
    # Avature RSS feeds (agent-identified + live-verified 2026-07-24). The feed
    # exposes only the ~20 most-recent roles per firm (folderOffset is ignored
    # server-side) — real URLs, no location field — so coverage is recent-roles,
    # not the full board.
    ("bain", AvatureBoard(firm="Bain & Company",
                          feed_url="https://careers.bain.com/jobs/SearchJobs/feed/")),
    ("macquarie", AvatureBoard(firm="Macquarie",
                               feed_url="https://recruitment.macquarie.com/en_US/careers/SearchJobs/feed/")),

    # Prop trading / market-makers + multi-strat funds (agent-identified +
    # live-verified 2026-07-24). Greenhouse dominates — note the non-obvious
    # tokens (HRT=wehrtyou, Five Rings=fiveringsllc, DRW=drweng, Optiver=
    # optiverus). SIG cracked 2026-08-08 (icims connector, entry below);
    # Citadel/Citadel Securities (in-house + Turnstile) and Balyasny
    # (Salesforce) remain JS-gated — backlog.
    ("janestreet", GreenhouseBoard(firm="Jane Street", token="janestreet")),
    ("optiver", GreenhouseBoard(firm="Optiver", token="optiverus")),
    ("imc", GreenhouseBoard(firm="IMC Trading", token="imc")),
    ("jump", GreenhouseBoard(firm="Jump Trading", token="jumptrading")),
    ("hrt", GreenhouseBoard(firm="Hudson River Trading", token="wehrtyou")),
    ("drw", GreenhouseBoard(firm="DRW", token="drweng")),
    ("fiverings", GreenhouseBoard(firm="Five Rings", token="fiveringsllc")),
    ("akuna", GreenhouseBoard(firm="Akuna Capital", token="akunacapital")),
    ("flowtraders", GreenhouseBoard(firm="Flow Traders", token="flowtraders")),
    ("virtu", GreenhouseBoard(firm="Virtu Financial", token="virtu")),
    ("point72", GreenhouseBoard(firm="Point72", token="point72")),
    # Millennium — Eightfold talent platform (new connector, live-verified).
    ("millennium", EightfoldBoard(firm="Millennium", host="career.mlp.com", domain="mlp.com")),

    # PE / private-credit expansion (agent-identified + live-verified 2026-07-24).
    # Golub is Workday on myworkdaysite.com with a separate cxs tenant. HPS's
    # Greenhouse board is valid but currently empty (now part of BlackRock) —
    # kept so it auto-populates when reqs open. CD&R (email-only) and Hellman &
    # Friedman (no careers API) are JS-gated — backlog.
    ("eqt", GreenhouseBoard(firm="EQT", token="eqtpartners")),
    ("sixthstreet", GreenhouseBoard(firm="Sixth Street", token="sixthstreet")),
    ("hps", GreenhouseBoard(firm="HPS Investment Partners", token="hpsinvestmentpartners")),
    ("golub", WorkdayBoard(firm="Golub Capital", tenant_host="wd501", tenant="golubcapital",
                           site="Golub_Capital_Careers", domain="myworkdaysite.com")),
    ("generalatlantic", GreenhouseBoard(firm="General Atlantic", token="generalatlantic")),
    # PE backlog (no public HTTP board): Warburg Pincus, Advent, Vista Equity
    # (Getro portfolio board only), Silver Lake, Thoma Bravo — all JS/gated.

    # ---- Hedge funds / quant expansion (probed + live-verified 2026-08-08).
    # Greenhouse again dominates; note XTX's token is xtxmarketsTECHNOLOGIES
    # and Verition's is veritiongroupllc (read from its site's Greenhouse
    # embed `for=` param). Marshall Wace / ExodusPoint / Permira are thin or
    # empty today — kept, HPS-style, so reqs land the day they open. Still
    # out of reach: Citadel + Citadel Securities and Cantor (403 this
    # package's honest UA — a block, not a puzzle), D.E. Shaw (in-house, no
    # feed in 1.8MB of markup), Two Sigma (Avature, but the RSS feed is
    # disabled and SearchJobs 404s), Balyasny (no ATS host in markup),
    # Wolverine/CTC (JS shells).
    # Re-checked 2026-08-18, nothing to change: Marshall Wace's own board
    # (boards-api.greenhouse.io/v1/boards/marshallwace/jobs) still returns
    # a clean 200 with `{"jobs": [], "meta": {"total": 0}}` — genuinely
    # empty right now, not a broken token or a shape change, so the
    # connector needs no fix. Citadel/D.E. Shaw/Two Sigma/Balyasny spot-
    # checked again too; still no discoverable ATS on any of their public
    # careers pages. ----
    ("bridgewater", GreenhouseBoard(firm="Bridgewater", token="bridgewater89")),
    ("aqr", GreenhouseBoard(firm="AQR", token="aqr")),
    ("squarepoint", GreenhouseBoard(firm="Squarepoint", token="squarepointcapital")),
    ("towerresearch", GreenhouseBoard(firm="Tower Research", token="towerresearchcapital")),
    ("schonfeld", GreenhouseBoard(firm="Schonfeld", token="schonfeld")),
    ("gsacapital", GreenhouseBoard(firm="GSA Capital", token="gsacapital")),
    ("qube", GreenhouseBoard(firm="Qube Research & Technologies", token="quberesearchandtechnologies")),
    ("xtx", GreenhouseBoard(firm="XTX Markets", token="xtxmarketstechnologies")),
    ("marshallwace", GreenhouseBoard(firm="Marshall Wace", token="marshallwace")),
    ("exoduspoint", GreenhouseBoard(firm="ExodusPoint", token="exoduspoint")),
    ("verition", GreenhouseBoard(firm="Verition", token="veritiongroupllc")),
    # First finance firm on Lever since Palantir left with the tech cut.
    ("belvedere", LeverBoard(firm="Belvedere Trading", org="belvederetrading")),
    # SIG + Stifel (incl. KBW reqs) — the new icims connector's first boards.
    # SIG's tenant is careers-sig: bare sig.icims.com 302-loops plain clients.
    ("sig", IcimsBoard(firm="SIG", tenant="careers-sig")),
    ("stifel", IcimsBoard(firm="Stifel", tenant="careers-stifel")),

    # ---- Banks expansion (probed + live-verified 2026-08-08). Workday
    # tenants read from each bank's own careers page markup; totals at
    # verification: TD 1433 (searchText "intern" matches loosely — the
    # classifier buckets the noise), MUFG 502 incl. the 2027 CIBM Summer
    # Intern Program, CIBC 221, DBS 718, Santander 420. BMO's Phenom board
    # needs keywords="" — "intern" matches zero because its campus reqs say
    # Co-op/Summer; broad fetch + classifier is the established posture.
    # Misses this round: Scotiabank/SMBC/Northern Trust (Workday roots 406
    # plain clients; site names undiscoverable without a browser), BNY
    # (Oracle host not exposed in markup), Natixis/Daiwa (no ATS in markup).
    # GIC was filed here as "SuccessFactors — no connector"; that was wrong
    # and was never retested until 2026-08-19, when it turned out to be the
    # same reachable RMK platform as EY. Built as a SuccessFactorsBoard with
    # the other SuccessFactors boards above, not here.
    ("td", WorkdayBoard(firm="TD Securities", tenant_host="td.wd3", site="TD_Bank_Careers", search_text="intern")),
    ("mufg", WorkdayBoard(firm="MUFG", tenant_host="mufgub.wd3", site="MUFG-Careers", search_text="intern")),
    ("cibc", WorkdayBoard(firm="CIBC", tenant_host="cibc.wd3", site="search", search_text="intern")),
    ("dbs", WorkdayBoard(firm="DBS", tenant_host="dbs.wd3", site="DBS_Careers", search_text="intern")),
    ("santander", WorkdayBoard(firm="Santander", tenant_host="santander.wd3", site="SantanderCareers", search_text="intern")),
    ("bmo", PhenomBoard(firm="BMO", host="jobs.bmo.com", keywords="")),
    ("truist", PhenomBoard(firm="Truist", host="careers.truist.com", keywords="intern")),

    # PE retry 2026-08-08: Permira's Greenhouse resolves (0 jobs — valid,
    # HPS-style). Warburg/Silver Lake/Thoma Bravo/Advent/Vista/CVC still
    # answer 404 on every plausible token — unchanged from 2026-07-24.
    ("permira", GreenhouseBoard(firm="Permira", token="permira")),

    # ---- The SECOND Workday site per tenant, and the regional banks
    # (enumerated from each tenant's own robots.txt + live-verified
    # 2026-09-02). WS-OPS-13, unblocked by D-20. ----
    #
    # THE METHOD, AND WHY IT IS NOT A SEARCH. On Workday the connector unit is
    # `(tenant, siteId)`: a recruiter decides which requisitions reach which
    # Job Posting Site, so "the firm's board" is never a single thing. Eight
    # catalog firms had a connector pointed at the experienced-hire site and
    # had never produced one campus row between them — 689 rows scraped, 0
    # ever campus. Every site below was read off its own tenant's
    # `https://<tenant_host>.myworkdayjobs.com/robots.txt` `Allow:` list,
    # which is the tenant's own published enumeration of its public sites,
    # and then fetched once to see what it holds. Nothing here was guessed at
    # and nothing `Disallow:`-listed was fetched (D-20; see
    # UNREACHABLE_BY_POLICY below).
    #
    # AUDITED BY MEMBERSHIP, NOT BY ROW COUNT. Campus and main sites are
    # disjoint, not subsets, so a second site is only worth registering if
    # the primary does not already carry its rows. One title off each new
    # site was searched against the firm's registered primary site, plus a
    # short distinctive phrase from it so a miss could not be an artifact of
    # searching a 60-character string (all measured 2026-09-02):
    #     PJT           Studentevents "University College Dublin & Trinity
    #                   College Dublin ..." -> Students: 0. "Company
    #                   Presentation" -> Students: 3, all Camberview
    #                   full-time analyst reqs, none of them an event.
    #     Raymond James EarlyCareers "2027 Full-Time Analyst, Risk
    #                   Management" -> raymondjamescareers: 0. "2027" ->
    #                   raymondjamescareers: 0 across the whole site.
    #     Moelis        University-Hires "Join Our Campus Talent Community"
    #                   -> Experienced-Hires: 0. "Campus" -> 0.
    #     M&T           Campus "2027 Management Development Program -
    #                   Internal Audit" -> MTB: 0. "2027 Summer" -> MTB: 0,
    #                   while "Management Development Program" -> 192 rows of
    #                   mainframe and programme-manager rôles. The firm-wide
    #                   site is NOT registered for that reason: its campus
    #                   answer is zero and its noise is 192.
    #
    ("pjt", WorkdayBoard(firm="PJT Partners", tenant_host="pjtpartners.wd1",
                         site="Studentevents")),  # live-verified 2026-09-02, 4 rows
    # 7 rows and every one of them a 2027 campus req (Investment Banking
    # Summer Analyst - Private Credit / M&A, Equity Research Associate, the
    # Clark Capital Mentoring Program). The registered `raymondjamescareers`
    # board answers ZERO for "2027" — this firm's whole campus cycle was
    # invisible to Coverage and the catalog could not have known, because the
    # experienced board is large and healthy.
    ("raymondjames", WorkdayBoard(firm="Raymond James", tenant_host="raymondjames.wd1",
                                  site="RaymondJamesEarlyCareers")),  # live-verified 2026-09-02, 7 rows
    # Both live and EMPTY today (HTTP 200, total 0), the Jefferies-events
    # case: a real site with a real slug, out of season. Registered because
    # this is where the rows land when the cycle opens, and a board that
    # reports zero cleanly costs one request. `health.board_health` keys per
    # board, so an empty one is visible as "empty" rather than hidden behind
    # its firm's producing sibling.
    ("hl", WorkdayBoard(firm="Houlihan Lokey", tenant_host="hl.wd1",
                        site="Events")),  # live-verified 2026-09-02, 0 rows
    ("guggenheim", WorkdayBoard(firm="Guggenheim", tenant_host="guggenheim.wd1",
                                site="Guggenheim_Undergraduate_Programs")),  # live-verified 2026-09-02, 0 rows
    # ONE row, and the row is "Join Our Campus Talent Community" — a standing
    # interest list, not a programme. This is the site the research holds up
    # as the reason a board is audited by membership rather than by row
    # count: a count of 1 would call it healthy and Moelis's real 2027 London
    # Summer Analyst sits on tal.net (registered above), not here. Registered
    # anyway, and with this note, because it is the tenant's own campus site
    # and a real req landing on it is exactly the event nothing else would
    # see.
    ("moelis", WorkdayBoard(firm="Moelis", tenant_host="moelis.wd1",
                            site="University-Hires")),  # live-verified 2026-09-02, 1 row
    #
    # The regional banks. `SYNTHESIS-PLAN.md` Part D recommendation 3 named
    # nine; six are built below, and the three that are not are recorded
    # under "Regional banks that are NOT boards" after this list rather than
    # left looking like an oversight. These carry the late-cycle inventory a
    # student who missed the spring IB round can still apply to, which is the
    # supply D-2's gate is counted against.
    #
    # SEARCH TEXT, MEASURED NOT ASSUMED. `intern` is rejected on every one of
    # these boards for the reason the Accenture entry above already records:
    # it matches "Internal" and "International". Measured 2026-09-02 —
    # PNC "intern" 1,449 rows led by "International Trade Services Analyst";
    # KeyBank 427; U.S. Bank 1,297. `internship` is the title word the
    # campus reqs themselves use.
    ("mtb", WorkdayBoard(firm="M&T Bank", tenant_host="mtb.wd5",
                         site="Campus")),  # live-verified 2026-09-02, 11 rows
    # PNC's unfiltered board reports total=2000, which is Workday's ceiling
    # and not a count (do-not-build register §5.2 item 17). Two search texts,
    # deduped by URL the way Accenture's pair is: "internship" 24 rows
    # (Corporate & Institutional Banking Undergraduate Summer 2027), and
    # "undergraduate intern" 23 (Technology / Internal Audit / Asset
    # Management Group Undergraduate Intern) — different reqs, same board.
    ("pnc", WorkdayBoard(firm="PNC", tenant_host="pnc.wd5", site="External",
                         search_text="internship")),  # live-verified 2026-09-02, 24 rows
    ("pnc", WorkdayBoard(firm="PNC", tenant_host="pnc.wd5", site="External",
                         search_text="undergraduate intern")),  # live-verified 2026-09-02, 23 rows
    # Harris Williams is PNC's M&A house and runs its own `Allow:`-listed
    # site on the same tenant. 22 rows unfiltered, including "Investment
    # Banking 1st Year Analyst, London - Summer 2027" and "2027 M&A
    # Associate - Consumer". Deliberately unscoped, the Haitong case: the
    # whole site is 22 rows, so a search_text would filter a board with
    # nothing to filter and would hide the next campus req the day it opens.
    ("pnc", WorkdayBoard(firm="PNC", tenant_host="pnc.wd5",
                         site="HarrisWilliams")),  # live-verified 2026-09-02, 22 rows
    ("keybank", WorkdayBoard(firm="KeyBank", tenant_host="keybank.wd5",
                             site="External_Career_Site",
                             search_text="internship")),  # live-verified 2026-09-02, 38 rows
    ("fifththird", WorkdayBoard(firm="Fifth Third", tenant_host="fifththird.wd5",
                                site="53careers",
                                search_text="internship")),  # live-verified 2026-09-02, 54 rows
    ("huntington", WorkdayBoard(firm="Huntington", tenant_host="huntington.wd12",
                                site="HNBcareers",
                                search_text="internship")),  # live-verified 2026-09-02, 16 rows
    # U.S. Bank answers, and answers ZERO for campus. Measured 2026-09-02 on
    # the only two sites its robots.txt allows (`US_Bank_Careers`,
    # `Elavon_Careers`): "internship" 0, "campus" 0, "summer 2027" 0, while
    # the unfiltered site holds 1,380 experienced reqs. That is a board with
    # no campus inventory today, not a broken one, and the honest handling is
    # the HPS/Marshall Wace one — register the scoped board so the row lands
    # the day it opens, and let `health` report the zero rather than the
    # catalog inventing a reason for it.
    ("usbank", WorkdayBoard(firm="U.S. Bank", tenant_host="usbank.wd1",
                            site="US_Bank_Careers",
                            search_text="internship")),  # live-verified 2026-09-02, 0 rows

    # ---- Browser tier (headless Chromium via Playwright) ----
    # CICC runs Beisen (北森); its job list only loads after JS bootstraps a
    # session, so beisen.py drives a browser and captures the site's own
    # GetJobAdPageList API. Campus/summer boards are seasonal (0 off-cycle);
    # the project-intern board carries live roles year-round.
    ("cicc", BeisenBoard(firm="CICC", host="cicc.zhiye.com")),
]

# ---- Coverage gaps against the founder's target universe (probed 2026-07-25) ----
#
# Reconciling `recruiting-radar/targets.yaml` (69 targets) against BOARDS left
# seven finance/consulting targets with no board. Each was probed directly; the
# findings are recorded here so the next attempt starts from the result rather
# than repeating the search. None are added as live entries: this file's
# standing rule is that a board earns its place by returning real rows (see the
# Jefferies note above, probed and held back on exactly this basis).
#
# 1. BNP Paribas — SOLVED except for rows. Runs tal.net, so the existing
#    `talnet` connector covers it with no new code; the board is brand-2 and
#    self-reports its canonical paths:
#        https://bnpparibas.tal.net/vx/lang-en-GB/mobile-0/brand-2/xf-b9254cc52738/candidate/jobboard/vacancy/1/adv/   (Programmes)
#        https://bnpparibas.tal.net/vx/lang-en-GB/mobile-0/brand-2/xf-b9254cc52738/candidate/jobboard/vacancy/2/adv/   (Events)
#    Both return HTTP 200 and zero `opp_` rows on every vacancy id 1–6 as of
#    2026-07-25 — empty out of season, the same state Solomon's students board
#    shows above. Re-probe when BNP's campus cycle opens (historically Aug–Sep);
#    if rows appear, add both as plain TalnetBoard entries. Note the `xf-` path
#    segment may be session-scoped and could need rediscovering from
#    https://bnpparibas.tal.net/candidate.
#
# 2. Société Générale — needs a connector this package does not have. SG runs
#    Oracle **Taleo** (socgen.taleo.net), not Oracle Recruiting Cloud, so
#    `oracle.py` does not apply. `/careersection/sgcareers/jobsearch.ftl` 302s
#    to a session bootstrap and the `rest/jobboard/searchjobs` POST returns
#    "An Error Occurred in TEE" without a valid portal id, which is only minted
#    in a browser session. A `taleo` connector is the single highest-leverage
#    one left to write — Taleo also backs BNP Paribas's non-campus boards and a
#    long tail of banks — but it needs the browser tier (see beisen.py), not a
#    plain fetch.
#
# 3–7. No public HTTP board at all; not a connector gap, an absence of data:
#    Centerview (careers.aspx is a static page listing no roles, sitemap.xml
#    403s — Centerview recruits through campus channels, not a board),
#    Crédit Agricole CIB, CMB International, BOCI, and Guotai Junan Intl (all
#    JS shells with no ATS host in the served markup). These belong on the
#    firm-detail page as manually-maintained `firm_dates`, not in the feed.
#
# ---- Big Four + CITIC Securities, probed 2026-08-19 ----
#
# Added this round: Accenture, Deloitte and EY (see their entries above).
# Probed and NOT added, recorded so the next attempt starts from the result:
#
# A. KPMG US — no public board to read. www.kpmguscareers.com is WordPress;
#    /job-search/ ships zero postings server-side (0 occurrences of
#    "JobPosting", 0 job hrefs in 235KB of markup), its keyword/location
#    inputs sit outside any <form> and are driven by JS, its theme bundle
#    contains no API host, admin-ajax or wp-json call, and Yoast's sitemap
#    index exposes exactly one child sitemap — pages, no jobs. The campus
#    ATS does exist and IS a platform this package already supports:
#    kpmgcampus.avature.net, linked from /early-career/. But its root
#    redirects to /Login/ and both /careers/SearchJobs/ and
#    /careers/SearchJobs/feed/ 404 — it is a candidate portal, not a public
#    job board, and the only public page on it is a single scholarship
#    landing page. Nothing here is scrapeable without an account, so this
#    stays a firm-detail manual-dates situation like Centerview above.
#
# B. EY US campus (usearlycareers.ey.com) — reachable, honest, and EMPTY.
#    The site is Radancy TalentBrew and its own front-end calls a plain
#    public JSON endpoint, /search-jobs/results, which answers 200 with
#    {"hasJobs": false} and data-total-job-results="0" (2026-08-19). A
#    Radancy connector is therefore buildable whenever a Radancy board with
#    rows is worth having — but writing one now would ship an untested
#    parser against a board with nothing in it, so EY is covered through
#    careers.ey.com's SuccessFactors board above instead. Its applications
#    route on to eyglobal.yello.co, a Yello board, which is a separate
#    platform again and was not pursued for the same reason.
#
# C. CITIC Securities — JS shell behind bot protection; not attempted.
#    job.citic.com still answers HTTP 521 (origin unreachable) on both http
#    and https, unchanged from the earlier scan. cs.ecitic.com serves a
#    certificate that is not valid for that name; www.cs.ecitic.com resolves
#    and points at the real careers site, careers.citics.com, whose campus
#    and intern paths (/campus/headquarters, /headquarters/interns) all
#    return the SAME 999-byte Vue shell with an empty <div id="app">. That
#    shell loads NetEase Yidun (cstaticdun.126.net/load.min.js) at page
#    level — an anti-bot/CAPTCHA product. Per the project rule, a board
#    behind bot detection is a documented gap, not a target: the same call
#    already made for Morgan Stanley/Nomura/Evercore on Oleeo Protect above.
#    citics.zhiye.com and citic.zhiye.com were checked on the chance CITIC
#    ran Beisen like CICC does; both return Beisen's "Not Found" page.
#
# Deliberately absent, and not a gap: the 11 corp-strat / pipeline targets
# (Google, Microsoft, Amazon, Meta, Apple, Nvidia, Tencent, Alibaba, ByteDance,
# SEO Career, MLT). `scripts/import_targets.py` creates `firms` rows for them so
# the founder can tier them and hang contacts off them, but adding boards here
# would put tech back in the public feed and reverse the 2026-07-23 scope cut.
# The scope decision lives in this catalog, which is why the firms table can
# carry them harmlessly.
#
# ---- Regional banks that are NOT boards, and the tenants with no second
# site (probed 2026-09-02, WS-OPS-13) ----
#
# THREE OF THE NINE REGIONAL BANKS ARE NOT BUILT. Recorded with the probe
# result so the next attempt starts from it rather than repeating the search.
#
# 1. Regions Financial — REACHABLE AND NOT FETCHED, by policy. Its tenant
#    `regions.wd5` publishes a robots.txt whose every line is a `Disallow:`
#    and whose Allow list is empty: `Regions_Careers`, `BlackArch_Careers`
#    (its M&A arm), `Conversion_Site` and `broadbean_external` are all
#    disallowed. Coverage does not fetch a site a tenant asks it not to —
#    the same call as BlackRock's below, and D-20's whole point. Recorded in
#    UNREACHABLE_BY_POLICY so a student gets the link and goes themselves.
#
# 2. Citizens Financial Group — no Workday tenant found. Eight candidate
#    hosts probed (`citizensbank`, `citizens`, `cfg`,
#    `citizensfinancialgroup` across wd1/wd3/wd5/wd12); every one answers
#    HTTP 422 on `/robots.txt`, which is what a myworkdayjobs.com host
#    returns for a tenant that does not exist. Its ATS is somewhere else and
#    finding it needs a look at the firm's own careers markup, which is the
#    method the 2026-08-08 banks round used and is a separate piece of work.
#
# 3. Comerica — `comerica.wd1` answers **HTTP 401 on robots.txt**, i.e. the
#    host declines to tell us its rules at all. `core/robots.py` already
#    treats a 401/403 robots.txt as a refusal rather than as an absence
#    ("FAILURE MEANS ALLOW ... Only an actual Disallow match, or a robots.txt
#    served as 401/403, blocks a fetch"), so this is not a tenant to probe
#    around. Every other candidate host (wd3/wd5/wd12/wd101/wd505,
#    `comericabank.wd1`, `cma.wd1`) answers 422.
#
# THE FIVE TENANTS WITH NO SECOND SITE TO REGISTER. Each of these was on the
# audit's "connector points at the experienced-hire board" list, and the
# question WS-OPS-13 asked was whether a campus site existed alongside it.
# Their own robots.txt says no, which is a better answer than a search:
#     Ares          `aresmgmt.wd1`  -> External, External-Ada
#     Oaktree       `oaktree.wd1`   -> Oaktree
#     Blue Owl      `blueowl.wd1`   -> blueowl  (blue_owl_private disallowed)
#     Fidelity Intl `fil.wd3`       -> 001, fidelitycanada
#     Std Chartered `peopleplus.wd3`-> SCB_Careers
#     Perella W.    `pwp.wd1`       -> PWP_Experienced_Opportunities
# So their NO_CAMPUS_BOARD entries below stand, and now stand on an
# enumeration rather than on a row count. Perella Weinberg is not on that
# list and does not join it: its students are on tal.net and registered.
#
# BLACKSTONE'S ALLOW LIST IS 300+ ENTRIES and almost all of it is
# `X-GhostSite-<agency name>` — one site per external recruiter, which is
# how the tenant routes agency submissions. The campus site
# (`Blackstone_Campus_Careers`) is already registered. The handful of
# school-scoped sites on the list (`UK_SEO`, `UK_Notre_Dame`,
# `UK_London_Business_School`, `Schwarzman_Scholars`, `Boston_Career_Fairs`)
# are single-school pipelines and each needs its own membership audit before
# it earns a line here; they are named so the next pass does not have to
# re-read 42KB of robots.txt to find them.


# Vertical (Firm.tracks) for catalog firms that are NOT in the founder's
# firms.yaml seed set, keyed by catalog slug. `scrape` pre-creates these Firm
# rows (catalog slug, canonical name, these tracks) so the calendar's Track
# filter bites on them; without this, ingest's auto-create would derive a
# drifting slug ("the-brattle-group") and an empty tracks list the filter can
# never match. Seeded firms are untouched — their tracks come from firms.yaml.
# Firms that hire off a test or a competition, where a coffee chat does not
# move the process. Jane Street's public FAQ answers "Can I schedule a phone
# call or coffee?" with "unfortunately, no"; Citadel Securities' campus
# funnel is Datathons and Invitationals; the practitioner finding is "if you
# can't pass their tests, it doesn't matter who you know."
#
# THE ONE DEFINITION OF `Firm.recruiting_style = "assessment"` (D-22,
# 2026-09-02). Both writers read this set and neither restates it: the seed
# migration (directory/0017) imports it, and `scrape` reads it when it
# pre-creates or corrects a catalog firm. It used to be copied into the
# migration as a frozen tuple "by convention", which is the two definitions
# of one fact P5 forbids — and the failure mode is not abstract: a firm added
# here and not there renders a coffee-chat prompt at a firm that says in
# writing it will not take one. Adding a slug here now tags the firm on a
# fresh deploy AND on the next scrape of an existing database.
#
# Multi-strat funds that run analyst programmes with real networking
# (Millennium, Point72, AQR) are deliberately absent.
ASSESSMENT_RECRUITING: frozenset[str] = frozenset({
    "janestreet", "citadel", "citadelsecurities", "citadel-securities", "sig",
    "imc", "jump", "drw", "hrt", "optiver", "akuna", "belvedere", "fiverings",
    "flowtraders", "tower", "towerresearch", "virtu", "xtx", "squarepoint",
    "qube",
})

DEFAULT_TRACKS: dict[str, list[str]] = {
    "brattle": ["consulting"],
    "oliverwyman": ["consulting"],
    "pwc": ["consulting"],
    # Asset-management expansion.
    "vanguard": ["am"],
    "troweprice": ["am"],
    "capitalgroup": ["am"],
    "alliancebernstein": ["am"],
    "franklintempleton": ["am"],
    "mangroup": ["am"],
    "neubergerberman": ["am"],
    "statestreet": ["am"],
    # Prop trading / market-makers → sales & trading; Point72 also asset mgmt.
    "janestreet": ["st"],
    "optiver": ["st"],
    "imc": ["st"],
    "jump": ["st"],
    "hrt": ["st"],
    "drw": ["st"],
    "fiverings": ["st"],
    "akuna": ["st"],
    "flowtraders": ["st"],
    "virtu": ["st"],
    "point72": ["st", "am"],
    "millennium": ["am", "st"],
    # PE / private-credit expansion.
    "eqt": ["pe"],
    "sixthstreet": ["pe"],
    "hps": ["pe"],
    "golub": ["pe"],
    "generalatlantic": ["pe"],
    # Hedge-fund / quant expansion (2026-08-08).
    "bridgewater": ["am"],
    "aqr": ["am", "st"],
    "squarepoint": ["st", "am"],
    "towerresearch": ["st"],
    "schonfeld": ["am", "st"],
    "gsacapital": ["st"],
    "qube": ["st", "am"],
    "xtx": ["st"],
    "marshallwace": ["am"],
    "exoduspoint": ["am"],
    "verition": ["am"],
    "belvedere": ["st"],
    "sig": ["st"],
    # Banks expansion (2026-08-08).
    "stifel": ["ib"],
    "td": ["ib"],
    "mufg": ["ib"],
    "cibc": ["ib"],
    "dbs": ["ib"],
    "santander": ["ib"],
    "bmo": ["ib"],
    "truist": ["ib"],
    "permira": ["pe"],
    # Big Four expansion (2026-08-19). Haitong is an HK investment bank;
    # Accenture/Deloitte/EY are the consulting column's Big Four additions.
    "haitong": ["ib"],
    "accenture": ["consulting"],
    "deloitte": ["consulting"],
    "ey": ["consulting"],
    # SuccessFactors retest (2026-08-19). Janus Henderson is a traditional
    # asset manager; GIC is a sovereign wealth fund, tracked as asset
    # management the same as Bridgewater/AQR/Marshall Wace above.
    "janushenderson": ["am"],
    "gic": ["am"],
    # Regional banks (2026-09-02). `ib` is the convention this table already
    # applies to every bank in the 2026-08-08 round — Truist, BMO, CIBC,
    # Santander, TD — and it is a statement about the FIRM's vertical, not a
    # claim about each row: PNC runs Harris Williams, KeyBank runs KeyBanc
    # Capital Markets, Fifth Third and Huntington both run capital-markets
    # arms, M&T runs Wilmington Trust. What keeps a commercial-banking or
    # wealth req on these boards from reading "matches IB" is the title-level
    # rule in `recommend._NON_TRACK_FUNCTION`, which declines those phrases
    # outright, not this column.
    "pnc": ["ib"],
    "keybank": ["ib"],
    "fifththird": ["ib"],
    "huntington": ["ib"],
    "usbank": ["ib"],
    "mtb": ["ib"],
}


# Campus boards Coverage knows the address of and will NOT fetch, because the
# tenant's own `robots.txt` disallows it. Slug -> (what the site is, where a
# student goes instead).
#
# WHY THIS EXISTS AS DATA AND NOT AS SILENCE. D-20 settled the question: the
# `Disallow:` list is how these sites were found in the first place, no login
# or paywall is involved, and the rows behind them are exactly the campus
# inventory this product exists to surface. Coverage still does not fetch
# them. `robots.txt` is the site operator's stated wish, X7 made honouring it
# the product's own rule the same week, and a product whose pitch to
# institutions is that it is careful with data does not override its own new
# rule for a handful of rows. The `Allow:`-listed sites roughly double reach
# without the question.
#
# But "we do not fetch it" is not the same as "it does not exist", and P9 is
# the reason this is a dict rather than a comment: the board cannot see what
# it is not allowed to read, and the honest move is to say so in the UI and
# hand the student the link, not to render a firm page that looks like the
# programme is not running. Every entry carries a URL a person can open.
#
# THE BAR FOR ADDING ONE. `tenant_host` and `site` must be a pair the
# tenant's own `robots.txt` `Disallow:` list named (never a guess at a URL),
# and `url` must be a page a student can actually open. A `Firm` row is NOT
# required: Regions is not a catalog firm precisely because this rule stopped
# it becoming one, and dropping it from the record for that reason would hide
# the very decision the record exists to hold. `firm` carries the display
# name so the report reads the same either way.
UNREACHABLE_BY_POLICY: dict[str, dict[str, str]] = {
    "blackrock": {
        "firm": "BlackRock",
        "tenant_host": "blackrock.wd1",
        "site": "BlackRock_Early_Careers_Program",
        "reason": "BlackRock's Workday tenant disallows its early-careers site in robots.txt",
        "url": "https://careers.blackrock.com/early-careers",
    },
    "regions": {
        "firm": "Regions Financial",
        "tenant_host": "regions.wd5",
        "site": "Regions_Careers",
        "reason": "Regions' Workday tenant disallows every one of its career sites in robots.txt",
        "url": "https://www.regions.com/about-regions/careers",
    },
}


#: `Disallow:`-listed Workday sites, keyed by tenant host, read off the
#: tenants probed on 2026-09-02. Kept as data so the test that forbids them
#: can name them instead of a comment nothing enforces.
#:
#: KEYED BY TENANT, and it has to be: the slug alone is not the fact. Houlihan
#: Lokey disallows a site called `External` while Ares, Mizuho, CLSA and
#: Morgan Stanley each publish one under the same name, so a flat set of
#: slugs would fail four healthy boards to protect one. `refreshFacet` is
#: omitted throughout — every tenant disallows it and it is an endpoint, not
#: a career site.
#:
#: Not exhaustive and not meant to be: this is the set the catalog has
#: actually seen, and a pair joins it when a probe finds it.
DISALLOWED_WORKDAY_SITES: dict[str, frozenset[str]] = {
    "blackrock.wd1": frozenset({"BlackRock_Early_Careers_Program", "BlackRock_AIG"}),
    "regions.wd5": frozenset({"Regions_Careers", "BlackArch_Careers",
                              "Conversion_Site", "broadbean_external"}),
    "guggenheim.wd1": frozenset({"Guggenheim-Confidential"}),
    "pjtpartners.wd1": frozenset({"NonPublicJobs082891", "PrivateEvents"}),
    "keybank.wd5": frozenset({"Invite-Only_Career_Site"}),
    "blueowl.wd1": frozenset({"blue_owl_private"}),
    "oaktree.wd1": frozenset({"DirectApply"}),
    "hl.wd1": frozenset({"External"}),
}


# The field(s) that IDENTIFY one board inside its provider — every connector's
# own docstring names them, gathered here because two readers now need them:
# `ingest` stamping a per-board line into `ScrapeRun`, and `health` printing a
# per-board table.
#
# WHY A PER-BOARD IDENTITY IS NEEDED AT ALL. The catalog holds 127 boards
# under 110 slugs: 13 firms run more than one. `ingest`'s own bookkeeping is
# keyed on `(firm, provider)` — correct for closed-detection, which must union
# every board a firm has on a provider before concluding a posting is gone —
# but as the ONLY key it makes a second board invisible. Moelis and Perella
# Weinberg each gained two tal.net campus boards on 2026-09-01; both firms
# already had a producing Workday board, so both new boards could fetch zero
# every night and never appear anywhere. Marshall Wace's Greenhouse board has
# answered `{"jobs":[]}` since August with the same silence.
#
# Per provider rather than a generic sweep of field names, because "what makes
# two boards different" is not the same question per platform and a generic
# answer got it wrong: Accenture registers the SAME Workday tenant and site
# twice under different `search_text` ("internship" and "early careers"), so
# `search_text` is part of a Workday board's identity while `domain` — the
# same `myworkdayjobs.com` on all but a handful — is noise. A provider whose
# board is firm-wide with nothing to vary lists no fields and keys on its own
# name. `test_board_health.py` pins that every catalog key is unique.
_BOARD_IDENTITY_FIELDS: dict[str, tuple[str, ...]] = {
    "greenhouse": ("token",),
    "lever": ("org",),
    "workday": ("tenant_host", "site", "search_text"),
    "oracle": ("host", "site_number"),
    "talnet": ("board_url",),
    "sitemap": ("sitemap_url",),
    "mckinsey": ("keywords",),
    "phenom": ("host", "keywords"),
    "goldmansachs": (),
    "talentgateway": ("partner_id", "site_id"),
    "eightfold": ("host", "domain"),
    "beisen": ("host", "pages"),
    "avature": ("feed_url",),
    "lumesse": ("host", "tech_id"),
    "icims": ("tenant",),
    "socgen": (),
    "talentsoft": ("list_url",),
    "successfactors": ("origin", "keywords"),
}


def _shorten(value: str) -> str:
    """A URL-shaped identifier rendered as `host/…/last/three/segments`.

    tal.net board URLs run past 110 characters, most of it a fixed
    `/vx/lang-en-GB/mobile-0/appcentre-ext/brand-4/xf-…/candidate/jobboard/`
    prefix that is identical between a firm's two boards; the part that
    actually distinguishes them is the tail (`vacancy/1/adv` against
    `vacancy/2/adv`). A key nobody can read is a key nobody uses, and
    `test_board_health.py` pins that every catalog key is still unique
    after this.
    """
    if "://" not in value:
        return value
    rest = value.split("://", 1)[1].rstrip("/")
    host, _, path = rest.partition("/")
    if not path:
        return host
    segments = [s for s in path.split("/") if s]
    if len(segments) <= 3:
        return f"{host}/{'/'.join(segments)}"
    return f"{host}/…/{'/'.join(segments[-3:])}"


def board_key(board: BoardConfig) -> str:
    """A short, stable identifier for ONE board — what a human would type to
    say which of a firm's boards they mean.

    Deliberately built from the config's own identifying fields rather than
    from a URL the connector happens to construct: the fields are what the
    catalog states and what an operator would edit, and several connectors
    build more than one URL per fetch. Two boards of the same provider for
    the same firm always differ in at least one of these (they would not be
    two boards otherwise), so the key separates them; a provider with a
    single firm-wide board and nothing to vary (`socgen`, `goldmansachs`)
    honestly reports its provider name and nothing more.

    An unregistered provider falls back to a sweep of every identifying
    field any provider uses — worse to read, never wrong, and it means a new
    connector reports something sensible before anyone remembers to add it
    above.
    """
    provider = getattr(board, "provider", "")
    fields = _BOARD_IDENTITY_FIELDS.get(
        provider,
        tuple(dict.fromkeys(f for fs in _BOARD_IDENTITY_FIELDS.values() for f in fs)),
    )
    parts = []
    for field in fields:
        value = getattr(board, field, None)
        if value in (None, "", (), []):
            continue
        if isinstance(value, (tuple, list)):
            value = ",".join(str(v) for v in value)
        parts.append(_shorten(str(value)))
    return " ".join(parts) if parts else (provider or "?")


# Catalog firms whose registered board(s) are the EXPERIENCED-HIRE site, with
# the campus programme on a site Coverage does not query. Slug -> the board
# that is registered, so the health line can say what is there instead.
#
# Measured on the live board 2026-09-01: each of these firms' connectors is
# working and producing rows — 689 between them — and not one row has ever
# classified into a campus bucket. That is not a firm with no students; it is
# the wrong board, and until tonight nothing on the firm page, in the Picked
# column or in the health report said so. `research-ats-lifecycle.md` Q6 is
# the mechanism: on Workday a tenant has many Job Posting Sites and a
# recruiter chooses which requisitions go to each, so "the firm's board" is
# never a single thing.
#
# NOT A GUESS, AND NOT A PERMANENT VERDICT. Nothing is added here without a
# measured zero, and `health.firms_without_campus_board()` re-checks the DB
# every run: the moment one of these firms produces a campus row it drops off
# the report on its own, with no edit here.
#
# Moelis and Perella Weinberg belong to the same audit finding and are
# deliberately NOT on this list. Both had tal.net student boards registered
# on 2026-09-01 (PWP board 2 carries the 2027 Private Funds Advisory Analyst;
# Moelis board 2 the 2027 London Summer Analyst), so the statement this
# marker makes — "no campus board is registered for this firm" — is no longer
# true of them. Their boards returning zero today is a different fact, and
# the per-board table is where it belongs. The distinction matters: one says
# nobody has found the students' board, the other says we are watching it.
#
# The bar for adding a firm is the same one the catalog uses everywhere:
# a measured zero AND a look for the campus board that did not find one.
# "It produces no campus rows" alone is not enough — that is the symptom,
# and 100-odd catalog firms share it for ordinary seasonal reasons.
NO_CAMPUS_BOARD: dict[str, str] = {
    "ares": "Workday 'External' — an experienced-hire site (Executive Assistant, Vice President …)",
    "oaktree": "Workday experienced-hire site",
    "fidelityintl": "Workday '001' — an experienced-hire site (Senior Data Engineer …)",
    "blueowl": "Workday experienced-hire site",
    "stanchart": "Workday 'SCB_Careers' — the firm-wide experienced site",
    "bain": "Avature careers.bain.com feed — consultant hiring (Project Leader …)",
}


def select_boards(
    *, provider: str | None = None, firm_slug: str | None = None, limit: int | None = None
) -> list[tuple[str, BoardConfig]]:
    """Return `(firm_slug, board)` pairs from BOARDS, optionally filtered by
    provider and/or firm slug, capped at `limit`. Order is the stable BOARDS
    order, so `--limit 2 --provider greenhouse` deterministically picks the
    two live-verified Greenhouse boards (William Blair, TPG)."""
    rows = BOARDS
    if provider:
        rows = [r for r in rows if r[1].provider == provider]
    if firm_slug:
        rows = [r for r in rows if r[0] == firm_slug]
    if limit is not None:
        rows = rows[:limit]
    return rows
