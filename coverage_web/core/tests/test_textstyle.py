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
