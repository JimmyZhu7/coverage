"""Deterministic role-type classification for scraped postings.

Why this exists: the connectors report exactly what the ATS APIs return and
never invent taxonomy (see coverage_connectors/models.py), so every posting
used to land with `bucket=""` — and a "Vice President, Fund Finance" row sat
on the public calendar next to a spring week. The calendar's promise is the
three campus categories (insight programmes, internships, entry-level), so
the bucket has to be derived somewhere deliberate, testable, and cheap. That
somewhere is here: pure functions over the posting title (plus one board-level
campus hint), no LLM call per row, same posture as the staleness layer.

Buckets (the `Opportunity.bucket` vocabulary):

- ``insight``      pre-internship events: insight days/weeks, spring weeks,
                   discovery programmes, women-in-X / sophomore / diversity
                   events with no internship attached.
- ``internship``   summer/winter/off-cycle analyst & associate internships,
                   co-ops, industrial placements.
- ``entry_level``  full-time campus hires: graduate programmes, new-grad
                   roles, analyst development programmes, trainees.
- ``other``        everything else — experienced hires, HR/recruiting roles,
                   anything without an explicit campus signal.

Precision beats recall, deliberately. A missed classification degrades to
"other" (hidden behind an honest "N other roles" toggle in the UI); a false
positive puts a Senior DevOps Engineer on a student's deadline list and costs
trust — the one thing the brand can't spend. So every rule needs an explicit
signal, word-boundary matched ("intern" must never fire on "Internal Audit"),
and neutral titles like plain "Analyst" only get promoted when the *board*
itself is campus-scoped (Blackstone_Campus_Careers, ...students sites).

Ordering is load-bearing and documented at `classify_role`.
"""

from __future__ import annotations

import re
from datetime import date

INSIGHT = "insight"
INTERNSHIP = "internship"
ENTRY_LEVEL = "entry_level"
OTHER = "other"

#: The buckets the product is about, in display order. `other` is not a
#: target — it exists so nothing is silently dropped.
TARGET_BUCKETS = (INSIGHT, INTERNSHIP, ENTRY_LEVEL)

BUCKET_LABELS = {
    INSIGHT: "Insight Programme",
    INTERNSHIP: "Internship",
    ENTRY_LEVEL: "Entry-Level",
    OTHER: "Other",
}


def _rx(*patterns: str) -> re.Pattern[str]:
    return re.compile("|".join(patterns), re.IGNORECASE)


# ---------------------------------------------------------------------------
# Region normalization — collapse a free-text location into one of the four
# markets the product targets (Hong Kong / US / Singapore / Europe), or "" for
# anything else. Boards report location wildly inconsistently ("London, UK";
# "New York, New York, United States"; "北京市 / 上海市"), so this maps the raw
# string to a canonical code the Region filter and the network scopes share.
# ---------------------------------------------------------------------------
REGION_LABELS = {
    "hk": "Hong Kong", "us": "United States", "sg": "Singapore", "eu": "Europe",
    # Secondary markets that real boards carry (CICC's mainland internships,
    # Point72 Tokyo). Mapped so the roles are filterable instead of sitting
    # region-less and invisible to the Region filter.
    "cn": "Mainland China", "jp": "Japan",
    # A real market Coverage does not track (Sydney, Mumbai, Seoul, São
    # Paulo...). Distinct from "" on purpose: blank means the posting never
    # said where it is, "other" means it said and the answer is outside the
    # six tracked markets. The two used to share one "Other / Unstated"
    # bucket, which made 230 stated-but-untracked locations indistinguishable
    # from 103 genuinely silent ones.
    "other": "Other Markets",
    # Placeless BY DESIGN, and that is itself a stated fact. KKR's "Early
    # Careers Talent Community" gives its location as "Global"; Bank of
    # America's "APAC Virtual Recruitment Event" rows are virtual with no
    # venue, one covering four countries at once. These are not postings
    # that forgot to say where they are — they are postings whose honest
    # answer is "nowhere in particular", which deserves a name rather than
    # the same blank as a page we simply failed to read.
    "global": "Global / Virtual",
}
# The markets Coverage actually tracks, in display order. This is the set a
# STUDENT can express a preference for (accounts.forms.REGION_CHOICES) and
# the set the firm-fit score reasons over.
TRACKED_REGIONS = ("hk", "us", "sg", "eu", "cn", "jp")
# The facet's order adds "other" — a place a role can BE, never a place a
# student chooses to target. Keeping the two vocabularies separate is what
# stops "Other Markets" appearing in Settings as somewhere to want to work.
REGION_ORDER = TRACKED_REGIONS + ("other", "global")

