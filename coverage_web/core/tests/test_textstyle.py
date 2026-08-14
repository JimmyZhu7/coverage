"""Table-driven tests for the site-wide smart_title filter — the cases are
real strings from live scrapes, which is exactly the mess the filter exists
to standardize."""

import pytest

from core.templatetags.textstyle import smart_title


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
