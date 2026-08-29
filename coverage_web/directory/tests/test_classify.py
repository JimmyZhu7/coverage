"""Table-driven tests for directory.classify — pure functions, no DB.

The example titles are real rows from live scrapes of the seeded boards
(TPG, Blackstone, Wellington, ...) plus the canonical campus-recruiting
shapes, so a rule regression shows up against data the calendar actually
faces, not toy strings.
"""

import pytest

from directory.classify import (
    normalize_region,
    ENTRY_LEVEL,
    INSIGHT,
    INTERNSHIP,
    OTHER,
    board_is_campus,
    campus_hint_pairs,
    classify_role,
    clean_title,
    extract_class_year,
    extract_cohort,
    extract_sponsorship,
)


CASES = [
    # ---- insight events ----
    ("Spring Insight Week 2027", INSIGHT),
    ("Insight Day — Investment Banking", INSIGHT),
    ("Early Insights Program", INSIGHT),
    ("Spring Week 2027 - London", INSIGHT),
    ("Pre-Internship Programme", INSIGHT),          # must beat the intern rule
    ("Discovery Day 2026", INSIGHT),
    ("First-Year Insight Series", INSIGHT),
    ("2027 Insight into Internal Audit Opportunities", INSIGHT),   # real tal.net row
    ("2027 Recruitment Process Demystified (Glasgow) - Virtual Event", INSIGHT),  # real row
    ("2027 Operations Industrial Placement Q&A (Frankfurt)", INSIGHT),  # event ABOUT a placement
    ("APAC Virtual Recruitment Event | Introduction to Global Markets", INSIGHT),
    # ---- affinity events with no internship attached ----
    ("Women in Banking  Programme - London 2026", INSIGHT),   # real row (double space)
    ("Sophomore Discovery Event", INSIGHT),
    # ---- internships ----
    ("Climate Impact, Intern | Fall Research Assistant", INTERNSHIP),  # real row
    ("2026 Summer Analyst Program - Investment Banking", INTERNSHIP),
    ("Summer Associate (MBA) 2026", INTERNSHIP),
    ("Sophomore Summer Analyst", INTERNSHIP),        # internship beats affinity
    ("Off-Cycle Internship, Hong Kong", INTERNSHIP),
    ("Off Cycle Analyst Intern", INTERNSHIP),
    ("Software Engineering Co-op", INTERNSHIP),
    ("Industrial Placement Student", INTERNSHIP),
    ("2027 Winter Analyst", INTERNSHIP),
    ("Central HSBC Student Work Placement Hong", INTERNSHIP),      # real HSBC sitemap row
    ("Global Markets Summer Programme", INTERNSHIP),
    ("Praktikum im Wealth Management, Hamburg", INTERNSHIP),   # German internship (real UBS row)
    ("Werkstudent Corporate Finance", INTERNSHIP),
    ("Intern - Talent Acquisition", INTERNSHIP),     # an HR internship is an internship
    # ---- entry level ----
    ("Graduate Analyst Programme 2026", ENTRY_LEVEL),
    ("Full-Time Analyst - 2026", ENTRY_LEVEL),
    ("Investment Banking Analyst (Campus 2026)", ENTRY_LEVEL),
    ("2026 Analyst Development Program", ENTRY_LEVEL),
    ("New Grad Software Engineer", ENTRY_LEVEL),
    ("Graduate Trainee, Markets", ENTRY_LEVEL),
    ("Rotational Program Associate", ENTRY_LEVEL),
    ("Investment Banking — New Analyst", ENTRY_LEVEL),          # GS full-time new-grad term
    ("Wealth Management — WMP Analyst", ENTRY_LEVEL),           # GS Wealth Mgmt Program
    ("Class of 2027 Investment Analyst", ENTRY_LEVEL),           # real Houlihan Lokey row
    # ---- other: experienced / HR / no campus signal ----
    ("Internal Audit Manager", OTHER),               # "intern" must not fire
    ("International Equities, Vice President", OTHER),
    ("Talent Acquisition, Senior Associate", OTHER),  # real row
    ("Real Estate Communications, Vice President", OTHER),  # real row
    ("Senior DevOps Engineer", OTHER),               # real row
    ("Tax Accountant", OTHER),                       # real row
    ("Control Room Analyst", OTHER),                 # neutral title, non-campus board
    ("Research Analyst - China Coverage", OTHER),    # real row
    ("Graduate Recruitment Manager", OTHER),         # veto beats "graduate"
    ("Head of Diversity", OTHER),                    # veto beats affinity
    ("Campus Recruiter", OTHER),                     # veto beats "campus"
    ("Market Insights Analyst", OTHER),              # unqualified "insights" is a data job
    ("Fund Finance (TRECO), Associate ", OTHER),     # real row
    ("", OTHER),
    ("Class of 2027 Managing Director", OTHER),      # senior veto beats "class of"
]


@pytest.mark.parametrize("title,expected", CASES)
def test_classify_role(title, expected):
    assert classify_role(title) == expected