# Checked in order; the first matching market wins. Keys are lowercase
# substrings (city / country / region tokens).
_REGION_KEYS: tuple[tuple[str, tuple[str, ...]], ...] = (
    # ", hkg" / ", jpn" / ", rou": ISO-3166 alpha-3 codes, which SocGen's
    # board emits as the ENTIRE location field (", HKG"). Comma-prefixed so a
    # word containing the trigram can't fire; ", rou" also sits inside a
    # hypothetical ", Rouen" — France, so the same eu answer either way.
    ("hk", ("hong kong", "hongkong", "香港", "hksar", ", hkg")),
    ("sg", ("singapore", "新加坡")),
    ("eu", (
        "london", "united kingdom", "u.k.", " uk", "england", "scotland",
        "frankfurt", "munich", "münchen", "berlin", "germany", "deutschland",
        "paris", "france", "amsterdam", "netherlands", "rotterdam",
        "zurich", "geneva", "switzerland", "milan", "italy", "madrid", "spain",
        "dublin", "ireland", "luxembourg", "brussels", "belgium",
        "stockholm", "sweden", "copenhagen", "denmark", "oslo", "norway",
        "warsaw", "wroclaw", "krakow", "poland", "sofia", "bulgaria",
        "lisbon", "portugal", "vienna", "austria", "budapest", "hungary",
        "prague", "czech", "bucharest", "romania", "athens", "greece",
        "helsinki", "finland", "europe", "emea",
        # Added 2026-08-03, same census: Barclays' HQ address renders as
        # "Canary Wharf, 1 Churchill Place" with no city; Glasgow and
        # Henley-on-Thames (Invesco) are UK sites the country list missed.
        "canary wharf", "glasgow", "henley-on-thames",
        # 2026-08-08 census tail: Intesa's Turin, ING's Hague, SoftServe's
        # Lviv. Ukraine files under Europe the geography, not any union.
        "torino", "turin", "the hague", "lviv", "kyiv", "kiev", "ukraine",
        # 2026-08-08 unregioned census, second pass — every key below had a
        # live row stuck at region="": HSBC's Sheffield insight programmes,
        # PwC's Global_Campus board (Vilnius, Warszawa — the Polish spelling
        # Warsaw's key can't see — and Skopje: Europe the geography again),
        # Santander's "Grande Lisboa" StartX rows, SocGen's ", ROU".
        "sheffield", "vilnius", "lithuania", "warszawa", "skopje",
        "lisboa", ", rou",
    )),
    ("us", (
        "united states", "u.s.", "usa", "new york", ", ny", "jersey city",
        "chicago", ", il", "boston", ", ma", "san francisco", "menlo park",
        "los angeles", ", ca", "seattle", ", wa", "houston", "dallas", ", tx",
        "atlanta", ", ga", "charlotte", ", nc", "washington, d", "miami", ", fl",
        "nashville", ", tn", "malvern", "philadelphia", ", pa", "denver", ", co",
        # Added 2026-08-03 from the live unmatched-location census (each key
        # below had real rows stuck at region=""): T. Rowe's Maryland offices,
        # Stamford/Short Hills/Wilmington suburbs, "NYC (1285)"-style
        # shorthands, Baird's Milwaukee. ", de" is deliberately ABSENT even
        # though Wilmington is in Delaware: as a substring it is inside
        # ", denmark", and a Copenhagen row must not become American.
        ", md", ", ct", ", nj", "baltimore", "milwaukee", "nyc",
        "wilmington", "stamford",
        # Guards for the "other markets" tier below, present here so the
        # TRACKED pass wins them first: "india" is a substring of Indiana and
        # Indianapolis, and plain "mexico" of New Mexico. Without these, a
        # Midwestern posting would emigrate.
        "indianapolis", "indiana", "new mexico", "albuquerque",
        # 2026-08-08 second-pass census: Morgan Stanley's tal.net rows state
        # their city bare ("South Jordan" Utah, "Alpharetta" Georgia). Both
        # are unambiguous as substrings; "south jordan" cannot reach the
        # country Jordan, which has no key.
        "south jordan", "alpharetta",
        # 2026-08-09, from the placeless-tier audit: KKR's regional VP rows
        # give their location as "Florida Remote" / "Minnesota Remote" and
        # Neuberger's as "Remote / Home Office - Texas". The state is spelled
        # out, which the ", TX" suffix pass cannot see. "washington" covers
        # both the state and the District; the Tyne and Wear town of the same
        # name has no finance board behind it.
        "texas", "florida", "minnesota", "washington", "- us",
    )),
    # After hk so "香港" never falls through to the mainland bucket.
    ("cn", (
        "beijing", "shanghai", "shenzhen", "guangzhou", "hangzhou", "chengdu",
        "北京", "上海", "深圳", "广东", "广州", "杭州", "成都", "中国",
        "mainland china", ", china",
        # Bare "China" as the whole location string (two live rows). Safe at
        # this position: any US-city-plus-"China Basin"-style address hits
        # the US tier first, which runs before this one.
        "china",
    )),
    ("jp", ("tokyo", "japan", "日本", "東京", ", jpn")),
)

