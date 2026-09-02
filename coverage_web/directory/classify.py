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
    # "Other" is what this bucket is to the classifier — the residue after
    # three positive tests. It is not what it is to a reader: every row in it
    # is an experienced hire. A firm page headed "Other 912" told nobody what
    # the 912 were, while the only place the app used the honest word was the
    # feed's own scope caption. The value stays `other`; the word a person
    # reads is the word for the thing.
    OTHER: "Experienced",
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

# Human labels for the firms.yaml track slugs. Single source of truth for
# the same reason REGION_LABELS/TRACKED_REGIONS are above it:
# accounts/forms.py's Settings checkboxes and directory/views.py's
# Opportunities filter used to hardcode this vocabulary separately and
# drifted — Settings offered "Private Equity", the filter called the same
# slug "Private Equity / Credit" (the firms actually tagged "pe" include
# credit shops: Apollo, Ares, Blue Owl, Golub Capital, HPS, Oaktree, Sixth
# Street, not just buyout funds, so the fuller label is the accurate one).
#
# A LABEL HERE IS NOT AN OFFER. Every slug the STORAGE vocabulary carries
# needs a human name, because a firm tagged with it still prints its desk on
# its own card; what a student may TARGET is `SELECTABLE_TRACKS` below.
TRACK_LABELS = {
    "ib": "Investment Banking",
    "st": "Sales & Trading",
    "pe": "Private Equity / Credit",
    "am": "Asset Management",
    "consulting": "Consulting",
    "corp-strat": "Corporate Strategy",
}

# THE STORAGE VOCABULARY. `FirmDate.track`'s CHECK constraint is built from
# this tuple (see directory/models.py and migration 0014), so a value cannot
# be deleted from it without a schema migration, and the nine firms tagged
# `corp-strat` keep their tag: a student who knows someone at Google should
# still be able to log it.
TRACKED_TRACKS = ("ib", "st", "pe", "am", "consulting", "corp-strat")

# Retired 2026-09-02 (D-3). `corp-strat` returned zero open rows in any
# bucket, 5 open campus rows named it by title against 602 for ib, and not
# one firm tagged with it has a connector — the scope cut of 2026-07-23 kept
# tech off the board deliberately (`directory/boards.py`, "Deliberately
# absent"). Seven of the founder's 54 tiered firms carry it and six of them
# have nobody at all, a quarter of his 24 zero-contact tiered firms: coverage
# work manufactured by a track the board cannot fill. A track that is
# selectable and returns nothing is a promise, so it stops being offered.
#
# RETIRED, NOT DELETED. The slug stays in TRACKED_TRACKS (the constraint),
# in TRACK_LABELS (the firm cards) and on the nine firms. What changes is
# that nobody can newly choose it, nothing counts it as a preference, and no
# surface offers it as somewhere to want to work.
RETIRED_TRACKS = ("corp-strat",)

# THE PICKER VOCABULARY: what a student may state a preference for, and the
# only track set any facet, archetype table or fit rule may read. Derived,
# never restated — a seventh track added above is selectable the same day.
SELECTABLE_TRACKS = tuple(t for t in TRACKED_TRACKS if t not in RETIRED_TRACKS)


def selectable_tracks(values):
    """`values` (a stored `User.tracks` list) as the product reads it today:
    retired slugs dropped, order and duplicates otherwise untouched.

    THE READ-TIME DEGRADE, and the reason D-3 needs no data migration. A
    profile holding `['ib', 'corp-strat']` answers `['ib']` here, which is an
    already-supported state (a one-track student), and the row itself is left
    alone until the student next saves Settings — where the checkbox is gone,
    so the save drops it. Nothing rewrites a student's stated preferences
    behind their back to make a product decision true."""
    return [t for t in (values or []) if t in SELECTABLE_TRACKS]