def test_campus_hint_promotes_neutral_junior_titles():
    # On a campus-scoped board, a plain Analyst posting is an entry-level hire…
    assert classify_role("Investment Banking Analyst", campus_hint=True) == ENTRY_LEVEL
    assert classify_role("Restructuring Associate", campus_hint=True) == ENTRY_LEVEL
    # …but the senior veto still wins, and non-junior titles stay other.
    assert classify_role("Vice President, Fund Finance", campus_hint=True) == OTHER
    assert classify_role("Tax Accountant", campus_hint=True) == OTHER
    # And without the hint, neutral titles are never promoted.
    assert classify_role("Investment Banking Analyst") == OTHER


@pytest.mark.parametrize(
    "title,expected",
    [
        ("2027 Summer Analyst Program", "2027"),
        ("Women in Banking Programme - London 2026", "2026"),
        ("Insight Week", ""),
        ("Founded in 1999", ""),   # out of the plausible cohort range
        ("", ""),
    ],
)
def test_extract_cohort(title, expected):
    assert extract_cohort(title) == expected


# extract_class_year exists to stop a programme year masquerading as a
# graduation year, so the negative cases below are the load-bearing half of
# this table: if any of them ever returns a year, the Year filter and the card
# chip start telling ~4,000 roles' worth of students the wrong eligibility.
CLASS_YEAR_CASES = [
    # ---- stated outright: the only thing that counts ----
    ("Class of 2027 Investment Analyst", "2027"),                      # real row
    ("Job Posting Title Financial Analyst (Class of 2027) - Financial "
     "Restructuring - Minneapolis", "2027"),                           # real row
    ("Class 2028 Analyst Programme", "2028"),                          # no "of"
    ("Summer Analyst [Class of 2029]", "2029"),
    ("CLASS OF 2026 - Global Markets", "2026"),                        # case-insensitive
    # A programme year AND a stated class year, and they differ by one. This is
    # a real board row and the single clearest proof the two fields can't be
    # collapsed: cohort is 2027, class year is 2028.
    ("JPN, 海外大, Autumn＆BCF選考, 2027 Summer Intern_Bachelor or Master "
     "with NO full-time work experience (Class of 2028)", "2028"),
    # ---- NOT a class year: a programme/intake year ----
    ("2027 Summer Analyst Program", ""),                # the confusion this field prevents
    ("2026 Off-Cycle Internship", ""),
    ("2027 Summer Internship – Account Analyst, Tokyo", ""),           # real row
    ("Graduate Analyst Programme 2026", ""),
    ("Spring Insight Week 2027", ""),
    # ---- "class" as a word with no year attached, or attached to the wrong
    # thing. "Classic" is a real coverage group name on the live board. ----
    ("Investment Banking, Classic — Summer Analyst", ""),              # real row
    ("Corporate Advisory, Classic Group — Summer Analyst 2027", ""),   # real row + year
    ("World-class 2027 Analyst Programme", ""),                        # hyphen guard
    ("First-Class Honours 2026 Graduate Scheme", ""),
    ("Asset Class Strategy 2027 Internship", ""),   # "Class" then a word, not a year
    ("Class of 1999 Reunion Analyst", ""),          # outside the plausible window
    ("", ""),
    # Defensive: callers pass model fields that are declared non-null but the
    # connectors' dataclass allows None.
    (None, ""),
]


@pytest.mark.parametrize("title,expected", CLASS_YEAR_CASES)
def test_extract_class_year(title, expected):
    assert extract_class_year(title) == expected


def test_class_year_and_cohort_are_independent():
    """The two extractors must never be able to stand in for one another."""
    title = "2027 Summer Intern (Class of 2028)"
    assert extract_cohort(title) == "2027"       # programme year
    assert extract_class_year(title) == "2028"   # stated graduation year
    # And a plain programme title yields a cohort but no class year at all.
    assert extract_cohort("2027 Summer Analyst Program") == "2027"
    assert extract_class_year("2027 Summer Analyst Program") == ""


class _FakeBoard:
    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)


@pytest.mark.parametrize(
    "board,expected",
    [
        (_FakeBoard(site="Blackstone_Campus_Careers"), True),
        (_FakeBoard(site="Students"), True),
        (_FakeBoard(site="RBCEARLYTALENT1"), True),
        (_FakeBoard(token="solomonpartnersstudentsgraduates"), True),
        (_FakeBoard(site="External"), False),
        (_FakeBoard(org="palantir"), False),
        (_FakeBoard(site="Experienced-Hires"), False),
        (_FakeBoard(board_url="https://x.tal.net/.../jobboard/vacancy/1/adv/"), False),
    ],
)
def test_board_is_campus(board, expected):
    assert board_is_campus(board) is expected


# ---------------------------------------------------------------------------
# campus_hint_pairs — reclassify's board-forgetting reconstruction of the
# per-board campus hint. A live ingest always knows its own board and never
# needs this; reclassify only has a stored row's `source` (a provider name),
# so it can only ask about the (slug, provider) pair. Two firms in the live
# catalog run one campus board and one non-campus board on the SAME
# provider, and `all()` (not `any()`) is what keeps that ambiguity from
# resolving to a false "yes" — see the function's own docstring.
# ---------------------------------------------------------------------------