# Markets Coverage does not track, recognised so they can be FILED rather than
# left blank. Checked only after every tracked market has declined (order in
# normalize_region), so "Melbourne, FL" is American before Melbourne is
# Australian and "Perth, Scotland" is European before Perth is Australian.
# Country names lead; cities are the ones the live census actually produced
# (Sydney 12, Bangalore 11, Mumbai 10, Seoul 9, São Paulo 14, KL 6, Dubai 7,
# Casablanca 4...). Ambiguous city names are deliberately absent: bare
# "San Jose" is California or Costa Rica, bare "Santiago" is Chile or Spain,
# and a wrong market is worse than "other" never matching.
_OTHER_MARKET_KEYS: tuple[str, ...] = (
    "australia", "sydney", "melbourne", "brisbane", "perth",
    "india", "gift city", "bangalore", "bengaluru", "mumbai", "pune", "chennai",
    "hyderabad", "gurgaon", "gurugram", "noida", "new delhi", "kolkata",
    "korea", "seoul",
    "brazil", "brasil", "sao paulo", "são paulo", "rio de janeiro",
    "canada", "toronto", "montreal", "montréal", "vancouver", "calgary",
    "ottawa", "mississauga",
    "united arab emirates", "dubai", "abu dhabi",
    "saudi", "riyadh", "qatar", "doha", "bahrain", "kuwait",
    "israel", "tel aviv",
    "malaysia", "kuala lumpur", "putrajaya",
    "indonesia", "jakarta",
    "thailand", "bangkok",
    "vietnam", "ho chi minh", "hanoi",
    "philippines", "manila", "taguig",
    "taiwan", "taipei",
    "morocco", "casablanca",
    "mexico", "argentina", "buenos aires", "colombia", "bogota", "bogotá",
    "chile", "peru",
    "south africa", "johannesburg", "cape town",
    "nigeria", "lagos", "kenya", "nairobi", "egypt", "cairo",
    "turkey", "türkiye", "istanbul",
    "new zealand", "auckland",
    # 2026-08-08 second-pass census — each with live rows at region="":
    # McKinsey's Costa Rica hub (its payload states countries=["Costa Rica"]
    # while the city is the deliberately-excluded bare "San Jose"), the
    # Almaty/Astana fellowships, PwC's Quito office, and PwC's New Zealand
    # rows, which render as "Wellington - NZL"/"Christchurch - NZL". The
    # alpha-3 " nzl" key (space-prefixed) reads that suffix without adding
    # "wellington" — Wellington, FL is a real place — or "christchurch",
    # which is also a town in Dorset.
    "costa rica", "kazakhstan", "ecuador", "quito", " nzl",
    # DRW's crypto operations rows: "Remote - Cayman Islands". Remote from
    # somewhere named is still somewhere named.
    "cayman",
)

# Placeless by design. Checked LAST, after every real market: "Virtual —
# Sydney" is an Australian role delivered remotely and "Remote, New York" is
# American, so both must resolve to their market before this tier is
# consulted. Only a posting that names no place at all, and says so, lands
# here.
#
# Regional acronyms are deliberately NOT keys. "EMEA" already resolves to
# Europe, and a bare coverage area is not a statement of placelessness — the
# two BofA rows reach this tier on the word "virtual" in their own titles,
# which is the thing that is actually true about them.
_GLOBAL_KEYS: tuple[str, ...] = (
    "remote", "worldwide", "anywhere",
    "multiple locations", "all locations", "location agnostic",
)

# "virtual", with the one collision this vocabulary has: a "virtual asset" is
# a cryptocurrency, not a way of attending. BOCI's "AVP, Virtual Asset Trade
# Settlement" is a desk job in Hong Kong and was filed as placeless on the
# strength of the word. Word-bounded as well, so "virtually" cannot fire it.
_VIRTUAL_RE = re.compile(r"\bvirtual\b(?!\s+assets?\b)")

# "global" is NOT in the list above, and the first draft of this tier proved
# why. In this industry the word names a DIVISION far more often than a
# place: Global Markets, Global Technology, Global Operations, Global
# Commodities, Global Coverage Centre, Global Investment Research, SIG's
# Global Routing. Scanning for it as a substring filed sixteen rows as
# placeless, and the URLs of three of them said EMEA, Singapore and
# Singapore. A division name is not a location.
#
# It counts only when it is the WHOLE of the location field and therefore
# cannot be modifying anything — KKR's talent community, whose location
# really does read "Global". Compared against the stripped field, never
# against a title.
_PLACELESS_EXACT: frozenset[str] = frozenset({
    "global", "worldwide", "various", "various locations",
})


# A two-letter US state suffix (", NY", ", CO") cannot be tested as a bare
# substring, and testing it that way was a live bug: ", ca" sits inside
# "Toronto, Canada", ", co" inside "Bogota, Colombia" and inside the ordinary
# English phrase "timely, complete and accurate", ", ma" inside ", Macau".
# Five Canadian roles were filed under the United States on live data, and any
# prose fed to this function resolved to "us" on the strength of a comma.
#
# The fix is a boundary, not a shorter list: a state code is followed by the
# end of the string or by a non-letter. The ", de"/", denmark" note in the key
# table above is the same hazard, caught once by hand and left to recur.
_STATE_SUFFIX = re.compile(
    # "va": SIG's iCIMS filings write "Richmond, VA, US". "usa?" is the
    # country tail of the same shape (", US" / ", USA") — boundary-checked
    # like the states, so ", Ust-Kamenogorsk" can never become American.
    r",\s*(?:ny|il|ma|ca|wa|tx|ga|nc|fl|tn|pa|co|md|ct|nj|va|usa?)(?![a-z])",
    re.IGNORECASE)
_US_STATE_KEYS = frozenset({", ny", ", il", ", ma", ", ca", ", wa", ", tx",
                            ", ga", ", nc", ", fl", ", tn", ", pa", ", co",
                            ", md", ", ct", ", nj"})