# Checked in order; the first matching market wins. Keys are lowercase
# substrings (city / country / region tokens).
_REGION_KEYS: tuple[tuple[str, tuple[str, ...]], ...] = (
    # ", hkg" / ", jpn" / ", rou": ISO-3166 alpha-3 codes, which SocGen's
    # board emits as the ENTIRE location field (", HKG"). Comma-prefixed so a
    # word containing the trigram can't fire; ", rou" also sits inside a
    # hypothetical ", Rouen" — France, so the same eu answer either way.
    ("hk", ("hong kong", "hongkong", "香港", "hksar", ", hkg",
            # 2026-08-09 non-campus census: banks file HK roles by BUILDING,
            # never by city. One Island East is the Taikoo Place tower (110
            # rows), Kwun Tong is a district. "The Center" is also a Hong
            # Kong tower but far too generic as a substring, so it lives in
            # the exact-match table instead.
            "one island east", "kwun tong")),
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
        # 2026-08-09, from the census after the page cap lifted and the
        # campus set grew by 276 rows. Safe as substrings — none of these
        # sits inside a US place name, which is the hazard the exact-match
        # table below exists for. "zürich" is spelled with the umlaut
        # because the ASCII "zurich" key above cannot see it.
        "zürich", "treviso", "neuilly", "sarajevo", "bratislava",
        "ruggell", "saint helier", "saint peter port", "rubano", "lublin",
        # 2026-08-09 non-campus census. "wrocław" is the Polish spelling the
        # ASCII "wroclaw" key cannot see; "pavilion drive" is Barclays'
        # Northampton campus, keyed on the street because Northampton,
        # Massachusetts is also real.
        "barcelona", "boadilla del monte", "nicosia", "katowice", "wrocław",
        "marseille", "bologna", "knutsford", "radbroke", "edinburgh",
        "mönchengladbach", "pavilion drive",
        # Workday's job slugs strip accents, so the accented keys above
        # cannot see their own cities: "Zürich" arrives as "Zrich". Keyed on
        # the mangled spelling as well — it collides with nothing.
        "zrich", "mnchengladbach",
        # Offices named without their city, each unique to one: Barclays'
        # Cannon Street (London) and Kingswood (Surrey), Deutsche's
        # Kronberg, and Spinningfields, which is Manchester's financial
        # district and unlike "Manchester" cannot be New Hampshire.
        "cannon street", "kingswood", "kronberg", "spinningfields", "ghent",
        # The campus tail, 2026-08-09. "genève" is the accented spelling the
        # ASCII "geneva" key misses. Dnipro and Kishinev are named where
        # only their countries were keyed.
        "genève", "basel", "padua", "padova", "firenze",
        "palma de mallorca", "zagreb", "croatia", "kishinev", "chisinau",
        "dnipro",
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
        # 2026-08-09: "Vineland, New Jersey" spells the state out, which the
        # ", NJ" suffix pass cannot see; Anchorage arrives bare.
        "new jersey", "anchorage",
        # 2026-08-09 non-campus census: American sites filed by campus or
        # street. Whippany is Barclays' New Jersey campus, "Madison Ave" the
        # Manhattan address. Every Jacksonville and Saint Louis in the set
        # is American whichever state it is in.
        "whippany", "madison ave", "jacksonville", "saint louis",
        "south carolina",
        # Spelled-out states from Workday slugs, which write "MINNEAPOLIS MN"
        # with no comma for the suffix rule to key on. "oregon" is spelled
        # out deliberately: ", or" as a code would fire inside the ordinary
        # phrase "London, or Paris", which the boundary cannot catch because
        # a space follows either way.
        "michigan", "nevada", "utah", "idaho", "oregon", "iowa", "delaware",
    )),
    # After hk so "香港" never falls through to the mainland bucket.
    ("cn", (
        "beijing", "shanghai", "shenzhen", "guangzhou", "hangzhou", "chengdu",
        "北京", "上海", "深圳", "广东", "广州", "杭州", "成都", "中国",
        "mainland china", ", china",
        # Bare "China" as the whole location string (two live rows). Safe at
        # this position: any US-city-plus-"China Basin"-style address hits
        # the US tier first, which runs before this one.
        "china", "dalian",
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
    # 2026-08-09 census: Southeast and Central Asian offices that arrived
    # with the wider fetch. Each is unambiguous as a substring.
    "makati", "jaipur", "ipoh", "penang", "johor bahru", "kuching",
    "bermuda", "ulaanbaatar", "tashkent", "bandar seri begawan",
    "yogyakarta", "guadalajara",
    # 2026-08-09 non-campus census. Canada is comma-prefixed on purpose:
    # bare "ontario" would take Ontario, California, and the tracked
    # American tier runs BEFORE this one only when the row says ", CA".
    ", ontario", ", alberta", ", british columbia", ", manitoba",
    ", québec", ", quebec", ", canada",
    # Latin America.
    "panamá", "cdmx", "managua", "asunción", "caracas", "santo domingo",
    "monterrey",
    # India and the Philippines, filed by office park as HK is by tower.
    "oberoi garden city", "vikhroli", "indira nagar", "pasig", "iloilo",
    # Accent-stripped Workday slugs again: "Montral Qubec", "Saint Lonard
    # Qubec". "qubec" and "montral" are misspellings that collide with
    # nothing, which is exactly what makes them safe keys.
    "qubec", "montral", ", yukon", "montevideo",
    # Campus tail: cities in markets already keyed only by country name
    # (Kazakhstan, Ecuador) plus two more Southeast Asian offices.
    "astana", "almaty", "atyrau", "guayaquil", "cebu", "melaka", "surabaya",
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
    # ak/sc/ks joined 2026-08-09 (Anchorage AK, Summerville SC, Olathe KS).
    # The boundary keeps them honest: ", Akron" and ", Scotland" cannot fire
    # because a letter follows the code.
    r",\s*(?:ny|il|ma|ca|wa|tx|ga|nc|fl|tn|pa|co|md|ct|nj|va|ak|sc|ks|az|mo"
    r"|mn|ia|nv|ut|id|mi|usa?)(?![a-z])",
    re.IGNORECASE)
_US_STATE_KEYS = frozenset({", ny", ", il", ", ma", ", ca", ", wa", ", tx",
                            ", ga", ", nc", ", fl", ", tn", ", pa", ", co",
                            ", md", ", ct", ", nj"})


# Cities whose names are REAL PLACES IN AMERICA when read as substrings, and
# which this table therefore matches only as the entire location field. The
# European tier runs before the American one, so adding "rome" or "trento" to
# the key list above would have sent Romeoville, Illinois to Italy and
# Trenton, New Jersey to the Alps — "trento" is a substring of "trenton".
#
# An exact test is honest here because these arrive as the whole field: the
# board's location really is the single word "Rome".
_EXACT_CITY: dict[str, str] = {
    "rome": "eu", "verona": "eu", "trento": "eu", "palermo": "eu",
    "bern": "eu", "lucerne": "eu", "belgrade": "eu",
    # Valencia is a Spanish city and a Californian one; "the center" is a
    # Hong Kong tower and an ordinary English phrase. Both are answerable
    # only when they are the whole field.
    "valencia": "eu", "the center": "hk",
    # Nassau in the Bahamas, and Nassau County, New York. Whole-field only.
    # ("Naples", bare "San Jose" and "Lima" stay out entirely — Naples,
    # Florida, San Jose, California and Lima, Ohio are all common enough in
    # this industry that even the whole field is not decisive. Lima was an
    # existing deliberate refusal; five live rows are not a reason to
    # overturn it.)
    "nassau": "other",
    # Florence, South Carolina is a real posting location for American
    # banks, and Europe is tested before America, so the Italian city can
    # only be read from the whole field. ("firenze" stays a substring — no
    # American city answers to it.)
    "florence": "eu",
}


def normalize_region(location: str | None, *, fallback: str = "") -> str:
    """Map a free-text location to a tracked market code, to "other" for a
    stated location outside the tracked markets, or to `fallback` (default
    "") when the text names no recognisable place at all."""
    text = (location or "").lower()
    if not text:
        return fallback
    # Whole-field city names first — see _EXACT_CITY for why these cannot be
    # substrings.
    exact = _EXACT_CITY.get(text.strip())
    if exact:
        return exact
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
# ("(Summer 2027)") has a space and is left alone. A bare 4-digit year
# ("(2026)", "(2027)") is explicitly excluded even though it has no space:
# IMC posts "Graduate Software Engineer (2026)" and "... (2027)" as two
# distinct, currently-open cohort postings (Amsterdam job ids 4564480101 and
# 4667814101) whose titles differ ONLY in that suffix — stripping it made
# both clean to "Graduate Software Engineer", which directory/dupes.py then
# folds into one row, silently hiding the 2027 posting from the board. A req
# code is never a bare calendar year with nothing else in the brackets, so
# excluding that one shape costs nothing real req codes need.
_REQ_CODE = re.compile(
    r"\s*[\(\[](?=[^)\]\s]*\d)(?!(?:19|20)\d{2}[\)\]])[A-Za-z0-9][\w.\-/]*[\)\]]\s*$"
)
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


# Insight events. "insight" alone is NOT enough — "Market Insights Analyst",
# "Insight & Design Lead" and "AI/Insight Senior Associate" are all real jobs
# on the live boards — so it must be qualified by an event/programme word or
# an early/spring prefix.
#
# Everything in this list is an ATTEND-THIS, never an apply-to-this, which is
# why it is allowed to run ahead of the internship rule and the seniority
# veto: an information session ABOUT a summer analyst programme is an event,
# not the internship, and "Career Insights: Meet our recruiters" must not lose
# to the word "recruiters".
_INSIGHT = _rx(
    # US early-ID programmes name the same event with different nouns than the
    # UK ones this list was built from. "BofA Campus Insight Forum" fell all
    # the way through to `_ENTRY`, which matches the bare word "campus", and
    # was filed as an entry-level JOB - the bucket a graduating senior applies
    # to - for an event aimed at first- and second-years. Every noun here is
    # still qualified by "insight", so this widens the event vocabulary
    # without loosening the guard the comment above describes. The possessive
    # ("Insight's Day") and "workshop|symposium" came from a second pass over
    # the live titles.
    r"insights?(?:\u2019s|'s)?\s+(?:day|week|programme|program|event|evening"
    r"|series|session|forum|summit|conference|workshop|symposium)",
    # "Career Insights: Meet the Business - Banking (NAM Session)" is Citi's
    # name for exactly the attend-this-don't-apply-to-it event the comment
    # above describes (17 live rows). The colon is what kept these out of the
    # qualifier list above, and the colon is also what keeps "Career Insights
    # Manager", a job, out of this bucket - so it stays anchored.
    r"\bcareer\s+insights?\s*:",
    # "Insight into Operations Opportunities"; Citi files the same shape as
    # "Citi Singapore - Insight to Services".
    r"\binsights?\s+(?:in)?to\b",
    r"(?:early|spring|first[\s-]?year|1st[\s-]?year)\s+insights?\b",
    r"\bspring\s*week\b",
    r"\bpre[\s-]?internship\b",
    r"\bdiscovery\s+(?:day|programme|program|event|series|session|summit|forum)\b",
    r"\bopen\s+day\b",
    r"\btaster\b",
    r"\bshadow(?:ing)?\s+(?:day|programme|program|event|session)\b",
    r"\bjob\s+shadow\w*\b",
    # US early-identification vocabulary. "Early ID" is the term of art for a
    # first/second-year pipeline programme and appears in no other kind of
    # posting; an externship is a shadowing stint by another name.
    r"\bearly\s+id(?:entification)?\b",
    r"\bexternship\b",
    r"\bcase\s+competition\b",
    r"\bmentorship\s+(?:programme|program)\b",
    # Optiver runs its student insight days as "Career Kickstarter - Trading
    # 2026" (7 live rows). Spelled out rather than a bare "kickstart" so
    # PwC's "Career Start in Audit" — a graduate JOB — cannot match.
    r"\bcareer\s+kick[\s-]?start(?:er)?\b",
    # Campus recruiting events are insight-type by nature: a "Virtual Event"
    # or "Q&A" listing on a campus events board is an attend-this, not an
    # apply-to-this.
    r"\b(?:virtual|in[\s-]?person)\s+event\b",
    r"\brecruit(?:ment|ing)\s+event\b",
    r"\bq\s*&\s*a\b",
    # Named event formats. Each is a word that never titles a job: there is no
    # "Seminar Analyst" or "Coffee Chat Manager". "Networking" and
    # "presentation" ARE job words ("Windows Networking Engineer",
    # "Presentation Designer"), so both are qualified by what follows or
    # precedes them.
    r"\bseminars?\b",
    r"\bwebinars?\b",
    r"\bopen\s+house\b",
    r"\bfireside\s+chat\b",
    r"\bcoffee\s+chats?\b",
    r"\binfo(?:rmation)?\s+session\b",
    r"\bnetworking\s+(?:event|evening|session|night|dinner|lunch(?:eon)?"
    r"|breakfast|reception|drinks|social)\b",
    r"\b(?:recruitment|recruiting|career|welcome|dinner)\s+(?:dinner"
    r"|lunch(?:eon)?|breakfast|reception|drinks|social)\b",
    r"\b(?:firmwide|corporate|company|campus|career|university)\s+presentation\b",
    r"\b(?:summer|autumn|spring|winter|career|networking|open|student)\s+evening\b",
    # "Meet the Business", "Meet our Analysts", "Meet our recruiters" — an
    # imperative verb where a job title would carry a noun.
    r"\bmeet\s+(?:our|the)\b",
    # "Meet and Greet our Traders: Structuring, Corporates and Exotics
    # Trading" (Morgan Stanley, live) puts the conjunction between the verb
    # and its object, so `meet\s+(our|the)` cannot see it and the row fell
    # through every positive rule to `other`. "Meet and greet" is a fixed
    # phrase for an event and titles no job on any board in the catalog.
    r"\bmeet\s+(?:and|&)\s+greet\b",
    # A hackathon is a recruiting event wearing a competition's name, and the
    # word titles no job — the sibling `_EARLY_ID` list already carries
    # "trading/case/quant ... challenge" for exactly this shape but is
    # checked after the entry-level rule, which is too late here: `_ENTRY`'s
    # bare "campus" was promoting "EU Campus - Aarhus Hackathon" to a
    # full-time hire. Unqualified because, unlike "challenge" or
    # "competition", "hackathon" has no antitrust-practice or product-name
    # homograph on these boards.
    r"\bhackathons?\b",
)

# Explicit internship signals. \b keeps "intern" off "Internal"/"International".
_INTERNSHIP = _rx(
    r"\bintern(?:ship)?s?\b",
    r"\bsummer\s+analyst\b",
    # THE YEAR IN THE MIDDLE. `summer\s+analyst` is adjacency-only, and Bank
    # of America writes the same programme with its intake year between the
    # two words: "Corporate Audit, Summer 2027 Analyst - Chester", "Global
    # Operations Operations Analyst Summer 2027 - London". Measured
    # 2026-09-02: 11 open BofA rows in both spellings were filed
    # `entry_level` — a FULL-TIME graduate hire — by the campus-board
    # fallback matching the bare word "Analyst", which is the worst possible
    # miss for a sophomore reading the board. Both orders, because the live
    # board carries both. The year is anchored to 20\d\d rather than \d{4}
    # so a desk number ("Analyst Summer 4021") cannot match.
    r"\bsummer\s+20\d\d\s+analyst\b",
    r"\banalyst\s+summer\s+20\d\d\b",
    r"\bintern\s+summer\s+20\d\d\b",
    r"\bsummer\s+associate\b",
    r"\bwinter\s+analyst\b",
    # Virtu's "Women's Winternship" — a winter internship spelled as one
    # word, which \bintern\b cannot see. Without this the affinity rule
    # below would call it an event.
    r"\bwinternship\b",
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
    # "Class of 2027 Investment Analyst" (Houlihan Lokey, live row) states a
    # graduating class outright, which is a stronger campus signal than any
    # other rule in this list and carries no false-positive risk — no
    # experienced-hire posting is ever titled "Class of YYYY". Checked after
    # the senior veto like every other _ENTRY pattern, so "Class of 2027
    # Managing Director" (hypothetical, never seen live) would still veto.
    # Year range matches `_YEAR` below; inlined because _YEAR is defined
    # later in the module and this list is built at import time.
    r"\bclass\s+of\s+20(?:2[4-9]|3[0-5])\b",
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

# "CAMPUS" ALONE IS NOT A HIRE. The bare word used to sit in `_ENTRY` above,
# which made it a full-time-graduate signal on its own — and the word is the
# firm's own name for the CHANNEL, not for the thing being offered on it. It
# is equally at home on an event ("2026 Fall Campus Showcase"), a talent
# pool ("2027 EU Campus Programme Talent Community") and an internal HR job
# ("Campus Sourcer", "Campus Talent Associate"), and the plan's own example
# is "EU Campus - Aarhus Hackathon" arriving as an entry-level job.
#
# So "campus" now has to co-occur with a word that names a ROLE OR A HIRING
# LEVEL. The list below is deliberately about the thing being hired, not
# about the audience: "programme", "talent" and "career" are all campus-event
# words as often as they are hiring words and would let the four rows above
# straight back through.
#
# Measured 2026-09-02 on the live board: 24 open rows carried a bare
# "campus" in an `entry_level` title. 19 keep the bucket (every "Campus X
# (Full-Time)" at Jump and Five Rings, plus SocGen's "Research Associate
# (Campus)"); 5 leave it — the Fall Campus Showcase, the Aarhus hackathon,
# the EU Campus talent community, "Campus Sourcer" and "From Campus to
# Career: Interview Tips that Work". Two of those five land in `insight` on
# their own event words and three in `other`, which is where an HR job and a
# talent pool belong. Nothing that names a real campus hire moved.
_ENTRY_CAMPUS = re.compile(
    r"\bcampus\b(?=.*\b(?:analysts?|associates?|full[\s-]?time|graduates?"
    r"|hires?|traders?|engineers?|researchers?|scientists?|developers?)\b)"
    r"|\b(?:analysts?|associates?|full[\s-]?time|graduates?|hires?|traders?"
    r"|engineers?|researchers?|scientists?|developers?)\b(?=.*\bcampus\b)",
    re.IGNORECASE,
)

# US early-identification programmes, whose vocabulary overlaps with real job
# titles and so is checked AFTER the seniority veto and the entry-level rule
# rather than with the unambiguous event words in `_INSIGHT`. That ordering is
# the precision guard, and it is doing real work on the live boards:
#
#   "Point72 Academy 2026 Investment Analyst Program for Experienced
#    Professionals"        -> `other`       (seniority veto, rule 3)
#   "US Graduate Leadership Program - Commercial Banking"
#                          -> `entry_level` (rule 4 — a hiring programme)
#   "Advanced Polytechnic Graduate Pathway Program"
#                          -> `entry_level` (rule 4)
#
# Words deliberately NOT in this list, each because the live boards carry a
# counter-example that would become a false positive: "academy" ("AI
# Instructor, Point72 Academy"), "leadership program" (Vanguard's Technology
# Leadership Program is a hire), "pathway" (Vanguard's "Sales Pathway, Senior
# Associate"), "edge" (CIBC's "Premium Edge" relationship managers),
# "conference" ("Conference Services Event Planner"), "competition"
# (Brattle's competition-economics analysts), bare "fellowship" (DBS's
# "Return to Work Fellowship", SIG's "Faculty Fellow") and bare "series"
# (Raymond James' "Series 7 & 63 required").
_EARLY_ID = _rx(
    # "Discover Nomura", "Discover our Institutional Securities Group",
    # "Explore Nomura - SEO Programme". \b is what keeps this off the 6 live
    # "eDiscovery" rows, and "discovery" stays qualified in `_INSIGHT` so
    # "Service Now Discovery Infra Administrator" cannot match. The lookahead
    # is for Discover Financial Services, a real firm.
    r"\bdiscover(?!\s+(?:financial|bank|card))\b",
    r"\bexplore\b",
    r"\blaunch\s+your\s+career\b",
    r"\b(?:future|emerging|rising|tomorrow(?:\u2019s|'s)?|next[\s-]?gen(?:eration)?)"
    r"\s+leaders?\b",
    r"\bfellowship\s+(?:programme|program)\b",
    r"\b(?:insider|speaker|spotlight|possibilit\w+|leaders?|career|campus"
    r"|student|virtual|event|discovery|insight)\s+series\b",
    # An event noun needs a campus/affinity qualifier, but not an adjacent one
    # — "Inclusion by Insight Virtual 2 Day Summit" puts three words between
    # them. "Inclusion" earns a place here as a qualifier while staying out of
    # `_AFFINITY`, where it would have caught Oliver Wyman's three live
    # "Inclusion & Culture Generalist" jobs.
    r"\b(?:career|insight|leaders?|campus|student|women(?:\u2019s|'s)?|diversity"
    r"|inclusion|networking|early)\b(?:\s+\w+){0,3}\s+(?:forum|summit|symposium)\b",
    r"\bworkshop\b",
    r"\bimmersion\b",
    # Trading/case competitions are recruiting events wearing a game's name.
    # Qualified because "Competition" alone is an antitrust practice area.
    r"\b(?:quant\w*|trading|investment|case|stock)(?:\s+\w+){0,2}\s+challenge\b",
)

# Eligibility/affinity events with no internship attached ("Women in Banking
# Programme — London 2026"). Checked last among the positive rules: a
# "Sophomore Summer Analyst" is an internship, not an event.
_AFFINITY = _rx(
    # Broadened from "women in": the live boards run the same programme as
    # "Women Who Lead", "Women Networking", "Women\u2019s Immersion Programme"
    # and "Connecting & Resourcing Empowered Women". Across 26k live titles
    # every \bwomen\b row is an affinity event or an internship already caught
    # by rule 2 — there is no "Women\u2019s <job>" on these boards.
    r"\bwomen(?:\u2019s|'s)?\b",
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
    4. full-time campus signals  -> entry_level, including "campus" only
       where it co-occurs with a role or level word (see `_ENTRY_CAMPUS`)
    5. US early-ID programmes    -> insight
    6. affinity events           -> insight
    7. campus-board fallback: a neutral Analyst/Associate/Student title on a
       board that is itself campus-scoped is an entry-level hire
       ("Investment Banking Analyst" on a ...students board).
    8. everything else           -> other

    Steps 1 and 5 are both insight vocabularies, split by how ambiguous the
    words are. Step 1 holds words that never title a job, so it may outrank
    the internship rule and the seniority veto. Step 5 holds words that also
    appear in real hires ("Discover", "Workshop", "Future Leaders"), so it
    sits behind both vetoes AND behind the entry-level rule — which is what
    keeps "US Graduate Leadership Program" an entry-level hire rather than an
    event. Moving step 5 earlier would break that; see `_EARLY_ID`.

    STEP 1 BEFORE STEP 2 IS THE CONTRACT, not an accident of ordering, and
    the new event words (`meet and greet`, `hackathon`) inherit it: an
    information session ABOUT a summer analyst programme is the session, not
    the internship, so `_INSIGHT` is allowed to outrank `_INTERNSHIP`. What
    the ordering must never do is let a merely SUGGESTIVE word demote a real
    internship, which is why every word in `_INSIGHT` is one that titles no
    job on these boards and every ambiguous one lives in `_EARLY_ID` behind
    step 4 instead. `test_stress_classify.py` pins both halves.
    """
    t = title or ""
    if _INSIGHT.search(t):
        return INSIGHT
    if _INTERNSHIP.search(t):
        return INTERNSHIP
    if _SENIOR.search(t):
        return OTHER
    if _ENTRY.search(t) or _ENTRY_CAMPUS.search(t):
        return ENTRY_LEVEL
    if _EARLY_ID.search(t):
        return INSIGHT
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


# ---------------------------------------------------------------------------
# THE DERIVED graduation year — a convention, not a quotation.
#
# `class_year` above refuses to infer, and stays that way: it is the column
# that means "the posting said so", and code and UI both trust it as such.
# This is the separate, clearly-labelled answer to a different question —
# "who is this programme conventionally for?" — and it lives in its own
# column, is rendered as an inference with its reasoning attached, and is
# never allowed to tell a student they are INELIGIBLE. A guess may open a
# door; it may not close one.
#
# The original objection to deriving at all was that the mapping "varies by
# firm, region, and degree length". Checked against the shapes actually on
# the board, that is true of some programmes and not others, and the split is
# clean enough to act on:
#
#   Summer internship, year N  ->  graduates N+1.  A summer internship is the
#     penultimate-year placement everywhere this product covers: a US rising
#     senior and a UK second-year of three both graduate the following year.
#     Degree length changes which year of study you are in, not the distance
#     from a penultimate-year summer to graduation.
#
#   Graduate programme / new analyst starting N  ->  graduates N.  Full-time
#     campus intakes hire out of the finishing class.
#
# Everything else is refused, because for those the objection stands:
#
#   Off-cycle and seasonal internships are 3-6 month placements taken on a
#     gap year, a sandwich-course placement year, or after graduating. Lazard
#     and Goldman post them for every quarter; there is no single answer.
#   Insight days and spring weeks target first-years, and a first-year in
#     year N graduates N+2 or N+3 depending on the degree — the exact
#     variance the objection named.
#   An internship whose title names no season could be any of the above:
#     "Fall 2026", "November 2026 Intake", "Winter 2027 Co-op".
#   Sophomore and freshman programmes are two or three years out, not one.
_SUMMER = re.compile(r"\bsummer\b", re.IGNORECASE)
_OFF_CYCLE = re.compile(r"off[-\s]?cycle|seasonal|\bco[-\s]?op\b", re.IGNORECASE)
# Early-year programmes, whose distance to graduation is the thing that
# varies. "Penultimate" is deliberately NOT here: that is the ordinary summer
# intern this derivation is about.
_EARLY_YEAR = re.compile(
    r"\bsophomore\b|\bfreshman\b|first[-\s]year|1st[-\s]year|"
    r"\bspring week\b|\binsight\b|\bdiscovery\b|\bopen day\b",
    re.IGNORECASE)
# Shapes with no single graduating class behind them at all, each caught in
# the first audit of what this function accepted:
#   - Apprenticeships and French alternance contracts run one to three years
#     with the person enrolled throughout, so the intake year is nowhere near
#     a graduation year (19 live rows, all "Apprentice hiring for 2026-2027").
#   - A talent community is a pipeline pool, not a programme; nobody
#     graduates out of one.
#   - A PhD summer internship is not a penultimate-year placement. Doctoral
#     candidates intern mid-programme and finish on a four-to-six-year clock,
#     so the +1 convention — which is an undergraduate and master's one —
#     simply does not apply.
_NO_CLASS = re.compile(
    r"apprentic|apprenti|alternan|talent community|talent pool|"
    r"register your interest|expression of interest|\bph\.?\s?d\b",
    re.IGNORECASE)

DERIVED_SUMMER = ("A summer {cohort} internship is the penultimate-year "
                  "placement, so its interns graduate in {year}. The posting "
                  "does not say this; Coverage inferred it.")
DERIVED_GRAD = ("A graduate programme starting in {cohort} hires from that "
                "year's finishing class. The posting does not say this; "
                "Coverage inferred it.")


def derive_class_year(bucket: str, title: str, cohort: str) -> tuple[str, str]:
    """The graduation year a programme's SHAPE implies, with the sentence
    that justifies it. `("", "")` whenever the shape does not imply one.

    Never consulted where the posting states a year itself — the caller
    checks `class_year` and the extracted graduation window first. See the
    block comment above for why only two shapes qualify.
    """
    if not cohort or not cohort.isdigit():
        return "", ""
    text = title or ""
    # Any of these and the convention has more than one answer.
    if (_OFF_CYCLE.search(text) or _EARLY_YEAR.search(text)
            or _NO_CLASS.search(text)):
        return "", ""
    if bucket == INSIGHT:
        return "", ""
    year = int(cohort)
    if bucket == INTERNSHIP:
        if not _SUMMER.search(text):
            return "", ""
        return str(year + 1), DERIVED_SUMMER.format(cohort=cohort, year=year + 1)
    if bucket == ENTRY_LEVEL:
        return cohort, DERIVED_GRAD.format(cohort=cohort)
    return "", ""


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


#: A title that names a GRADUATE programme in its own words, in the three
#: spellings the live catalog carries. `graduate` is the English one;
#: 管培生 / 管理培训生 are the Chinese "management trainee", which is a
#: full-time graduate hire everywhere it appears on these boards (the same
#: two strings `_ENTRY` already treats that way).
_TITLE_GRADUATE = _rx(r"\bgraduates?\b", "管培生", "管理培训生")


def bucket_from_contract(contract_type: str | None, title: str = "") -> str:
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

    THE TITLE BREAKS THE TIE THE FILING CANNOT. A provider's contract-type
    vocabulary is coarser than its own titles: Crédit Agricole files
    "Graduate Trainee in CIB Coverage 企业金融部管培生-北京" — a
    management-trainee programme, i.e. a full-time graduate hire — under the
    single value "Internship/Trainee", because that ONE value covers both
    kinds and the firm has nowhere else to put it. Reading "trainee" and
    stopping there filed the row as an internship, which is the wrong bucket,
    the wrong facet count and the wrong cycle for the student reading it.

    So where the filing is one of the ambiguous ones, the title is asked
    whether it says "graduate" in its own words, and that answer wins. The
    guard is the ordering: an EXPLICIT internship title outranks it, because
    "Intern - EY-Parthenon - M&A Lead Advisory (graduate)" (live, EY) uses
    the word to mean the intern is a graduate student, not that the role is
    a graduate hire. Same precedence the title rules already use — step 2
    before step 4 in `classify_role` — stated once more here so the two
    paths cannot disagree about one row.

    `title` defaults to "" so a caller that has only the filing (and every
    existing test) behaves exactly as before.
    """
    t = (contract_type or "").lower()
    if not t or not any(k in t for k in _CAMPUS_CONTRACT_KEYS):
        return ""
    if "graduate" in t:
        return "entry_level"
    if _TITLE_GRADUATE.search(title or "") and not _INTERNSHIP.search(title or ""):
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


def campus_hint_pairs(board_entries) -> frozenset[tuple[str, str]]:
    """Collapse a `(firm slug, board)` catalog into the `(slug, provider)`
    pairs it is safe to treat as campus-scoped once a row's specific board is
    forgotten.

    A live ingest calls `board_is_campus` per board and never needs this: the
    scraper is iterating one board at a time, so the hint it passes to
    `classify_role` is always that exact board's own verdict. `reclassify`
    (and any other pass that only has an already-stored row) is different —
    the row remembers `source` (a provider name: "workday", "greenhouse", the
    connector, one per firm‑ish) but not which of a firm's several boards on
    that provider produced it, so it can only ask "is THIS (slug, provider)
    pair campus-scoped?"

    Two firms in the live catalog answer that question differently depending
    on which board you ask: Solomon Partners runs both a Greenhouse
    "studentsgraduates" board and a Greenhouse "professionals" board — the
    literal opposite of campus — and Citi runs both a Workday
    "Citi_Early_Careers_Events_Site" board and a second Workday site with no
    campus token at all. A naive "does ANY of the firm's boards on this
    provider look like campus" test (an `any()` over the per-board verdicts)
    answers "yes" for both pairs, and that "yes" then applies to every row
    ever stored with that (slug, provider) — including the professionals
    board's own rows. A neutral title with no senior/experienced word
    ("Investment Banking Associate - Technology") is exactly what the
    campus-hint fallback exists to promote, so a lateral hire posted to
    Solomon Partners' professionals board would silently promote to
    entry_level on the next backfill.

    `all()` instead of `any()` is the fix: a pair counts only when every
    catalog board sharing it agrees. Same "silence over a guess" contract as
    the rest of this module — an ambiguous (slug, provider) stays unhinted,
    which only means its neutral titles stay in `other` (rule 7), never a
    false promotion. Boards unique to their pair are unaffected either way,
    since `all()` of one verdict is that verdict.
    """
    verdicts: dict[tuple[str, str], list[bool]] = {}
    for slug, board in board_entries:
        verdicts.setdefault((slug, board.provider), []).append(board_is_campus(board))
    return frozenset(pair for pair, vs in verdicts.items() if all(vs))


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
    "without the need for visa sponsorship",
    "not eligible for sponsorship",
    # Added from a live-data sweep of the 946 not-stated rows whose text
    # mentions sponsor/visa/work authorisation (see
    # docs/founder-decisions-2026-08-20.md, Decision 3): declarative
    # employer statements the phrase lists above didn't catch because the
    # exact wording differs from the handful of set phrases already
    # covered. Each is a literal quote from a real posting, not a
    # generalised pattern, to keep the same "wrong is worse than unknown"
    # discipline as the rest of this list.
    "not offering sponsorship", "not offering visa sponsorship",
    "sponsorship not offered",
    "will not engage in immigration sponsorship",
    "without the need for current or future sponsorship",
    "without the need for employer sponsorship",
    "will not require sponsorship",
    "sponsorship for work authorization not required",
    "do not offer any type of employment-based immigration sponsorship",
    "unable to consider candidates that will require visa sponsorship",
    "unable to consider candidates who will require visa sponsorship",
)
_SPONSOR_YES = (
    "visa sponsorship available", "sponsorship is available",
    "will sponsor", "provides visa sponsorship", "offers visa sponsorship",
    "provide visa sponsorship", "offer visa sponsorship",
    "h-1b sponsorship", "h1b sponsorship",
    # Same live-data sweep as _SPONSOR_NO above.
    "sponsorship where necessary", "supportive of immigration sponsorship",
    "supportive of visa sponsorship", "supportive of us immigration sponsorship",
)

# The Workday structured field ("Available for Work Visa Sponsorship? Yes")
# and its label variants, read as a labelled answer rather than free prose.
# 636 open PwC rows carry this exact field (487 No / 46 Yes / 103 blank) and
# it was never read — classify.extract_sponsorship only ever looked at
# prose. Requiring the label to be followed immediately (allowing only
# whitespace) by ":" or "?" and then the bare word "yes"/"no" is what keeps
# this safe: a blank field ("...Sponsorship? Government Clearance...") or an
# unrelated sentence ("...sponsorship. No refunds...", a period, not ":"/"?")
# never matches, so this can't manufacture an answer the field didn't give.
# Checked against all 1,513 open PwC rows: 46 Yes / 488 No / 103 blank (no
# match) / 876 rows with no such field at all — matching the live counts in
# docs/founder-decisions-2026-08-20.md exactly.
_SPONSOR_STRUCTURED_FIELD = re.compile(
    r"(?:available for work visa sponsorship|work visa sponsorship|"
    r"visa sponsorship available|visa sponsorship|will sponsor|"
    r"sponsorship available|sponsorship)"
    r"\s*[:?]\s*(yes|no)\b",
    re.IGNORECASE,
)

# "now or in the future" is handled separately from the plain _SPONSOR_NO
# substrings above because the same six words open two completely different
# things: the standard US declarative NO-sponsorship sentence ("must be
# authorized to work ... without the need for sponsorship now or in the
# future.") AND the standard US/HK/SG visa-status APPLICATION-FORM QUESTION
# every candidate answers regardless of the firm's actual policy ("Will you
# now or in the future require sponsorship...? * Select..."). A bare
# substring match cannot tell these apart, and previously always returned
# "no" -- misreading a per-candidate screening question as an employer
# policy statement on 59 open rows across 7 firms (Point72, DRW, Five
# Rings, IMC Trading, Qube Research & Technologies, Bridgewater, Solomon
# Partners), each producing a false "No sponsorship" fact-wall chip and a
# false blocking eligibility verdict.
#
# The two are told apart by what follows the phrase: a declarative sentence
# runs on to a period with no "?" in the next stretch; a form question is
# bounded by its own "?" within a short span (the scraped "* Select..."
# dropdown marker sits right after it). Checked live against all 101 open
# rows whose only _SPONSOR_NO-family trigger was this phrase: every row
# with a "?" within 200 chars after the phrase was the scraped dropdown
# question (59 rows, zero exceptions); every row without one was a genuine
# policy statement (42 rows across Wells Fargo, SIG, Morgan Stanley,
# Houlihan Lokey, MUFG, William Blair, Citi -- zero false positives).
_SPONSOR_FUTURE = re.compile(r"now or in the future", re.IGNORECASE)
_SPONSOR_FUTURE_WINDOW = 200


def _future_phrase_is_statement(text: str) -> bool:
    """True if at least one "now or in the future" occurrence in `text` is a
    declarative employer statement, rather than every occurrence being the
    scraped visa-status application-form question."""
    for m in _SPONSOR_FUTURE.finditer(text):
        if "?" not in text[m.end(): m.end() + _SPONSOR_FUTURE_WINDOW]:
            return True
    return False


def extract_sponsorship(text: str | None) -> str:
    """"yes" / "no" / "unknown" from posting prose."""
    t = (text or "").lower()
    if not t:
        return "unknown"
    # Structured field first: it's a direct labelled answer, not prose to
    # interpret, so it outranks the phrase lists below.
    structured = _SPONSOR_STRUCTURED_FIELD.search(text or "")
    if structured:
        return "yes" if structured.group(1).lower() == "yes" else "no"
    if any(k in t for k in _SPONSOR_NO):
        return "no"
    if "now or in the future" in t and _future_phrase_is_statement(text or ""):
        return "no"
    if any(k in t for k in _SPONSOR_YES):
        return "yes"
    return "unknown"


_MONTH_NAMES = ("january", "february", "march", "april", "may", "june",
                "july", "august", "september", "october", "november",
                "december")
_MONTHS = {m: i + 1 for i, m in enumerate(_MONTH_NAMES)}
# Three-letter abbreviations, plus the four-letter "Sept". Boards write dates
# both ways and the full-name-only list read exactly half of them: HSBC's
# pages state "Closing Date: Fri Oct 30, 2026" and every one of them fell
# through, on 16 live campus rows. Worse than a miss — several of those rows
# were still carrying an older, wrong date, so the board showed "Deadline
# passed" on roles that stay open for another eleven weeks.
_MONTHS.update({m[:3]: i + 1 for i, m in enumerate(_MONTH_NAMES)})
_MONTHS["sept"] = 9
#: Longest-first so "january" wins over "jan"; the optional "." covers
#: "Oct." Anchored with \b so it cannot fire inside a longer word.
_MONTH_RX = (r"\b(january|jan|february|feb|march|mar|april|apr|may|june|jun|"
             r"july|jul|august|aug|september|sept|sep|october|oct|"
             r"november|nov|december|dec)\.?")
# A deadline KEYWORD, then a date within the next ~80 characters. The keyword
# gate is what keeps this conservative: a bare date in a posting is usually a
# start date or a posted date, and treating those as deadlines would put
# false countdowns on the feed.
_DEADLINE_KEY = re.compile(
    # "applications WILL close" / "applications MUST be received": an
    # auxiliary between the noun and the verb is ordinary English and used to
    # defeat the whole gate — SIG's "Applications will close November 16,
    # 2026" was read as no deadline at all.
    r"(?:apply by|application deadline|applications?\s+(?:will\s+|must\s+|"
    r"shall\s+)?(?:close[sd]?|due|be\s+(?:received|submitted)|must be "
    r"(?:received|submitted))|closing date|deadline|"
    # "Application window is open until 30th August 2026" (William Blair) —
    # a fully-specified closing date stated as an open-until window rather
    # than a "close(s)"/"deadline" phrasing. Gated on "window" so a bare
    # "applications are open" (no window, no date sense) never fires.
    r"(?:application\s+)?window\s+(?:is|are|remains?)\s+open\s+(?:until|through))",
    re.IGNORECASE)
_DATE_ISO = re.compile(r"(20\d{2})-(\d{2})-(\d{2})")
_DATE_MDY = re.compile(
    _MONTH_RX + r"\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(20\d{2})", re.IGNORECASE)
_DATE_DMY = re.compile(
    r"(\d{1,2})(?:st|nd|rd|th)?\s+" + _MONTH_RX + r",?\s+(20\d{2})",
    re.IGNORECASE)
# A slash date, which boards write in both conventions and never label.
# BMO posts "Application Deadline: 08/27/2026"; a UK board writing the same
# day says 27/08/2026. Read ONLY when the two numbers cannot swap roles —
# one of them above 12 forces which is the month. "9/4/26" is September 4 to
# an American board and 9 April to a British one, a five-month difference
# with nothing in the text to settle it, so it is refused rather than
# guessed. A 4-digit year is required here for the same reason it is
# everywhere else in this function.
_DATE_SLASH = re.compile(r"\b(\d{1,2})/(\d{1,2})/(20\d{2})\b")


def extract_deadline_from_text(text: str | None) -> str | None:
    """An ISO date string, only when a deadline keyword sits within 80
    characters before a fully-specified date (a 4-digit year is required —
    "closes January 5" with no year is a guess, and guesses don't ship)."""
    t = text or ""
    if not t:
        return None
    for key_match in _DEADLINE_KEY.finditer(t):
        # The window stops at the first line break. These pages are label /
        # value tables ("Closing Date: Fri Oct 30, 2026"), so a date on the
        # NEXT line belongs to a different field — and letting the window
        # reach it is how a truncated "Closing Date: Fri Oct 30,…" borrowed
        # the date sitting two lines below it. A label's value is on the
        # label's line.
        window = t[key_match.end():key_match.end() + 80].split("\n")[0]
        iso = _DATE_ISO.search(window)
        if iso:
            y, m, d = int(iso.group(1)), int(iso.group(2)), int(iso.group(3))
        else:
            mdy = _DATE_MDY.search(window)
            dmy = _DATE_DMY.search(window)
            sl = _DATE_SLASH.search(window)
            if mdy:
                y, m, d = int(mdy.group(3)), _MONTHS[mdy.group(1).lower()], int(mdy.group(2))
            elif dmy:
                y, m, d = int(dmy.group(3)), _MONTHS[dmy.group(2).lower()], int(dmy.group(1))
            elif sl:
                a, b, y = int(sl.group(1)), int(sl.group(2)), int(sl.group(3))
                if a > 12 and b <= 12:
                    d, m = a, b          # 27/08/2026 — day first
                elif b > 12 and a <= 12:
                    m, d = a, b          # 08/27/2026 — month first
                else:
                    continue             # both plausible as a month: no answer
            else:
                continue
        try:
            return date(y, m, d).isoformat()
        except ValueError:
            continue  # "February 30" — keep scanning, don't invent
    return None


#: Keys in `raw` that COVERAGE wrote, not the provider — our fetch
#: bookkeeping and our own derived facts. Excluded from `posting_text` so the
#: extractors only ever read the posting's own words.
#:
#: This was not hypothetical. `detail_fetched` holds an ISO timestamp of when
#: we read the page, and flattening it into the searchable text put
#: "2026-08-08T16:..." two lines below HSBC's "Closing Date" label — close
#: enough for the deadline scanner's window to reach it. Seven live HSBC roles
#: were dated with OUR FETCH TIME and rendered "Deadline passed", while their
#: pages said "Closing Date: Fri Oct 30, 2026".
#:
#: `detail_text` is deliberately NOT here: it is the posting's own body and
#: the single richest thing the extractors read. `facts` is, because a fact's
#: stored evidence phrase is our copy of text already present in
#: `detail_text` — reading it again only lets a derived value be re-derived
#: from itself.
_OURS_NOT_THE_POSTINGS = ("detail_fetched", "facts", "facts_at",
                          "detail_source")


def posting_text(title: str | None, raw: dict | None) -> str:
    """Every string in the posting, flattened for the extractors: the title
    plus all string values in the provider's raw payload, recursively.
    Capped, because a payload is untrusted input and one pathological
    posting must not make extraction quadratic.

    Coverage's own keys are skipped — see `_OURS_NOT_THE_POSTINGS`."""
    parts: list[str] = [(title or "")]

    def walk(node, depth=0):
        if depth > 6 or sum(len(p) for p in parts) > 40_000:
            return
        if isinstance(node, str):
            parts.append(node)
        elif isinstance(node, dict):
            for k, v in node.items():
                # Only at the top level: a provider is free to have its own
                # nested "facts" key and that one IS the posting's.
                if depth == 0 and k in _OURS_NOT_THE_POSTINGS:
                    continue
                walk(v, depth + 1)
        elif isinstance(node, (list, tuple)):
            for v in node:
                walk(v, depth + 1)

    walk(raw or {})
    return "\n".join(parts)[:40_000]
