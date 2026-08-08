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
    classify_role,
    extract_class_year,
    extract_cohort,
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
    ("Almaty, Ust-Kamenogorsk", ""),      # ", us" must not fire inside "Ust"
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