def normalize_region(location: str | None, *, fallback: str = "") -> str:
    """Map a free-text location to a tracked market code, to "other" for a
    stated location outside the tracked markets, or to `fallback` (default
    "") when the text names no recognisable place at all."""
    text = (location or "").lower()
    if not text:
        return fallback
    for code, keys in _REGION_KEYS:
        for k in keys:
            # State codes are skipped here and checked below as one
            # boundary-aware pass; a plain `in` test on them is the bug
            # described above.
            if k not in _US_STATE_KEYS and k in text:
                return code
        if code == "us" and _STATE_SUFFIX.search(text):
            return code
    # Only after every tracked market has declined: a stated location in a
    # market Coverage does not track files under "other" instead of blank.
    for k in _OTHER_MARKET_KEYS:
        if k in text:
            return "other"
    # Last: the posting states that it has no single place. The exact test
    # first, because its vocabulary ("global") is only safe when it stands
    # alone as the entire field.
    if text.strip() in _PLACELESS_EXACT:
        return "global"
    if _VIRTUAL_RE.search(text):
        return "global"
    for k in _GLOBAL_KEYS:
        if k in text:
            return "global"
    return fallback


# ---------------------------------------------------------------------------
# Region from prose — the fill-only rescue for postings whose LIST payload
# carried no location at all. 240 open campus roles were invisible to every
# concrete region filter while their own cached descriptions said "Program
# Locations: New York, NY" in as many words.
#
# Two gates, both load-bearing:
#
# 1. LOCATION-ANCHORED WINDOWS, never the whole text. A bank's boilerplate
#    name-drops half its offices ("our London, Hong Kong and New York teams"),
#    and running normalize_region over full prose would hand first-match-wins
#    to whichever market sorts first in _REGION_KEYS. Only the ~90 characters
#    after an anchor phrase ("location:", "based in", "office in"...) are
#    read, because that is where postings state where THIS role sits.
# 2. AGREEMENT, or silence. Windows that resolve to different markets mean
#    the posting is multi-market or the anchors caught noise; either way the
#    honest region is "unstated", not a coin flip. Same contract as every
#    extractor in facts.py: silence is an answer.
# ---------------------------------------------------------------------------
# The window stops at a sentence boundary: with a bare `.{0,90}` the first
# anchor's window ran into the NEXT sentence, so "Location: New York. This
# role is based in Hong Kong." read both cities through one anchor and the
# agreement gate never saw a conflict.
_REGION_ANCHOR = re.compile(
    # `\*?\s*` before the colon: Wells Fargo writes "Program Locations * :
    # Charlotte, NC" — the asterisk is a footnote marker, not a boundary.
    r"(?:locations?\s*\*?\s*:|based in|located in|office in|position is in|"
    r"role is (?:based |located )?in|work location|this internship is in|"
    # tal.net detail pages render a label table with no colons: "Region
    # Europe, Middle East, Africa Event - Location United Kingdom" (Morgan
    # Stanley), "Region U.S. and Canada Location United States of America"
    # (Bank of America). The label SEQUENCE is the anchor — a bare "location"
    # is everyday prose ("based on your location, experience, and skills")
    # and must never anchor on its own. The gap between the labels refuses a
    # sentence end, but a dot closing a single-letter token is an
    # abbreviation ("U.S. and Canada"), not a boundary — without that
    # allowance the BofA shape above never matches its own Region value.
    r"\bregion\b(?:[^.\n|]|(?<=(?<![a-z])[a-z])\.){0,50}?\blocation\b|"
    # tal.net's venue field, same boards: "Event address / venue details
    # London City Centre - …". A "To be confirmed" value names no place and
    # resolves to nothing, which is the right answer.
    r"event address\s*/\s*venue details)"
    r"\s*([^.\n]{0,90})", re.IGNORECASE)


def region_from_prose(text: str | None) -> str:
    if not text:
        return ""
    found = set()
    for m in _REGION_ANCHOR.finditer(text):
        code = normalize_region(m.group(1))
        if code:
            found.add(code)
    return found.pop() if len(found) == 1 else ""


# ---------------------------------------------------------------------------
# Region from the provider's own location structures — fields that were
# ALREADY IN the stored payload and never read. Goldman's list API files every
# posting with city/state/country ({"city": "Birmingham", "state": "West
# Midlands, England", "country": "United Kingdom"} — which is also the only
# honest way to place a bare "Birmingham"); McKinsey's carries parallel
# `cities`/`countries` arrays ("San Jose" + "Costa Rica"); Greenhouse's a
# `location.name`; and `enrich_postings` stores what a posting page's own
# structured data states under `detail_location`. Same agreement contract as
# region_from_prose: all stated locations must resolve to ONE market, or the
# answer is silence — a two-market posting has no single region.
# ---------------------------------------------------------------------------
def _stated_locations(raw: dict | None) -> list[str]:
    """Every location string the provider's payload states for THIS posting."""
    raw = raw or {}
    out: list[str] = []
    for loc in raw.get("locations") or ():          # Goldman list API shape
        if isinstance(loc, dict):
            parts = (loc.get("city"), loc.get("state"), loc.get("country"))
            joined = ", ".join(str(p) for p in parts if p)
            if joined:
                out.append(joined)
    cities = raw.get("cities") or ()                # McKinsey parallel arrays
    countries = raw.get("countries") or ()
    if isinstance(cities, (list, tuple)) and isinstance(countries, (list, tuple)):
        for i in range(max(len(cities), len(countries))):
            parts = (cities[i] if i < len(cities) else "",
                     countries[i] if i < len(countries) else "")
            joined = ", ".join(str(p) for p in parts if p)
            if joined:
                out.append(joined)
    loc = raw.get("location")                       # Greenhouse object shape
    if isinstance(loc, dict) and loc.get("name"):
        out.append(str(loc["name"]))
    detail = raw.get("detail_location") or ""       # enrich_postings' read of
    if isinstance(detail, str) and detail:          # the page's structured data
        out.extend(p.strip() for p in detail.split(";") if p.strip())
    return out