def test_campus_hint_pairs_requires_every_board_on_the_pair_to_agree():
    # Solomon Partners' real catalog shape: a campus Greenhouse board and a
    # "professionals" (experienced-hire) Greenhouse board share one
    # provider. The ambiguous pair must NOT count as campus-scoped.
    boards = [
        ("solomonpartners", _FakeBoard(provider="greenhouse",
                                        token="solomonpartnersstudentsgraduates")),
        ("solomonpartners", _FakeBoard(provider="greenhouse",
                                        token="solomonpartnersprofessionals")),
    ]
    assert campus_hint_pairs(boards) == frozenset()


def test_campus_hint_pairs_keeps_a_lone_campus_board():
    # A firm with exactly one board on a provider, campus-scoped, is
    # unambiguous and must still count — `all()` of one true is true.
    boards = [
        ("blackstone", _FakeBoard(provider="workday",
                                   site="Blackstone_Campus_Careers")),
    ]
    assert campus_hint_pairs(boards) == frozenset({("blackstone", "workday")})


def test_campus_hint_pairs_excludes_a_lone_non_campus_board():
    boards = [("acme", _FakeBoard(provider="greenhouse", token="acme-experienced"))]
    assert campus_hint_pairs(boards) == frozenset()


def test_campus_hint_pairs_keeps_other_pairs_independent():
    # A firm/provider collision must not affect an unrelated pair sharing
    # neither the slug nor the provider.
    boards = [
        ("citi", _FakeBoard(provider="workday", site="Citi_Early_Careers_Events_Site")),
        ("citi", _FakeBoard(provider="workday", site="2")),
        ("blackstone", _FakeBoard(provider="workday", site="Blackstone_Campus_Careers")),
    ]
    assert campus_hint_pairs(boards) == frozenset({("blackstone", "workday")})


# ---------------------------------------------------------------------------
# The "other markets" tier — stated-but-untracked locations file under
# "other" instead of vanishing into the same blank as genuinely silent rows.
# 230 live campus roles (Sydney, Bangalore, Seoul, São Paulo...) were
# indistinguishable from the 103 that never said where they are.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("location,expected", [
    ("Sydney", "other"),
    ("Bangalore, Velankani Tech Park", "other"),
    ("Seoul", "other"),
    ("Sao Paulo Edificio Pedro Mariz", "other"),
    ("Kuala Lumpur", "other"),
    ("Dubai United Arab Emirates", "other"),
    ("Casablanca, Morocco", "other"),
    ("Toronto, Canada", "other"),
    # Tracked markets must win first — the guards that keep cities at home:
    ("Melbourne, FL", "us"),          # Florida, not Victoria
    ("Indianapolis", "us"),           # contains the substring "india"
    ("Albuquerque, New Mexico", "us"),  # contains "mexico"
    ("Perth, Scotland", "eu"),        # Scotland, not Western Australia
    ("China", "cn"),                  # bare country as the whole string
    # Silence is still silence:
    ("", ""),
    # "Remote" is NOT silence. It used to answer "" here, next to the empty
    # string, which is what made a posting that told us it has no fixed place
    # indistinguishable from a posting we failed to read. See the placeless
    # tier at the foot of this file.
    ("Remote", "global"),
])
def test_other_markets_file_under_other_and_tracked_markets_win_first(location, expected):
    assert normalize_region(location) == expected


# ---------------------------------------------------------------------------
# 2026-08-08 second-pass census keys — every positive case below is a real
# location string that sat at region="" on live data, and every guard is an
# ambiguity the key list deliberately refuses: bare "San Jose" (California or
# Costa Rica), "Santiago" (Chile or Spain), "Bristol" (England or
# Connecticut), "Lima" (Peru or Ohio), and "Chester", which as a substring
# also sits inside Rochester and Manchester. Those rows resolve — if at all —
# through the provider's own structured fields, never through a guess.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("location,expected", [
    # ISO-3166 alpha-3 codes as SocGen and PwC boards render them.
    (", HKG", "hk"),
    (", JPN", "jp"),
    (", ROU", "eu"),                      # Romania
    ("Wellington - NZL", "other"),        # the " nzl" suffix, not the city
    ("Christchurch - NZL", "other"),
    ("Rouen, France", "eu"),              # ", rou" inside ", rouen": same eu
    # European cities the first census missed.
    ("Vilnius", "eu"),
    ("Warszawa", "eu"),                   # Polish spelling of Warsaw
    ("Skopje", "eu"),
    ("Sheffield, GB, S1 4NB", "eu"),      # HSBC's label shape
    ("Commercial StartX - Grande Lisboa", "eu"),
    # Bare US cities Morgan Stanley's tal.net rows state.
    ("South Jordan", "us"),
    ("Alpharetta", "us"),
    # The ", VA" state and ", US" country tails SIG's iCIMS filings use.
    ("Richmond, VA, US", "us"),
    ("Philadelphia, PA, USA", "us"),
    # Untracked markets from the same census.
    ("Quito", "other"),
    ("San Jose, Costa Rica", "other"),    # the country disambiguates
    ("Almaty, Kazakhstan", "other"),
    # ---- the guards: ambiguous alone, so silence ----
    ("San Jose", ""),
    ("Santiago", ""),
    ("Bristol", ""),
    ("Lima", ""),
    ("Chester", ""),
    ("Rochester, NY", "us"),              # ", ny", never a "chester" key
    # A statement, and the tier below reads it as one — but only because it
    # is the WHOLE field. "Global Markets Recruitment Event" is a division.
    ("Global", "global"),
    # Boundary guards on the new suffixes: a word continuing past the code
    # never matches.
    # Almaty became a real key on 2026-08-09, so this row now resolves to
    # Kazakhstan for an honest reason. The ", us" boundary it was written to
    # guard is asserted on the line below, on a string with no keyed city in
    # it — a guard case has to be able to fail.
    ("Almaty, Ust-Kamenogorsk", "other"),
    ("Ust-Kamenogorsk", ""),              # ", us" must not fire inside "Ust"
    ("Valletta, Valencia District", ""),  # ", va" must not fire inside "Valencia"
])
def test_second_pass_census_keys_and_guards(location, expected):
    assert normalize_region(location) == expected


