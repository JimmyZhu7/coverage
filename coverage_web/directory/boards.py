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
    BeisenBoard, BoardConfig, EightfoldBoard, GoldmanSachsBoard, GreenhouseBoard,
    LeverBoard, McKinseyBoard, OracleBoard, PhenomBoard, SitemapBoard,
    TalentGatewayBoard, TalnetBoard, WorkdayBoard,
)

# (firm_slug, BoardConfig). board.firm carries the display name.
BOARDS: list[tuple[str, BoardConfig]] = [
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
    # Series" insight evenings). Probed and rejected the same day: Jefferies'
    # jefferies.tal.net resolves but lists nothing on either board path —
    # not added until it can be verified against real rows.
    ("nomura", TalnetBoard(firm="Nomura", kind="jobs",
                           board_url="https://nomuracampus.tal.net/vx/mobile-0/candidate/jobboard/vacancy/1/adv/")),
    ("nomura", TalnetBoard(firm="Nomura", kind="events",
                           board_url="https://nomuracampus.tal.net/vx/mobile-0/candidate/jobboard/vacancy/2/adv/")),

    ("apollo", WorkdayBoard(firm="Apollo", tenant_host="athene.wd5", site="apollononpubliccareersite",
                            search_text="summer analyst intern")),
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
                                  search_text="summer analyst intern")),
    ("pipersandler", WorkdayBoard(firm="Piper Sandler", tenant_host="pipersandler.wd501", site="Piper_Sandler_Careers", search_text="intern")),
    ("stanchart", WorkdayBoard(firm="Standard Chartered", tenant_host="peopleplus.wd3", site="SCB_Careers", search_text="intern")),
    ("mizuho", WorkdayBoard(firm="Mizuho", tenant_host="mizuho.wd1", site="MizuhoAmericas", search_text="intern")),
    ("citi", WorkdayBoard(firm="Citi", tenant_host="citi.wd5", site="2", search_text="summer analyst")),
    # CITIC CLSA — Workday, HQ Hong Kong (agent-identified + live-verified 2026-07-23).
    ("clsa", WorkdayBoard(firm="CLSA", tenant_host="citicclsa.wd3", site="External",
                          search_text="summer analyst intern")),
    ("barclays", WorkdayBoard(firm="Barclays", tenant_host="barclays.wd3", site="External_Career_Site_Barclays",
                              search_text="summer analyst intern")),
    ("db", WorkdayBoard(firm="Deutsche Bank", tenant_host="db.wd3", site="DBWebsite", search_text="summer analyst intern")),
    ("wf", WorkdayBoard(firm="Wells Fargo", tenant_host="wf.wd1", site="WellsFargoJobs", search_text="summer analyst intern")),
    ("ms", WorkdayBoard(firm="Morgan Stanley", tenant_host="ms.wd5", site="External", search_text="summer analyst intern")),
    ("blackrock", WorkdayBoard(firm="BlackRock", tenant_host="blackrock.wd1", site="BlackRock_Professional", search_text="summer analyst intern")),
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
