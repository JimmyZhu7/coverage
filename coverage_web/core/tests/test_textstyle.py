"""Table-driven tests for the site-wide smart_title filter — the cases are
real strings from live scrapes, which is exactly the mess the filter exists
to standardize."""

import pytest

from core.templatetags.textstyle import (
    smart_location, smart_person_name, smart_role, smart_title,
)


CASES = [
    # Shouting from real Workday boards gets title-cased…
    ("OLIVER WYMAN - INTERN CONSULTANT – 2026 – NETHERLANDS",
     "Oliver Wyman - Intern Consultant – 2026 – Netherlands"),
    # …while short all-caps tokens are acronyms and survive.
    ("TPG", "TPG"),
    ("RBC Capital Markets", "RBC Capital Markets"),
    ("M&A Analyst", "M&A Analyst"),
    ("2026 Summer Analyst, Institutional Client Group",
     "2026 Summer Analyst, Institutional Client Group"),
    # Mixed-case branding is preserved, never "fixed".
    ("PwC Graduate Programme - Tax (Auckland)", "PwC Graduate Programme - Tax (Auckland)"),
    ("McKinsey & Company", "McKinsey & Company"),
    ("BofA Securities", "BofA Securities"),
    # Lowercase drift from real boards gets capitalized.
    ("international Corporate Tax Associate", "International Corporate Tax Associate"),
    ("associate de auditoría financiera", "Associate de Auditoría Financiera"),
    # Minor words stay lowercase mid-phrase, capitalized at the edges.
    ("head of diversity", "Head of Diversity"),
    ("women in banking programme", "Women in Banking Programme"),
    ("the brattle group", "The Brattle Group"),
    # Hyphen/slash compounds are cased per part.
    ("off-cycle internship", "Off-Cycle Internship"),
    ("intern (m/f/d)", "Intern (M/F/D)"),
    # Person names.
    ("daniel kim", "Daniel Kim"),
    # Whitespace collapses (real rows ship trailing spaces).
    ("Fund Finance (TRECO), Associate ", "Fund Finance (TRECO), Associate"),
    ("Women in Banking  Programme - London 2026", "Women in Banking Programme - London 2026"),
    # Degenerate inputs pass through.
    ("", ""),
    (None, None),
]


@pytest.mark.parametrize("raw,expected", CASES)
def test_smart_title(raw, expected):
    assert smart_title(raw) == expected


# --- Symbol-stitched brand marks and lowercase-stored acronyms -------------
# Regression tests for the confirmed defect: Network's Follow Up/Others
# queues rendered "Nianxu Wang / E*trade" and "Kristin Welty / Usc" while
# Today's cards happened to show "E*TRADE"/"USC" only because .opp-firm's
# CSS blanket-uppercases everything on that card type — smart_title() itself
# mis-cased both, and any page without that CSS (Network's net-mini-firm/
# cc-firm, no text-transform) showed the garbled form.

def test_a_symbol_stitched_brand_survives_recasing():
    """E*TRADE: _letters() strips the "*", leaving "ETRADE" (6 letters) —
    over the 4-letter acronym cutoff and not in the whitelist, so the old
    code treated it as shouting and recapped to "E*trade"."""
    assert smart_title("E*TRADE") == "E*TRADE"


def test_an_ampersand_stitched_brand_still_works():
    assert smart_title("AT&T") == "AT&T"


def test_a_lowercase_stored_acronym_is_recognized():
    """Contact.firm_text stores free text a student typed; the same school
    acronym shows up as "USC" from one contact and "usc" from another. The
    old code only preserved acronym casing when the DB row was ALREADY
    all-uppercase, so "usc" fell through to _recap() and became "Usc"."""
    assert smart_title("usc") == "USC"
    assert smart_title("USC") == "USC"


def test_an_ordinary_lowercase_short_word_is_not_treated_as_an_acronym():
    """The fix is a curated whitelist, not "any short lowercase token is an
    acronym" — that would mis-case ordinary short words and firm names the
    same way it currently mis-cases real acronyms."""
    assert smart_title("sap") == "Sap"
    assert smart_title("the brattle group") == "The Brattle Group"


# --- Locations are not titles ---------------------------------------------
# `smart_title`'s docstring scopes it to firm names, job titles and person
# names, but six templates piped Opportunity.location through it, so the
# English title-case minor-word convention ran over place names. Every RAW
# string below is a live open-role location.