# ---------------------------------------------------------------------------
# region_from_fields — the provider's own location structures, already stored
# in `raw`. The agreement gate is the contract: every stated location must
# resolve to ONE market or the answer is silence.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("raw,expected", [
    # Goldman's list-API shape: the country field is the only honest way to
    # place a bare "Birmingham" or "Lima".
    ({"locations": [{"city": "Birmingham", "state": "West Midlands, England",
                     "country": "United Kingdom", "primary": True}]}, "eu"),
    ({"locations": [{"city": None, "state": "Lima", "country": "Peru",
                     "primary": True}]}, "other"),
    ({"locations": [{"city": "Albany", "state": "New York",
                     "country": "United States", "primary": True}]}, "us"),
    # McKinsey's parallel cities/countries arrays.
    ({"cities": ["San Jose"], "countries": ["Costa Rica"]}, "other"),
    ({"cities": ["Almaty", "Astana"],
      "countries": ["Kazakhstan", "Kazakhstan"]}, "other"),
    # Greenhouse's location object. "Global" states no market, and says so on
    # purpose — KKR's and EQT's talent communities both file this way.
    ({"location": {"name": "Bristol, City Of Bristol, England, United Kingdom"}}, "eu"),
    ({"location": {"name": "Global"}}, "global"),
    # enrich_postings' detail_location — schema.org jobLocation reads.
    ({"detail_location": "Hong Kong, HK"}, "hk"),
    ({"detail_location": "Bala Cynwyd (Philadelphia Area), PA, US"}, "us"),
    # Two stated markets is not one answer — the agreement gate holds.
    ({"detail_location": "London, United Kingdom; New York, NY"}, ""),
    ({"locations": [
        {"city": "London", "state": "", "country": "United Kingdom"},
        {"city": "Tokyo", "state": "", "country": "Japan"}]}, ""),
    # Silence in, silence out.
    ({}, ""),
    (None, ""),
])
def test_region_from_fields(raw, expected):
    from directory.classify import region_from_fields
    assert region_from_fields(raw) == expected


# ---------------------------------------------------------------------------
# region_from_title_segments — the leading routing segments clean_title
# strips ("APAC | Singapore | …") are the only place some tal.net rows ever
# state a location. Read before they are lost, never from the title body.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("title,expected", [
    ("APAC | Singapore | Global Markets Recruitment Event", "sg"),   # real row
    ("EMEA | London | Quantitative Strategies & Data Group | "
     "Recruitment Event", "eu"),                                     # real row
    ("Paris | Bank of America | Insight Day - Step into Finance", "eu"),
    # "EMEA" alone files as Europe — the same answer a location field
    # saying "EMEA" already gets from normalize_region.
    ("EMEA | Chief Financial Officer | Virtual Insight Event", "eu"),
    # A leading segment that is a sentence, not a routing tag, is not read…
    ("APAC Virtual Recruitment Event | A Career with Bank of America "
     "in Southeast Asia", ""),
    # …and the title BODY is never read: a market word there is a desk.
    ("Equity Research, China Industrials — Summer Analyst", ""),
    ("Global Markets Summer Programme", ""),
    ("", ""),
    (None, ""),
])
def test_region_from_title_segments(title, expected):
    from directory.classify import region_from_title_segments
    assert region_from_title_segments(title) == expected


# ---------------------------------------------------------------------------
# The prose anchors added by the same census: Wells Fargo's footnoted label
# ("Program Locations * :"), tal.net's colon-less Region…Location label table,
# and tal.net's venue field. A bare "location" in ordinary prose must still
# never anchor.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("text,expected", [
    ("Program Locations * : Charlotte, NC (CM, RAPA) *Locations subject "
     "to change", "us"),                                             # real WF row
    ("Region Europe, Middle East, Africa Event - Location United Kingdom "
     "Business Unit Internal Audit", "eu"),                          # real MS row
    # The abbreviation dots in "U.S." must not read as a sentence end.
    ("Program ID 14594 Region U.S. and Canada Location United States of "
     "America States New York City New York, NY", "us"),             # real BofA row
    ("Event address / venue details London City Centre - Venue details "
     "to be shared upon invitation.", "eu"),                         # real BofA row
    # A venue that names no place resolves to nothing.
    ("Event address / venue details To be confirmed on invite acceptance.", ""),
    # Everyday prose "location" never anchors on its own…
    ("a competitive salary (based on your location, experience, and "
     "skills) in London", ""),
    # …and a real sentence boundary between the labels still blocks.
    ("we serve every region. Location data shows Hong Kong usage grew", ""),
])
def test_region_from_prose_census_anchors(text, expected):
    from directory.classify import region_from_prose
    assert region_from_prose(text) == expected


