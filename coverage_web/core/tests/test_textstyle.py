"""Table-driven tests for the site-wide smart_title filter — the cases are
real strings from live scrapes, which is exactly the mess the filter exists
to standardize."""

import pytest

from core.templatetags.textstyle import smart_location, smart_title


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