LOCATION_CASES = [
    # The reported defect: a capitalized particle mid-string got downcased.
    ("Batesville; Des Moines", "Batesville; Des Moines"),
    ("Boston or Des Moines", "Boston or Des Moines"),
    ("West Des Moines, Iowa, United States of America",
     "West Des Moines, Iowa, United States of America"),
    # ...and it was never only the Iowa city.
    ("Wilmington, DE, United States", "Wilmington, DE, United States"),
    ("Milano Via Turati 25-27", "Milano Via Turati 25-27"),
    ("WI-Milwaukee, 411 E Wisconsin Ave Ste 1850",
     "WI-Milwaukee, 411 E Wisconsin Ave Ste 1850"),
    ("Gemini Building A, Prague", "Gemini Building A, Prague"),
    ("Portage La Prairie, Manitoba", "Portage La Prairie, Manitoba"),
    ("Puerto La Cruz", "Puerto La Cruz"),
    # A particle the SOURCE wrote lowercase is its own orthography and stays.
    # This is why deleting "des" from _MINOR would have been the wrong fix.
    ("Geneva Place des Bergues 3", "Geneva Place des Bergues 3"),
    ("Rio de Janeiro", "Rio de Janeiro"),
    # An all-caps particle inside a run carries no case signal, so the
    # title-case convention still decides it.
    ("VILLE DE QUEBEC, Canada", "Ville de Quebec, Canada"),
    ("RIO DE JANEIRO", "Rio de Janeiro"),
    # ...but a particle that CLOSES one is still a particle. ISO 3166 long
    # forms end in one, and the app must not manufacture a capital the source
    # never wrote: the DB stores 'Republic of', so the page says 'Republic of'.
    ("Seoul, Korea, Republic of", "Seoul, Korea, Republic of"),
    ("Korea, Republic of", "Korea, Republic of"),
    ("Taipei, Taiwan, Province of China", "Taipei, Taiwan, Province of China"),
    # Same rule, different shape: a trailing street-number suffix is not a
    # word to capitalize either.
    ("Salzburg - Wilhelm-Spazier-Straße 2a", "Salzburg - Wilhelm-Spazier-Straße 2a"),
    # A particle that OPENS the name is part of it.
    ("EL DORADO HILLS, CA", "El Dorado Hills, CA"),
    ("DEL REY OAKS, CA", "Del Rey Oaks, CA"),
    ("ON-81 Bay Street-Virtual", "ON-81 Bay Street-Virtual"),
    # Shouting city names lose their fake acronyms...
    ("SAN FRANCISCO, CA", "San Francisco, CA"),
    ("NEW YORK, NY", "New York, NY"),
    # ...but a real state/territory code in its own slot survives, comma or not.
    ("WASHINGTON DC", "Washington DC"),
    ("NYC (1285)", "NYC (1285)"),
    ("NY - 375 - 18", "NY - 375 - 18"),
    ("", ""),
    (None, None),
]


@pytest.mark.parametrize("raw,expected", LOCATION_CASES)
def test_smart_location(raw, expected):
    assert smart_location(raw) == expected


def test_the_title_filter_still_downcases_minor_words():
    """smart_location must not have been implemented by weakening _MINOR —
    titles still want the convention that locations do not."""
    assert smart_title("Head of Diversity") == "Head of Diversity"
    assert smart_title("Women in Banking") == "Women in Banking"


def test_a_shouting_location_is_the_one_case_that_cannot_be_recovered():
    """Documented limit, pinned so a future reader knows it is known rather
    than missed: 'DES' in an all-caps string is indistinguishable from the
    'DE' of RIO DE JANEIRO, and only one of them wants a capital. One open
    row is affected; the ten mixed-case 'Des Moines' rows above are not."""
    assert smart_location("WEST DES MOINES, IA") == "West des Moines, IA"


# --- Clause boundaries inside a title -------------------------------------
# force_cap applied only to index 0 and the last index OF THE WHOLE STRING, so
# a title that restarts mid-way opened its second half with a lowercase word.
# Every RAW string below is a live open-role title.

BOUNDARY_CASES = [
    ("Bank of America Campus Insight Forum: The Power to Lead - Fall 2026",
     "Bank of America Campus Insight Forum: The Power to Lead - Fall 2026"),
    ("2026 Women Who Lead: An Insight into Banking",
     "2026 Women Who Lead: An Insight into Banking"),
    # A standalone dash or pipe is the same boundary, and the bigger half of
    # the defect: nine more live titles.
    ("Senior Premier Banker - La Cienega Corridor",
     "Senior Premier Banker - La Cienega Corridor"),
    ("VP, Regional Vice President - External Wholesaler - LA County, CA",
     "VP, Regional Vice President - External Wholesaler - LA County, CA"),
    ("APAC Virtual Recruitment Event | A Career with Bank of America in China",
     "APAC Virtual Recruitment Event | A Career with Bank of America in China"),
    ("Business Manager | S3 | T&O | Milton Keynes",
     "Business Manager | S3 | T&O | Milton Keynes"),
    ("Associate Banker II - So Portland, ME (Market St)",
     "Associate Banker II - So Portland, ME (Market St)"),
    ("Manager Customer Experience - Des Sources Branch",
     "Manager Customer Experience - Des Sources Branch"),
    # A minor word that is NOT at a boundary still stays lowercase.
    ("Bank of America Campus Insight Forum: The Power to Lead",
     "Bank of America Campus Insight Forum: The Power to Lead"),
]