# ---------------------------------------------------------------------------
# The placeless tier. A posting whose honest answer is "nowhere in particular"
# used to share the blank bucket with a posting we simply failed to read, so
# "Global"/"Anywhere"/"Multiple Locations" — all stated facts — looked like
# missing data. They file under "global" now.
#
# The negative half of this table is the important half. The first draft
# scanned for "global" as a substring and filed sixteen rows as placeless; the
# URLs of three of them said Singapore, Singapore and EMEA. In this industry
# the word names a division far more often than a place.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("text,expected", [
    # Stated placelessness.
    ("Global", "global"),                            # real KKR / EQT row
    ("Anywhere", "global"),                          # real ExodusPoint row
    ("Anywhere (subject to approval)", "global"),    # real Qube row
    ("Multiple Locations", "global"),                # real Tower row
    ("Virtual", "global"),                           # real Akuna row
    ("APAC Virtual Recruitment Event | Welcome to Bank of America",
     "global"),                                      # real BofA row
    # Division names, every one a real live row that this tier wrongly took.
    ("Global Markets Recruitment Event", ""),        # the posting says Singapore
    ("Global Technology Recruitment Event", ""),     # Singapore too
    ("Global Operations Summer Analyst Program - 2027", ""),
    ("Software Developer - Global Routing | Experienced Hire", ""),
    ("VP/ED/MD, Relationship Manager, Global Coverage Centre", ""),
    ("Central Global Investment Research Internship Hong", ""),
    # A virtual asset is a cryptocurrency, not a way of attending.
    ("AVP, Virtual Asset Trade Settlement, Operations", ""),
    ("Virtual Assets Analyst", ""),
    # A real market always wins: this tier runs last, after every other.
    ("Virtual - Sydney", "other"),
    ("Remote - Cayman Islands", "other"),
    ("Remote, New York", "us"),
    ("Florida Remote", "us"),                        # real KKR row
    ("Minnesota Remote", "us"),                      # real KKR row
    ("Remote / Home Office - Texas", "us"),          # real Neuberger row
    ("CPG - Remote Sales - US", "us"),               # real KKR row
    ("Hong Kong", "hk"),
])
def test_placeless_tier(text, expected):
    from directory.classify import normalize_region
    assert normalize_region(text) == expected


def test_placeless_is_a_facet_row_but_never_a_target_a_student_can_pick():
    """"Global / Virtual" is somewhere a role can BE, not somewhere a student
    can want to work. It belongs in REGION_ORDER, which drives the feed's
    region facet, and must stay out of TRACKED_REGIONS, which drives the
    Settings picker and the market count on the pricing page."""
    from directory.classify import REGION_LABELS, REGION_ORDER, TRACKED_REGIONS
    assert "global" in REGION_ORDER
    assert "global" not in TRACKED_REGIONS
    assert REGION_LABELS["global"] == "Global / Virtual"
    # Last in the facet: it is the residual, and it reads as one.
    assert REGION_ORDER[-1] == "global"


# ---------------------------------------------------------------------------
# The provider's original title as a cohort source. Goldman's board was the
# whole caseload: its jobTitle leads with the year and the connector keeps
# only the human tail, so all 142 live GS campus rows sat under "No Year
# Stated" while every one stated its year in the payload we already store.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("raw,expected", [
    ({"jobTitle": "2027 | APAC | Japan | Tokyo | Operations | New Analyst"},
     "2027"),
    ({"jobTitle": "2026 | EMEA | Paris | FICC & Equities (Sales & Trading) | "
                  "Seasonal/Off Cycle Internship, July - December"}, "2026"),
    # Other boards park the year in parentheses the display cleaner strips.
    ({"title": "Graduate Software Engineer (2027)"}, "2027"),
    # No year in the provider's title either: silence stays silence.
    ({"title": "APAC | Singapore | Global Markets Recruitment Event"}, ""),
    # The plausible window holds: a founding year is not a cohort.
    ({"title": "Analyst at Firm est. 1999"}, ""),
    ({}, ""),
    (None, ""),
])
def test_cohort_from_provider_title(raw, expected):
    from directory.classify import cohort_from_provider_title
    assert cohort_from_provider_title(raw) == expected