def region_from_fields(raw: dict | None) -> str:
    """The one market the provider's own location fields agree on, else ""."""
    found = {code for code in
             (normalize_region(s) for s in _stated_locations(raw)) if code}
    return found.pop() if len(found) == 1 else ""


# ---------------------------------------------------------------------------
# Title cleanup — boards prepend location/region routing to titles ("EMEA |
# Frankfurt | Women in Banking Dinner") and append requisition codes
# ("Project Intern (J19302)"). Strip both so the displayed title is the role.
# ---------------------------------------------------------------------------
# Region-ish words that show up as a leading pipe-segment (not real title text).
_TITLE_REGION_WORDS = {
    "emea", "apac", "amer", "amrs", "americas", "global", "asia",
    "asia pacific", "eu", "us", "usa", "uk", "na", "latam", "mena", "anz",
}
# A trailing requisition code in brackets: has a digit, no spaces inside —
# "(J19302)", "(R-788678)", "[REQ-30087]". A worded parenthetical with spaces
# ("(Summer 2027)") has a space and is left alone.
_REQ_CODE = re.compile(r"\s*[\(\[](?=[^)\]\s]*\d)[A-Za-z0-9][\w.\-/]*[\)\]]\s*$")
_WS = re.compile(r"\s{2,}")


def _is_location_segment(seg: str) -> bool:
    s = seg.strip().lower()
    if not s or len(s) > 24:
        return False
    return s in _TITLE_REGION_WORDS or bool(normalize_region(s))


def clean_title(title: str | None) -> str:
    """Strip leading location/region pipe-segments and a trailing requisition
    code, then collapse whitespace. Idempotent: cleaning a clean title is a
    no-op, so it's safe to re-run over already-stored rows."""
    t = (title or "").strip()
    if not t:
        return t
    if "|" in t:
        parts = [p.strip() for p in t.split("|")]
        while len(parts) > 1 and _is_location_segment(parts[0]):
            parts.pop(0)
        t = " | ".join(parts)
    t = _REQ_CODE.sub("", t)
    t = _WS.sub(" ", t)
    return t.strip(" |·-–—")


def region_from_title_segments(title: str | None) -> str:
    """The one market a title's leading routing segments state, else "".

    The segments clean_title STRIPS are location data the board itself put
    there ("APAC | Singapore | Global Markets Recruitment Event" — Bank of
    America's tal.net routing), and stripping them was throwing away the only
    place those rows ever stated. This reads the same segments, through the
    same `_is_location_segment` gate, before they are lost. Only the leading
    routing segments are read — never the title body, whose market words are
    desks, not offices ("Equity Research, China Industrials" sits in Hong
    Kong). Region-tag segments that name no single market ("APAC", "Global")
    resolve to nothing and drop out; conflicting segments mean no answer,
    same agreement contract as region_from_prose. Callers should pass the
    provider's ORIGINAL title (raw payload), since a stored title has already
    been cleaned.
    """
    parts = [p.strip() for p in (title or "").split("|")]
    found = set()
    while len(parts) > 1 and _is_location_segment(parts[0]):
        code = normalize_region(parts.pop(0))
        if code:
            found.add(code)
    return found.pop() if len(found) == 1 else ""


# Insight events. "insight" alone is NOT enough — "Market Insights Analyst"
# is a real data job — so it must be qualified by an event/programme word or
# an early/spring prefix.
_INSIGHT = _rx(
    r"insights?\s+(?:day|week|programme|program|event|evening|series|session)",
    r"\binsight\s+into\b",                      # "Insight into Operations Opportunities"
    r"(?:early|spring|first[\s-]?year|1st[\s-]?year)\s+insights?\b",
    r"\bspring\s*week\b",
    r"\bpre[\s-]?internship\b",
    r"\bdiscovery\s+(?:day|programme|program)\b",
    r"\bopen\s+day\b",
    r"\btaster\b",
    r"\bshadow(?:ing)?\s+(?:day|programme|program)\b",
    # Campus recruiting events are insight-type by nature: a "Virtual Event"
    # or "Q&A" listing on a campus events board is an attend-this, not an
    # apply-to-this.
    r"\b(?:virtual|in[\s-]?person)\s+event\b",
    r"\brecruitment\s+event\b",
    r"\bq\s*&\s*a\b",
)

# Explicit internship signals. \b keeps "intern" off "Internal"/"International".
_INTERNSHIP = _rx(
    r"\bintern(?:ship)?s?\b",
    r"\bsummer\s+analyst\b",
    r"\bsummer\s+associate\b",
    r"\bwinter\s+analyst\b",
    r"\boff[\s-]?cycle\b",
    r"\bco[\s-]?op\b",
    r"\bindustrial\s+placement\b",
    r"\bplacement\s+year\b",
    r"\bsummer\s+placement\b",
    r"\b(?:student|work)\s+placement\b",
    r"\byear\s+in\s+industry\b",
    r"\bsummer\s+(?:programme|program)\b",
    r"\bsummer\s+scholar\b",
    r"\bpraktikum\b",              # German for internship (UBS/DB/European boards)
    r"\bwerkstudent\b",           # German working-student
    r"\bworking\s+student\b",
    "实习",                        # Chinese "internship/intern" (CICC/Beisen boards):
                                   # catches 实习生, 项目实习, 暑期实习. No \b — CJK has
                                   # no word boundaries; the term only appears in an
                                   # actual intern title, so precision holds.
)