@pytest.mark.parametrize("raw,expected", BOUNDARY_CASES)
def test_a_clause_boundary_restarts_title_case(raw, expected):
    assert smart_title(raw) == expected


def test_into_is_a_minor_word():
    """The same title downcased a minor word after a colon AND upcased a
    preposition in one line: "…Lead: an Insight Into Banking…". "into" was
    simply missing from _MINOR."""
    assert smart_title("2027 Insight Into Internal Audit Opportunities") == (
        "2027 Insight into Internal Audit Opportunities")
    assert smart_title("Bank of America | Insight Day - Step into Finance") == (
        "Bank of America | Insight Day - Step into Finance")


# ---------------------------------------------------------------------------
# Ordinals. The DB values are clean — a regex for [0-9](St|Nd|Rd|Th) over every
# open row's stored location matches nothing — so this mis-casing was entirely
# manufactured at render time, on 140 open rows (122 locations, 18 titles).
#
# The fix lives in `_recap`, the leaf BOTH filters funnel each token into, not
# in `smart_title`: 122 of the 140 render through `smart_location`, so a fix
# in smart_title alone would have gone green on its own test and left every
# one of them on the page still reading "745 7Th Avenue".
# ---------------------------------------------------------------------------
ORDINAL_LOCATIONS = [
    ("New York, 745 7th Avenue", "New York, 745 7th Avenue"),
    ("Mumbai, Nirlon Knowledge Park (BX) 9th & 11-12 Floor",
     "Mumbai, Nirlon Knowledge Park (BX) 9th & 11-12 Floor"),
    ("Calgary, 888 3rd Street SW", "Calgary, 888 3rd Street SW"),
    ("New York, NY (1271 AOA/6th Ave)", "New York, NY (1271 AOA/6th Ave)"),
    ("Pasig - 4th Floor JMT Corporate Condominium",
     "Pasig - 4th Floor JMT Corporate Condominium"),
    ("1st Avenue", "1st Avenue"),
    # A shouting source has no case of its own to respect, so the ordinal is
    # folded down with everything else rather than left as "42ND".
    ("WEST 42ND STREET", "West 42nd Street"),
]


@pytest.mark.parametrize("raw,expected", ORDINAL_LOCATIONS)
def test_an_ordinal_suffix_is_not_a_word_start_in_a_location(raw, expected):
    assert smart_location(raw) == expected


ORDINAL_TITLES = [
    ("Relationship Banker II (19th & 1st)", "Relationship Banker II (19th & 1st)"),
    ("Relationship Banker 83rd Ave & Lake Pleasant Pkwy",
     "Relationship Banker 83rd Ave & Lake Pleasant Pkwy"),
    ("Senior Manager, 1st Line Controls", "Senior Manager, 1st Line Controls"),
    ("October 19th - Direct Investing", "October 19th - Direct Investing"),
    ("22nd Street", "22nd Street"),
]


@pytest.mark.parametrize("raw,expected", ORDINAL_TITLES)
def test_an_ordinal_suffix_is_not_a_word_start_in_a_title(raw, expected):
    assert smart_title(raw) == expected


def test_the_ordinal_guard_does_not_swallow_ordinary_words():
    """The guard keys on "digit immediately before the letters", so a word
    that merely BEGINS with an ordinal's letters is untouched. Without the
    boundary the regex would also have matched the "st" of "Stanley"."""
    assert smart_title("morgan stanley") == "Morgan Stanley"
    assert smart_title("this thursday") == "This Thursday"
    assert smart_title("2026 standard chartered") == "2026 Standard Chartered"
    # A digit glued to letters that are NOT an ordinal suffix still recaps.
    assert smart_title("3m company") == "3M Company"
    assert smart_location("500 startup lane") == "500 Startup Lane"