# ---------------------------------------------------------------------------
# The DERIVED graduation year. `extract_class_year` above still refuses to
# infer — that column means "the posting said so". This is the separate,
# labelled answer to "who is this programme conventionally for", and the
# tests below are mostly about where it declines to answer.
#
# The rule it acts on: a summer internship is the penultimate-year placement
# everywhere this product covers, so its interns graduate the following year;
# a graduate programme hires that year's finishing class. Where the shape
# makes the distance to graduation vary — off-cycle placements, spring weeks,
# sophomore programmes, apprenticeships — there is no single answer and the
# function returns nothing at all.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bucket,title,cohort,expected", [
    # Summer internships: cohort N, graduating N+1.
    ("internship", "2027 Investment Banking Summer Analyst", "2027", "2028"),
    ("internship", "Asset Management — Summer Analyst", "2027", "2028"),
    # A summer ASSOCIATE is the MBA-level equivalent; same one-year distance.
    ("internship", "Investment Banking — Summer Associate", "2027", "2028"),
    # Graduate programmes hire the finishing class.
    ("entry_level", "2027 Guggenheim Investment Banking Analyst", "2027", "2027"),
    ("entry_level", "Global Operations Full Time Analyst - Tokyo - 2028",
     "2028", "2028"),
    # --- Refusals. Each shape below has more than one honest answer. ---
    # Off-cycle and seasonal placements: gap year, placement year, or after
    # graduating, posted for every quarter.
    ("internship", "Investment Banking — Seasonal/Off Cycle Internship",
     "2027", ""),
    ("internship", "Off-Cycle Intern - Debt Advisory (Q1, 2027)", "2027", ""),
    ("internship", "First Nations Students - Winter 2027 Co-op", "2027", ""),
    # Early-year programmes are two or three years out, and which one depends
    # on degree length — the exact variance that blocks a single answer.
    ("insight", "2027 Nomura Early Career Insight Evening", "2027", ""),
    ("internship", "2026 Sophomore Summer Analyst, Engineering", "2026", ""),
    ("internship", "Spring Week 2027", "2027", ""),
    # An internship naming no season could be any of the above.
    ("internship", "2027 Internship - Quantitative Trading", "2027", ""),
    ("internship", "Analytics Internship: Fall 2026", "2026", ""),
    # Shapes with no graduating class behind them at all.
    ("entry_level", "Apprentice hiring for 2026 – 2027", "2026", ""),
    ("entry_level", "Alternance 2026 - Analyste Crédit", "2026", ""),
    ("entry_level", "2027 EU Campus Programme Talent Community", "2027", ""),
    ("internship", "Quantitative Research Internship - PhD: Summer 2027",
     "2027", ""),
    # No programme year to reason from, and nothing to reason about.
    ("internship", "Summer Analyst Programme", "", ""),
    ("other", "Vice President, Fund Finance", "2027", ""),
])
def test_derive_class_year(bucket, title, cohort, expected):
    from directory.classify import derive_class_year
    year, why = derive_class_year(bucket, title, cohort)
    assert year == expected
    # An inference that cannot show its reasoning does not ship, and one that
    # declines must not leave a dangling explanation behind.
    assert bool(why) == bool(year)
    if year:
        assert "inferred" in why


def test_a_derived_year_is_never_a_stated_one():
    """The column separation is the whole safety property: `class_year` means
    the posting said so, and code and UI both trust it as such. A derivable
    shape must leave it empty."""
    from directory.classify import derive_class_year, extract_class_year
    title = "2027 Investment Banking Summer Analyst Program"
    assert extract_class_year(title) == ""
    assert derive_class_year("internship", title, "2027")[0] == "2028"


# ---------------------------------------------------------------------------
# The 2026-08-09 census, run after lifting the Workday page cap grew the
# campus set from 1,183 to 1,459 rows and re-opened a region gap.
#
# The interesting half is the exact-match tier. The European keys are checked
# BEFORE the American ones, so a European city name that is also an American
# one cannot be added as a substring: "rome" would have sent Romeoville,
# Illinois to Italy, and "trento" is a substring of "trenton".
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("location,expected", [
    # Whole-field only, and the American collisions they would have caused.
    ("Rome", "eu"),
    ("Romeoville, IL", "us"),
    ("Trento", "eu"),
    ("Trenton, NJ", "us"),
    ("Verona", "eu"),
    ("Belgrade", "eu"),
    ("Belgrade, MT", ""),          # Montana: stated, but not whole-field Serbia
    ("Bern", "eu"),
    # "bern" as a substring would have made a European of a fund manager.
    ("AllianceBernstein Summer Analyst", ""),
    ("Lucerne", "eu"),
    # Safe as substrings — the ASCII "zurich" key cannot see the umlaut.
    ("Zürich", "eu"),
    ("Treviso - Viale Felissent 90", "eu"),
    ("Neuilly-sur-Seine", "eu"),
    ("Rubano", "eu"),
    ("Saint Peter Port", "eu"),
    # Stated, untracked.
    ("Makati", "other"),
    ("Ipoh", "other"),
    ("Bandar Seri Begawan", "other"),
    ("Bermuda", "other"),
    ("Ulaanbaatar", "other"),
    ("Guadalajara", "other"),
    # Spelled-out state, and three state codes the suffix rule gained.
    ("Vineland, New Jersey", "us"),
    ("ANCHORAGE, AK", "us"),
    ("SUMMERVILLE, SC", "us"),
    ("Olathe, KS", "us"),
    # The boundary still holds for the new codes.
    ("Tbilisi, Akmola Region", ""),  # ", ak" must not fire inside "Akmola"
    (", Scotland", "eu"),            # ", sc" must not fire inside "Scotland"
    # Deliberately still unresolved: Bristol is a real city in England and in
    # Tennessee, Virginia and Connecticut, and the field says only "Bristol".
    ("Bristol", ""),
])
def test_region_census_20260809(location, expected):
    assert normalize_region(location) == expected