# Experienced/HR veto. Runs AFTER insight+internship (an HR *internship* is
# still an internship) but BEFORE the entry-level and affinity rules, so
# "Graduate Recruitment Manager" and "Head of Diversity" land in `other`
# instead of masquerading as campus roles.
_SENIOR = _rx(
    r"\bsenior\b",
    # Abbreviated seniority, which the spelled-out list missed entirely. Live
    # consequence: "FP & A Sr. Lead Analyst S.V.P. - C14 - NEW YORK" contains
    # the neutral word "Analyst", so a campus-scoped board's hint promoted it
    # to entry_level and the recommender served a Senior Vice President job to
    # a sophomore as one of six picks. Dots optional because boards write both
    # "SVP" and "S.V.P.".
    r"\bsr\.?\b",
    r"\bs\.?v\.?p\.?\b",
    r"\be\.?v\.?p\.?\b",
    r"\bm\.?d\.?\b",
    r"\bexec(?:utive)?\s+director\b",
    # Citi grade codes. C10-C19 span senior analyst to managing director; a
    # campus posting never carries one, and the token is unambiguous.
    r"\bc1[0-9]\b",
    r"\bvp\b",
    r"\bvice\s+president\b",
    r"\bdirector\b",
    r"\bprincipal\b",
    r"\bhead\s+of\b",
    r"\bchief\b",
    r"\bmanager\b",
    r"\bexperienced\b",
    r"\bmid[\s-]?level\b",
    r"\blateral\b",
    r"\brecruit(?:er|ing|ment)\b",
    r"\btalent\s+acquisition\b",
)

# Full-time campus signals.
_ENTRY = _rx(
    r"\bgraduates?\b",
    r"\bnew\s+grad(?:uate)?s?\b",
    r"\bcampus\b",
    r"\bentry[\s-]?level\b",
    r"\bfull[\s-]?time\s+analyst\b",
    r"\banalyst\s+(?:development\s+)?(?:programme|program)\b",
    r"\brotational\s+(?:programme|program)\b",
    r"\btrainee\b",
    r"\bapprentice(?:ship)?\b",
    r"\buniversity\s+hire\b",
    r"\bearly\s+careers?\b",
    r"\bnew\s+analyst\b",          # Goldman Sachs' term for a full-time new-grad hire
    r"\bwmp\s+analyst\b",          # GS Wealth Management Program analyst (new grad)
    "校园招聘", "校招",             # Chinese campus recruitment (CICC/Beisen boards)
    "应届",                        # fresh graduate
    "管培生", "管理培训生",         # management trainee
)

# Eligibility/affinity events with no internship attached ("Women in Banking
# Programme — London 2026"). Checked last among the positive rules: a
# "Sophomore Summer Analyst" is an internship, not an event.
_AFFINITY = _rx(
    r"\bwomen\s+in\b",
    r"\bsophomore\b",
    r"\bfreshman\b",
    r"\bfirst[\s-]generation\b",
    r"\bdiversity\b",
)

# Neutral junior words that a campus-scoped board may promote to entry_level.
_NEUTRAL_JUNIOR = _rx(r"\banalyst\b", r"\bassociate\b", r"\bstudent\b")

# Board identifiers (Workday site slugs, Greenhouse tokens, ...) that mean the
# whole board is early-careers, e.g. "Blackstone_Campus_Careers", "Students",
# "RBCEARLYTALENT1", "solomonpartnersstudentsgraduates".
_CAMPUS_BOARD = _rx(r"campus", r"student", r"early", r"graduate", r"earlytalent")


def classify_role(title: str, *, campus_hint: bool = False) -> str:
    """Bucket a posting title. Rule order (load-bearing):

    1. insight events            ("Pre-Internship" must beat the intern rule)
    2. explicit internships
    3. experienced/HR veto       -> other
    4. full-time campus signals  -> entry_level
    5. affinity events           -> insight
    6. campus-board fallback: a neutral Analyst/Associate/Student title on a
       board that is itself campus-scoped is an entry-level hire
       ("Investment Banking Analyst" on a ...students board).
    7. everything else           -> other
    """
    t = title or ""
    if _INSIGHT.search(t):
        return INSIGHT
    if _INTERNSHIP.search(t):
        return INTERNSHIP
    if _SENIOR.search(t):
        return OTHER
    if _ENTRY.search(t):
        return ENTRY_LEVEL
    if _AFFINITY.search(t):
        return INSIGHT
    if campus_hint and _NEUTRAL_JUNIOR.search(t):
        return ENTRY_LEVEL
    return OTHER


# ---------------------------------------------------------------------------
# Two different years, and conflating them is the bug this section exists to
# prevent:
#
#   cohort      the PROGRAMME / INTAKE year the posting runs in. "2027 Summer
#               Internship - Account Analyst, Tokyo" is a 2027 programme. Say
#               nothing about who is eligible to apply to it.
#   class_year  the GRADUATION year the posting explicitly asks for, and only
#               when the posting says so in those words ("Class of 2028").
#
# On the live open set (~4,000 roles) cohort is present on ~10% of rows and is
# a programme year in essentially all of them; exactly three titles state a
# class year out loud. So a year filter or a card chip labelled "Class" would
# be factually wrong for ~99% of the feed. Hence two fields, two extractors,
# and a hard rule: `extract_class_year` NEVER derives, infers, or offsets a
# year from a programme year. If the posting didn't say it, the answer is "".
# ---------------------------------------------------------------------------

