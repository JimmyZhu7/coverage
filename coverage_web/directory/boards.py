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
    GreenhouseBoard, LumesseBoard, McKinseyBoard, OracleBoard, PhenomBoard, SocGenBoard,
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

    # ---- Ported providers (radar-verified 2026-07-22, re-verified on add) ----
    # Oracle Recruiting Cloud — J.P. Morgan's public REST. PostingEndDate is
    # a real deadline, so JPM rows land dated on the calendar.
    ("jpm", OracleBoard(firm="J.P. Morgan", host="jpmc.fa.oraclecloud.com",
                        site_number="CX_1001",
                        keywords=("summer analyst", "intern", "insight"))),
    # tal.net — each firm runs two boards: vacancy/1 (jobs, City column) and
    # vacancy/2 (events, Event Date + Registration Deadline columns).
    ("bofa", TalnetBoard(firm="Bank of America", kind="jobs",
                         board_url="https://bankcampuscareers.tal.net/vx/mobile-0/brand-4/candidate/jobboard/vacancy/1/adv/")),
    ("bofa", TalnetBoard(firm="Bank of America", kind="events",
                         board_url="https://bankcampuscareers.tal.net/vx/mobile-0/brand-4/candidate/jobboard/vacancy/2/adv/")),
    ("ms", TalnetBoard(firm="Morgan Stanley", kind="jobs",
                       board_url="https://morganstanley.tal.net/vx/lang-en-GB/mobile-0/appcentre-1/brand-2/candidate/jobboard/vacancy/1/adv/")),
    ("ms", TalnetBoard(firm="Morgan Stanley", kind="events",
                       board_url="https://morganstanley.tal.net/vx/lang-en-GB/mobile-0/appcentre-1/brand-2/candidate/jobboard/vacancy/2/adv/")),
    # HSBC — the career site is a JS shell; its sitemap.xml lists every
    # posting, campus roles under the dedicated /emergingtalent/job/ path.
    ("hsbc", SitemapBoard(firm="HSBC", sitemap_url="https://apply.careers.hsbc.com/sitemap.xml",
                          path_filter="/emergingtalent/job/")),
    # Nomura's campus platform is also tal.net (live-verified 2026-07-23:
    # jobs board returns off-cycle internships, events board the "Insider
    # Series" insight evenings).
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
    ("evercore", TalnetBoard(firm="Evercore", kind="jobs",
                             board_url="https://evercore.tal.net/vx/lang-en-GB/mobile-0/channel-1/appcentre-ext/brand-5/candidate/jobboard/vacancy/2/adv/")),
    ("evercore", TalnetBoard(firm="Evercore", kind="jobs",
                             board_url="https://evercore.tal.net/vx/lang-en-GB/mobile-0/channel-1/appcentre-ext/brand-5/candidate/jobboard/vacancy/3/adv/")),
    # Live and EMPTY, not broken (re-verified 2026-08-05: the page serves
    # ~108KB with zero vacancy links). Kept configured so the day Jefferies
    # posts campus roles they land without anyone remembering to re-add the
    # board; health reports it as "fetches cleanly, no rows" rather than as
    # a config bug.
    ("jefferies", TalnetBoard(firm="Jefferies", kind="jobs",
                              board_url="https://jefferies.tal.net/vx/lang-en-GB/mobile-0/appcentre-ext/brand-4/xf-016c915b0a67/candidate/jobboard/vacancy/2/adv/")),
    # Solomon's students board is real but empty out of season; the
    # professionals board carries the current IB associate roles.
    ("solomonpartners", GreenhouseBoard(firm="Solomon Partners", token="solomonpartnersprofessionals")),
    ("pwp", WorkdayBoard(firm="Perella Weinberg", tenant_host="pwp.wd1",
                         site="PWP_Experienced_Opportunities", tenant="pwp", search_text="intern")),

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
    ("barclays", WorkdayBoard(firm="Barclays", tenant_host="barclays.wd3", site="External_Career_Site_Barclays",
                              search_text="intern")),
    ("db", WorkdayBoard(firm="Deutsche Bank", tenant_host="db.wd3", site="DBWebsite", search_text="intern")),
    ("wf", WorkdayBoard(firm="Wells Fargo", tenant_host="wf.wd1", site="WellsFargoJobs", search_text="intern")),
    ("ms", WorkdayBoard(firm="Morgan Stanley", tenant_host="ms.wd5", site="External", search_text="intern")),
    ("blackrock", WorkdayBoard(firm="BlackRock", tenant_host="blackrock.wd1", site="BlackRock_Professional", search_text="intern")),
    ("invesco", WorkdayBoard(firm="Invesco", tenant_host="invesco.wd1", site="IVZ", search_text="intern")),
    ("fidelityintl", WorkdayBoard(firm="Fidelity International", tenant_host="fil.wd3", site="001", search_text="intern")),

    # Asset-management expansion (agent-identified + live-verified 2026-07-24).
    # All Workday except State Street (Phenom, server-rendered JSON). Janus
    # Henderson (SuccessFactors) and Amundi (Avature) are JS-gated — backlog.
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
    # optiverus). Citadel/Citadel Securities (in-house + Turnstile), SIG
    # (iCIMS), and Balyasny (Salesforce) are JS-gated — backlog.
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
# Deliberately absent, and not a gap: the 11 corp-strat / pipeline targets
# (Google, Microsoft, Amazon, Meta, Apple, Nvidia, Tencent, Alibaba, ByteDance,
# SEO Career, MLT). `scripts/import_targets.py` creates `firms` rows for them so
# the founder can tier them and hang contacts off them, but adding boards here
# would put tech back in the public feed and reverse the 2026-07-23 scope cut.
# The scope decision lives in this catalog, which is why the firms table can
# carry them harmlessly.


# Vertical (Firm.tracks) for catalog firms that are NOT in the founder's
# firms.yaml seed set, keyed by catalog slug. `scrape` pre-creates these Firm
# rows (catalog slug, canonical name, these tracks) so the calendar's Track
# filter bites on them; without this, ingest's auto-create would derive a
# drifting slug ("the-brattle-group") and an empty tracks list the filter can
# never match. Seeded firms are untouched — their tracks come from firms.yaml.
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
