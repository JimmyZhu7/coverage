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
    ("Remote", ""),
])
def test_other_markets_file_under_other_and_tracked_markets_win_first(location, expected):
    assert normalize_region(location) == expected
