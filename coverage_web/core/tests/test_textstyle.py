"""Table-driven tests for the site-wide smart_title filter — the cases are
real strings from live scrapes, which is exactly the mess the filter exists
to standardize."""

import pytest

from core.templatetags.textstyle import smart_location, smart_role, smart_title


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


# --- Nobiliary and patronymic particles -----------------------------------
# _MINOR shipped with the Romance particles ("de", "della", "du") and none of
# the Germanic or Scandinavian ones, so a name or a German-language title
# built on the missing half got its particle capitalized.

GERMANIC_PARTICLE_CASES = [
    # The founder's own contact row, and the exact name ContactMerge cites as
    # the duplicate-merge feature's motivating example — so it renders on the
    # Settings > Duplicate Contacts card, where it read "Ebba Af Klercker".
    ("Ebba af Klercker", "Ebba af Klercker"),
    ("Ursula von der Leyen", "Ursula von der Leyen"),
    ("Jan van den Berg", "Jan van den Berg"),
    ("Ludwig von Mises", "Ludwig von Mises"),
    # Live scraped titles: twelve German-language PwC/EY roles carry "in der".
    ("Trainee in der Steuerberatung", "Trainee in der Steuerberatung"),
    ("Praktikum in der Wirtschaftsprüfung",
     "Praktikum in der Wirtschaftsprüfung"),
    # ...and a Dutch one from Accenture carries "van" three times.
    ("De Rol van Hybride Warmtesystemen", "De Rol van Hybride Warmtesystemen"),
    # A particle that OPENS the phrase is part of the name, not a connective
    # inside it, so force_cap still applies — the same rule "Del Rey Oaks"
    # already depends on.
    ("Von Neumann", "Von Neumann"),
    ("Van Lanschot Kempen", "Van Lanschot Kempen"),
]


@pytest.mark.parametrize("raw,expected", GERMANIC_PARTICLE_CASES)
def test_a_germanic_particle_stays_lowercase_mid_name(raw, expected):
    assert smart_title(raw) == expected


def test_a_lowercase_particle_survives_in_a_location_too():
    """Three live Deutsche Bank rows carry a street name built on the same
    particles, and smart_location shares _MINOR with smart_title."""
    assert smart_location("Berlin, Unter den Linden 13-15 (O)") == (
        "Berlin, Unter den Linden 13-15 (O)")
    assert smart_location("Hamburg, An der Alster 63-64") == (
        "Hamburg, An der Alster 63-64")


def test_a_particle_cannot_be_spelled_across_a_hyphen():
    """The minor-word test measures a token with its separators stripped, so
    a compound can spell a particle across the gap that neither half spells
    on its own: the building code "A-12F" measures as the letters "AF", and
    adding the Swedish particle to _MINOR turned three live 'Honhui A-12F'
    rows into 'Honhui a-12f'. Letters on both sides of a separator mean a
    code, never a word."""
    assert smart_location("Honhui A-12F") == "Honhui A-12F"
    assert smart_title("Suite D-E") == "Suite D-E"


def test_a_particle_welded_to_punctuation_is_still_a_particle():
    """The guard above must not overreach into the shapes real rows ship: a
    particle really does arrive with a separator hanging off one side, and
    each of these is still a single lettered atom."""
    assert smart_title("English and /or French") == "English and /or French"
    assert smart_title("Personal Banking - Rivière- des- Prairies") == (
        "Personal Banking - Rivière- des- Prairies")


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