# Every distinct pattern found on the founder's real board (2026-08-31),
# reduced to its shape rather than kept as 85 near-duplicates: already-clean
# titles must pass through untouched, a "Title, Department" pattern keeps
# the title and drops the department, a parenthetical aside is dropped
# whole, an em-dash elaboration is dropped whole, and a runaway sentence
# with none of those boundaries still gets capped so it never reaches the
# page as a full sentence.
ROLE_CASES = [
    ("IB Analyst", "IB Analyst"),
    ("PE Associate", "PE Associate"),
    ("Recruiting", "Recruiting"),
    ("Manager, Talent Acquisition", "Manager"),
    ("Account Manager, AWS", "Account Manager"),
    ("Senior Analyst, Commodities & Global Markets", "Senior Analyst"),
    ("Associate Director, Credit Analyst, C&IB", "Associate Director"),
    ("Professor (USC Dornsife, WRIT 150)", "Professor"),
    ("USC junior/senior peer (coffee-chat contact)", "USC junior/senior peer"),
    (
        "Campus recruiter (PwC) — self-described 'the campus recruiter and "
        "primary point of contact' for USC students",
        "Campus recruiter",
    ),
    (
        "USC on-campus staff — Assistant Director, Dornsife First-Year "
        "Advising (academic advising, not career services)",
        "USC on-campus staff",
    ),
    ("IB Associate - M&A", "IB Associate"),
    ("Investment Banking", "IB"),
    ("Private Equity Associate", "PE Associate"),
    ("Technology IB Associate", "Tech IB Associate"),
    ("Asset Management Analyst", "AM Analyst"),
    ("Sales & Trading Analyst", "S&T Analyst"),
    ("Sales and Trading", "S&T"),
    (
        "BCG contact via USC International Consulting Club alumni panel outreach",
        "BCG contact via USC",
    ),
    ("", ""),
    (None, None),
]


@pytest.mark.parametrize("raw,expected", ROLE_CASES)
def test_smart_role_compresses_to_the_first_clean_clause(raw, expected):
    assert smart_role(raw) == expected


def test_smart_role_never_invents_a_word_that_was_not_typed():
    """The whole safety property: every possible output is a strict PREFIX
    of the input, so it can shorten what a student wrote and can never
    assert something they did not."""
    cases = [
        "USC alum, finance professional",
        "Campus recruiting manager (Deloitte, national) — made the "
        "introduction to USC's specific recruiter",
        "Director, TMT Investment Banking",
    ]
    for raw in cases:
        out = smart_role(raw)
        assert raw.startswith(out), (raw, out)


def test_smart_role_respects_a_lower_word_cap():
    assert smart_role("One Two Three Four Five Six", max_words=3) == "One Two Three"


# --- smart_person_name: an email-local-part-shaped name, made readable ----
# Verified against the founder's own real 43 (2026-08-31, read-only): every
# firstname.lastname case below is a real `Contact.name` / `Contact.email`
# pair taken off his live board, not an invented example.
PERSON_NAME_CASES = [
    # The measured bug, real pairs.
    ("jude.yoon", "Jude Yoon"),
    ("wenyu.xiong", "Wenyu Xiong"),
    ("dongyoon.kim", "Dongyoon Kim"),
    ("monica.xiao", "Monica Xiao"),
    ("shubham.gupta", "Shubham Gupta"),
    # Underscore and hyphen are corporate-address separators too.
    ("jude_yoon", "Jude Yoon"),
    ("jude-yoon", "Jude Yoon"),
    # A typed name is untouched: it has a space, full stop.
    ("Youqi Chen", "Youqi Chen"),
    ("J.P. Morgan Recruiting", "J.P. Morgan Recruiting"),
    # Mixed case already present reads as a deliberate typed name, not raw
    # local-part evidence — left alone rather than overwritten.
    ("John_Smith", "John_Smith"),
    # A single initial split from a real name cannot be told apart from a
    # typo cutting a real word short, so the whole thing is left alone.
    ("j.smith", "j.smith"),
    # A digit anywhere in a piece is not a name syllable.
    ("jsmith2", "jsmith2"),
    ("team-2026", "team-2026"),
    # No separator at all: a bare local part like the real 'cv' (from
    # 'cv@citi.com'), or a single already-good given name like the real
    # 'Kirthi' and 'Matt' — nothing here to split, so nothing changes.
    ("cv", "cv"),
    ("Kirthi", "Kirthi"),
    ("Matt", "Matt"),
    # Degenerate inputs pass through.
    ("", ""),
    (None, None),
]


@pytest.mark.parametrize("raw,expected", PERSON_NAME_CASES)
def test_smart_person_name(raw, expected):
    assert smart_person_name(raw) == expected