# ---------------------------------------------------------------------------
# The non-campus census (2026-08-09). Lifting the Workday cap to 1,500 grew
# the whole open set to 15,234 rows and brought a long tail of geography with
# it — banks file by BUILDING, and Workday's job slugs strip accents.
#
# As with the campus census, the collisions are the point: Europe is tested
# before America, so a European city that is also an American one has to go
# in the exact-match table. "Florence" was caught here — as a substring it
# sent Florence, South Carolina to Italy.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("location,expected", [
    # Filed by building, never by city.
    ("One Island East", "hk"),                   # Taikoo Place tower, 110 rows
    ("Kwun Tong", "hk"),
    ("The Center", "hk"),                        # whole-field only
    ("the center of excellence team", ""),       # ...because of this
    ("Building 400-Whippany Campus, Jefferson Park", "us"),
    ("Madison Ave Corp", "us"),
    ("Northampton, Barclays Campus, Pavilion Drive", "eu"),
    ("Cannon Street Office", "eu"),
    ("Manchester Spinningfields", "eu"),         # not Manchester, New Hampshire
    ("Dalian Office", "cn"),
    # Accent-stripped Workday slugs: the accented keys cannot see these.
    ("Montral Qubec", "other"),
    ("Saint Lonard Qubec", "other"),
    ("Zrich", "eu"),
    ("Genève", "eu"),
    # Canada, comma-prefixed so Ontario, California stays American.
    ("Brampton, Ontario", "other"),
    ("Ontario, CA", "us"),
    ("Surrey, British Columbia", "other"),
    ("Surrey, England", "eu"),
    ("Whitehorse, Yukon", "other"),
    # Latin America, South and Southeast Asia.
    ("Panamá City", "other"), ("CDMX", "other"), ("Caracas", "other"),
    ("Vikhroli", "other"), ("Cebu City", "other"), ("Melaka", "other"),
    ("Astana", "other"),
    # More spelled-out states, from slugs with no comma to key on.
    ("Southfield Michigan", "us"), ("Portland, Oregon", "us"),
    ("Bear, Delaware", "us"), ("MINNEAPOLIS, MN", "us"),
    # ", or" is deliberately NOT a state code: the boundary cannot save it,
    # because a space follows the "or" either way.
    ("London, or Paris", "eu"),
    # Whole-field only, each with a real American twin.
    ("Florence", "eu"), ("Florence, SC", "us"),
    ("Nassau", "other"),
    # Still refused outright: both readings are common in this industry.
    ("Naples", ""), ("Douglas", ""),
])
def test_non_campus_region_census_20260809(location, expected):
    assert normalize_region(location) == expected


# ---------------------------------------------------------------------------
# clean_title / _REQ_CODE — a trailing bracket with a digit and no internal
# space used to be stripped unconditionally, which ate IMC's bare cohort-year
# suffixes ("Graduate Software Engineer (2027)" -> "Graduate Software
# Engineer") and made two distinct, currently-open postings (Amsterdam job
# ids 4564480101 / 2026 and 4667814101 / 2027) clean to an identical title —
# directory/dupes.py then folded the 2027 row out of the feed entirely.
@pytest.mark.parametrize("title, expected", [
    # The confirmed defect: a bare 4-digit cohort year in parens must survive.
    ("Graduate Software Engineer (2026)", "Graduate Software Engineer (2026)"),
    ("Graduate Software Engineer (2027)", "Graduate Software Engineer (2027)"),
    ("Summer Analyst [2026]", "Summer Analyst [2026]"),
    # Real requisition codes must still be stripped — the fix must not
    # blunt the rule it is narrowing.
    ("Project Intern (J19302)", "Project Intern"),
    ("Business Analyst (R-788678)", "Business Analyst"),
    ("Data Engineer [REQ-30087]", "Data Engineer"),
    # A worded parenthetical (has a space) was already left alone.
    ("Summer Analyst (Summer 2027)", "Summer Analyst (Summer 2027)"),
    # A year plus anything else is still a real code, not a bare year.
    ("Graduate Engineer (2026A)", "Graduate Engineer"),
])
def test_clean_title_keeps_bare_cohort_years(title, expected):
    assert clean_title(title) == expected