# The plausible-year window, shared by both extractors so "Founded in 1999" and
# a 4-digit requisition code can never pose as a year.
_YEAR = r"20(?:2[4-9]|3[0-5])"

_COHORT_YEAR = re.compile(rf"\b{_YEAR}\b")

# Explicit class-year statements only: "Class of 2028", "Class 2028",
# "(Class of 2028)". The lookbehind rejects a hyphen or word character before
# "class", which is what keeps "world-class 2027 programme" and the very real
# "Investment Banking, Classic — Summer Analyst" family off this field. The
# year must follow "class" directly (optionally via "of"), so a bare year
# anywhere else in the title is ignored no matter how suggestive it looks.
_CLASS_YEAR = re.compile(rf"(?<![\w-])class\s+(?:of\s+)?({_YEAR})\b", re.IGNORECASE)


def extract_cohort(title: str) -> str:
    """First plausible cohort year (2024-2035) in the title, else "".
    "2027 Summer Analyst Program" -> "2027". Never fabricates a year.

    This is a PROGRAMME year, not a graduation year — see the section comment
    above before displaying or filtering on it."""
    m = _COHORT_YEAR.search(title or "")
    return m.group(0) if m else ""


def cohort_from_provider_title(raw: dict | None) -> str:
    """The programme year read from the provider's ORIGINAL title, for rows
    whose display title lost it in cleaning.

    Goldman's board is the whole caseload: its `jobTitle` leads with the year
    ("2027 | APAC | Japan | Tokyo | Operations | New Analyst") and the
    connector keeps only the human tail — correctly for display, but all 142
    live GS campus rows were filed under "No Year Stated" while every one of
    them stated its year in the payload we already store. Same posture as the
    raw-title region fallback in reclassify: the provider's own routing, not
    a guess, and the plausible-year window keeps requisition codes out."""
    r = raw or {}
    return extract_cohort(r.get("jobTitle") or r.get("title") or "")


def extract_class_year(title: str) -> str:
    """The graduation year a posting EXPLICITLY states, else "".

    "Class of 2027 Investment Analyst"        -> "2027"
    "Financial Analyst (Class of 2027)"       -> "2027"
    "2027 Summer Intern ... (Class of 2028)"  -> "2028"   (cohort would be 2027)
    "2027 Summer Analyst Program"             -> ""       (that is a cohort)

    The last case is the whole point of this function. A 2027 summer programme
    is typically read by recruiters as targeting 2028 graduates, but that
    mapping varies by firm, region, and degree length, and the posting did not
    say it. Deriving it here would put a confident graduation year on 89% of
    the feed that no source ever stated. Blank means "not stated", which is a
    true answer and the one the UI is built to show."""
    m = _CLASS_YEAR.search(title or "")
    return m.group(1) if m else ""


# The provider's own contract-type vocabulary, where a board states one as a
# structured field (Talentsoft's card list, SocGen's sourcestr8). This is a
# STRONGER signal than any title heuristic — it is the firm's own filing of
# the role — and it is what classifies the French-titled internship the title
# rules cannot read ("Chargé de communication interne H/F" filed under
# Apprenticeship). Matched as substrings of the lowered value so
# "Internship / Trainee (with agreement)" and "VIE Programme" both land.
_CAMPUS_CONTRACT_KEYS = (
    "internship", "trainee", "graduate", "apprentice", "vie", "stage",
    "alternance", "placement", "working student", "cooperative", "co-op",
)


def contract_is_campus(contract_type: str | None) -> bool:
    """True when a provider-stated contract type names a campus programme."""
    t = (contract_type or "").lower()
    return bool(t) and any(k in t for k in _CAMPUS_CONTRACT_KEYS)


def bucket_from_contract(contract_type: str | None) -> str:
    """The bucket a provider's own filing decides, or "" when it decides
    nothing.

    A hint was not enough. 51 open roles sat in `other` carrying explicit
    Internship/Trainee, Apprenticeship, VIE or COOPERATIVE filings, because
    the hint only breaks ties on NEUTRAL titles — and "Gestionnaire des
    processus H/F" is not neutral to a classifier that reads no French, it
    is opaque. When the firm has already answered the exact question the
    title rules exist to guess at, the answer outranks the guess: graduate
    programmes are entry-level hires, every other campus filing is an
    internship-family programme. Blank or unrecognised filings decide
    nothing and fall through to the title rules unchanged.
    """
    t = (contract_type or "").lower()
    if not t or not any(k in t for k in _CAMPUS_CONTRACT_KEYS):
        return ""
    if "graduate" in t:
        return "entry_level"
    return "internship"


def board_is_campus(board) -> bool:
    """True when a board is campus-scoped. Some connectors are campus-only
    by construction (a fixed campus query, no identifier to sniff); the
    rest are sniffed by their provider-specific identifying field (Workday
    `site`, Greenhouse `token`, Lever `org`, tal.net `board_url`, sitemap
    `path_filter`) — duck typing keeps this module dependency-free."""
    if board.__class__.__name__ in ("GoldmanSachsBoard", "McKinseyBoard"):
        return True
    ident = " ".join(
        str(getattr(board, attr, "") or "")
        for attr in ("site", "token", "org", "board_url", "path_filter")
    )
    return bool(_CAMPUS_BOARD.search(ident))