def test_smart_person_name_never_touches_a_name_with_a_space():
    """The one signal this filter trusts completely: whitespace means a
    person typed word breaks on purpose. No case in this file may pass a
    spaced input through the separator-splitting logic at all."""
    for raw in ("Youqi Chen", "J.P. Morgan Recruiting", "Ellen Huang", "  Jude   Yoon  "):
        assert smart_person_name(raw) == raw


def test_smart_person_name_chains_cleanly_with_smart_title():
    """The real call sites pipe `name|smart_person_name|smart_title` — the
    first filter turns the shape into words, the second standardizes their
    case the same way every other name on the page is standardized."""
    assert smart_title(smart_person_name("jude.yoon")) == "Jude Yoon"
    assert smart_title(smart_person_name("Youqi Chen")) == "Youqi Chen"


# ---------------------------------------------------------------------------
# Nobiliary particles (2026-09-01). "Ebba af Klercker" is a real row on the
# founder's board -- the one `ContactMerge`'s docstring names as the case the
# merge feature exists for -- and it rendered "Ebba Af Klercker" on the
# Decisions card until "af" joined _MINOR.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "raw,expected",
    [
        ("ebba af klercker", "Ebba af Klercker"),
        ("Ebba af Klercker", "Ebba af Klercker"),
        ("ludwig van beethoven", "Ludwig van Beethoven"),
        ("jan von neumann", "Jan von Neumann"),
        # A LEADING particle is still a capital: `force_cap` runs before the
        # _MINOR lookup, which is what keeps real firm names intact.
        ("van lanschot", "Van Lanschot"),
        ("Van Lanschot Kempen", "Van Lanschot Kempen"),
    ],
)
def test_smart_title_lowercases_a_particle_but_never_a_leading_one(raw, expected):
    assert smart_title(raw) == expected


# ---------------------------------------------------------------------------
# A whole address stored as a name (2026-09-01). Capture usually keeps only
# the local part, but two of the founder's rows hold the full string and
# rendered as "Victoria.hsu@gs.com" on the firm page and in the Cmd-K
# palette. The domain says nothing a reader wants in a name.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "raw,expected",
    [
        ("victoria.hsu@gs.com", "Victoria Hsu"),
        ("yvonne.cheng@gs.com", "Yvonne Cheng"),
        # The local part still has to clear the same shape rule: one token
        # is ambiguous, so it is left alone rather than guessed at.
        ("cv@citi.com", "cv@citi.com"),
        # Not an address: nothing before the @, or more than one.
        ("@handle", "@handle"),
        ("a@b@c", "a@b@c"),
        # A real typed name that happens to contain an address is untouched,
        # because the whitespace check upstream already refuses it.
        ("Youqi Chen <youqi@ms.com>", "Youqi Chen <youqi@ms.com>"),
    ],
)
def test_smart_person_name_reads_a_whole_address(raw, expected):
    assert smart_person_name(raw) == expected


# ---------------------------------------------------------------------------
# `timesince1` — cross-surface consistency audit, finding C. Django's
# `timesince` template filter defaults to `depth=2` and has no way to pass a
# depth (its one argument is a comparison time), so every template that
# wanted the product's own one-unit convention had nothing to reach for and
# rendered "1 hour, 38 minutes ago" / "5 days, 13 hours ago" — noise in a
# sentence meant to be read at a glance, and inconsistent with the Python
# call sites (`directory.views`' `checked_ago` and closed-posting note) that
# already pass `depth=1` directly.
# ---------------------------------------------------------------------------
from datetime import timedelta

from django.utils import timezone
from django.utils.timesince import timesince

from core.templatetags.textstyle import timesince1


def test_timesince1_collapses_to_one_unit():
    now = timezone.now()
    # 5 days, 13 hours ago — the exact shape named in the audit
    # (`templates/assistant/_message.html`'s old "5 days, 13 hours ago").
    # Default `timesince` renders two units; `timesince1` must render one.
    two_units = timesince(now - timedelta(days=5, hours=13), now)
    one_unit = timesince1(now - timedelta(days=5, hours=13))
    assert two_units.count(",") == 1
    assert "," not in one_unit
    # Django's `timesince` joins the count and unit with a non-breaking
    # space (`\xa0`), not a plain one — matched literally rather than
    # normalized, so a future Django upgrade that changes the separator
    # fails this test instead of silently changing the rendered HTML.
    assert one_unit == "5\xa0days"


def test_timesince1_passes_through_falsy_values_unchanged():
    """A blank/`None` timestamp is a real, expected case at every call site
    (`o.closed_at`, `row.created`, `gmail_live.last_notification_at` are all
    optional) — the filter must not raise or print "0 minutes" for it."""
    assert timesince1(None) is None
    assert timesince1("") == ""