# ---------------------------------------------------------------------------
# extract_sponsorship / _SPONSOR_NO's "now or in the future" phrase — round 4
# regression. The bare substring match could not tell a genuine employer
# NO-sponsorship statement from the standard US/HK/SG visa-status
# APPLICATION-FORM QUESTION every candidate answers regardless of the firm's
# real policy. Both text fixtures below are trimmed verbatim from live
# scraped `detail_text` (DRW id=3395 for the question, Wells Fargo id=19202
# for the declarative statement) — not paraphrased, so the fix is proven
# against the actual scraped shape, not an idealized one.
@pytest.mark.parametrize("text, expected", [
    # The confirmed defect: a scraped dropdown SCREENING QUESTION, bounded
    # by its own "?" and the "* Select..." form-field marker, must NOT read
    # as an employer NO-sponsorship statement.
    ("Are you a foreign national requiring a work pass to work in Singapore? "
     "* Select... Will you now or in the future require DRW to assist you "
     "with an application to the Ministry of Manpower for a work pass in "
     "respect of your prospective employment with DRW in Singapore? * "
     "Select... If you are currently the holder of a valid work pass",
     "unknown"),
    ("Will you now or in the future require employment pass sponsorship? "
     "* Select... In which language(s) are you fluent", "unknown"),
    ("Do you require visa sponsorship now or in the future? * Select... "
     "Which specific role or department", "unknown"),
    # The genuine article: a declarative employer sentence using the exact
    # same six words must still read as "no" — the fix must not blunt the
    # rule it is narrowing.
    ("Wells Fargo only considers candidates who are presently authorized to "
     "work for any employer in the United States and who do not require "
     "work visa sponsorship from Wells Fargo now or in the future in order "
     "to retain their authorization to work in the United States. Based on "
     "the volume of applications", "no"),
    ("Susquehanna cannot provide sponsorship for work authorization is not "
     "available for this position now or in the future. What we offer",
     "no"),
    ("MUFG will not hire individuals for internships whose work eligibility "
     "is based on their F-1 or other visa status that will expire and not "
     "require visa sponsorship now or in the future. MUFG will not hire",
     "no"),
    # A different _SPONSOR_NO phrase entirely must be unaffected by this gate.
    ("Unfortunately we are unable to sponsor employment visas for this role.",
     "no"),
    (None, "unknown"),
])
def test_extract_sponsorship_future_phrase_question_vs_statement(text, expected):
    assert extract_sponsorship(text) == expected


# ---------------------------------------------------------------------------
# extract_sponsorship — the Workday structured field and the additional
# declarative phrasings from the Decision 3 live-data sweep
# (docs/founder-decisions-2026-08-20.md). Fixtures are trimmed verbatim from
# live scraped detail text (PwC id=23662/16135/18193, Vanguard id=22299,
# HSBC id=23668, Invesco, BofA, Optiver, Belvedere) so the patterns are
# proven against the real scraped shape, not an idealised one.
@pytest.mark.parametrize("text, expected", [
    # PwC's Workday structured field: label, "?", then the bare answer,
    # immediately followed by the next field label with no punctuation.
    ("Travel Requirements Not Specified Available for Work Visa "
     "Sponsorship? No Government Clearance Required? No Job Posting End "
     "Date", "no"),
    ("Amount of Overnight Travel Up to 40% Available for Work Visa "
     "Sponsorship? Yes Government Clearance Required? No Job Posting End "
     "Date", "yes"),
    # Blank field: the label fires but the very next token is another
    # label, not "Yes"/"No" — must stay unknown, never guess.
    ("Travel Requirements Available for Work Visa Sponsorship? Government "
     "Clearance Required? Job Posting End Date", "unknown"),
    # A differently-labelled structured field (Belvedere): colon instead of
    # "?", no "Available for Work Visa" prefix.
    ("Amount of Travel Required: None Sponsorship: Yes Belvedere Trading",
     "yes"),
    ("Amount of Travel Required: None Sponsorship: No Belvedere Trading",
     "no"),
    # A bare "Sponsorship:" label with no Yes/No token right after it must
    # not manufacture an answer out of unrelated prose that happens to
    # follow.
    ("Educational Requirements Sponsorship: Bank of America is unable to "
     "consider candidates who will require visa sponsorship now, or in "
     "the future, for this specific role.", "no"),
    # Declarative NO phrasings the original _SPONSOR_NO list missed.
    ("Special Factors Vanguard is not offering visa sponsorship for this "
     "position. About Vanguard", "no"),
    ("Applicants must be legally authorized to work in the U.S. as HSBC "
     "will not engage in immigration sponsorship for this position. About "
     "the business area", "no"),
    ("We are seeking candidates authorized to work in the U.S. on a "
     "permanent basis. We do not offer any type of employment-based "
     "immigration sponsorship for this program. Invesco will not provide",
     "no"),
    ("Please note: Bank of America is unable to consider candidates that "
     "will require visa sponsorship now, or in the future, for this "
     "specific role.", "no"),
    ("You are a proficient user of MS Word, Access, Excel, and PowerPoint "
     "You will not require sponsorship for U.S. Work Authorization at "
     "any time", "no"),
    # Declarative YES phrasings the original _SPONSOR_YES list missed.
    ("Competitive relocation packages and visa sponsorship where "
     "necessary for expats. Who you are", "yes"),
    ("Optiver is supportive of US immigration sponsorship for this role. "
     "Optiver has a strong track record", "yes"),
])
def test_extract_sponsorship_structured_field_and_new_phrasings(text, expected):
    assert extract_sponsorship(text) == expected