# ---------------------------------------------------------------------------
# Prose extraction over the posting's own text (title + stored raw payload).
#
# These exist because the answers were ALREADY IN the fetched data and being
# thrown away: postings state "unable to sponsor" in body text while the
# sponsorship column read unknown on all 4,319 scraped rows, and a minority
# state an application deadline in prose while the deadline column was null
# on 99% of them. Both extractors are deliberately conservative — on shared
# directory data, a wrong "sponsors" or a fabricated deadline is worse than
# an honest unknown, so anything ambiguous returns the null answer.
# ---------------------------------------------------------------------------

# Negative phrasings FIRST, and checked first: sponsorship language is mostly
# negation, and several negative phrases contain a positive one verbatim
# ("no visa sponsorship available" contains "visa sponsorship available").
_SPONSOR_NO = (
    "unable to sponsor", "not able to sponsor", "cannot sponsor",
    "will not sponsor", "does not sponsor", "not provide sponsorship",
    "not provide visa sponsorship", "not offer sponsorship",
    "not offer visa sponsorship", "no visa sponsorship",
    "sponsorship is not available", "sponsorship will not be",
    "without sponsorship", "without the need for sponsorship",
    "without the need for visa sponsorship", "now or in the future",
    "not eligible for sponsorship",
)
_SPONSOR_YES = (
    "visa sponsorship available", "sponsorship is available",
    "will sponsor", "provides visa sponsorship", "offers visa sponsorship",
    "provide visa sponsorship", "offer visa sponsorship",
    "h-1b sponsorship", "h1b sponsorship",
)


def extract_sponsorship(text: str | None) -> str:
    """"yes" / "no" / "unknown" from posting prose.

    "now or in the future" is in the NO list because it is the standard US
    phrasing of exactly that answer ("must be authorized to work ... without
    the need for sponsorship now or in the future") and never appears in a
    sponsoring posting's boilerplate.
    """
    t = (text or "").lower()
    if not t:
        return "unknown"
    if any(k in t for k in _SPONSOR_NO):
        return "no"
    if any(k in t for k in _SPONSOR_YES):
        return "yes"
    return "unknown"


_MONTHS = {m: i + 1 for i, m in enumerate((
    "january", "february", "march", "april", "may", "june", "july",
    "august", "september", "october", "november", "december"))}
# A deadline KEYWORD, then a date within the next ~80 characters. The keyword
# gate is what keeps this conservative: a bare date in a posting is usually a
# start date or a posted date, and treating those as deadlines would put
# false countdowns on the feed.
_DEADLINE_KEY = re.compile(
    r"(?:apply by|application deadline|applications?\s+(?:close[sd]?|due|must be "
    r"(?:received|submitted))|closing date|deadline)", re.IGNORECASE)
_DATE_ISO = re.compile(r"(20\d{2})-(\d{2})-(\d{2})")
_DATE_MDY = re.compile(
    r"(january|february|march|april|may|june|july|august|september|october|"
    r"november|december)\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(20\d{2})", re.IGNORECASE)
_DATE_DMY = re.compile(
    r"(\d{1,2})(?:st|nd|rd|th)?\s+(january|february|march|april|may|june|july|"
    r"august|september|october|november|december),?\s+(20\d{2})", re.IGNORECASE)


def extract_deadline_from_text(text: str | None) -> str | None:
    """An ISO date string, only when a deadline keyword sits within 80
    characters before a fully-specified date (a 4-digit year is required —
    "closes January 5" with no year is a guess, and guesses don't ship)."""
    t = text or ""
    if not t:
        return None
    for key_match in _DEADLINE_KEY.finditer(t):
        window = t[key_match.end():key_match.end() + 80]
        iso = _DATE_ISO.search(window)
        if iso:
            y, m, d = int(iso.group(1)), int(iso.group(2)), int(iso.group(3))
        else:
            mdy = _DATE_MDY.search(window)
            dmy = _DATE_DMY.search(window)
            if mdy:
                y, m, d = int(mdy.group(3)), _MONTHS[mdy.group(1).lower()], int(mdy.group(2))
            elif dmy:
                y, m, d = int(dmy.group(3)), _MONTHS[dmy.group(2).lower()], int(dmy.group(1))
            else:
                continue
        try:
            return date(y, m, d).isoformat()
        except ValueError:
            continue  # "February 30" — keep scanning, don't invent
    return None


def posting_text(title: str | None, raw: dict | None) -> str:
    """Every string in the posting, flattened for the extractors: the title
    plus all string values in the provider's raw payload, recursively.
    Capped, because a payload is untrusted input and one pathological
    posting must not make extraction quadratic."""
    parts: list[str] = [(title or "")]

    def walk(node, depth=0):
        if depth > 6 or sum(len(p) for p in parts) > 40_000:
            return
        if isinstance(node, str):
            parts.append(node)
        elif isinstance(node, dict):
            for v in node.values():
                walk(v, depth + 1)
        elif isinstance(node, (list, tuple)):
            for v in node:
                walk(v, depth + 1)

    walk(raw or {})
    return "\n".join(parts)[:40_000]
