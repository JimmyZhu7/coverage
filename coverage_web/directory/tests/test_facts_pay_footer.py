"""extract_pay's evidence phrase must not run into an "Open positions"
footer block listing OTHER roles on the same scraped page.

Live regression: Optiver id=2815 ("Graduate FPGA Engineer (2027 Start -
Chicago)"). The stored fact was::

    {'value': '$200k',
     'phrase': 'Base Salary Range $200,000 — $200,000 USD Open positions '
                'HR Business Partner (12-month contract) Sydney Exper...'}

$200k is genuinely this role's own stated base salary -- the sentence right
before the match reads "This is a good-faith estimate of the base pay scale
for this position" -- but the quoted phrase tacked on the immediately
following footer block naming three unrelated open roles in Sydney. See
coverage_web/directory/facts.py's `_OTHER_POSTINGS_FOOTER` /
`_trim_footer` for the fix.
"""

from directory.facts import extract_pay


def test_pay_phrase_does_not_swallow_the_other_open_positions_footer():
    text = (
        "This is a good-faith estimate of the base pay scale for this "
        "position. Base Salary Range $200,000 — $200,000 USD Open "
        "positions HR Business Partner (12-month contract) Sydney "
        "Experienced Software Engineer Sydney Graduate Trader Chicago")
    got = extract_pay(text)
    assert got["value"] == "$200k"
    assert "Open positions" not in got["phrase"]
    assert "HR Business Partner" not in got["phrase"]
    assert "Sydney" not in got["phrase"]
    # The genuine evidence -- the actual salary statement -- must survive
    # the trim, not just get blanked out.
    assert "$200,000" in got["phrase"]


def test_pay_phrase_without_a_footer_is_unaffected():
    """No 'Open positions' marker present -> _trim_footer is a no-op."""
    got = extract_pay("Pay Range $85,000-$100,000 for this role.")
    assert got["value"] == "$85k–$100k"
    assert "$85,000" in got["phrase"]


def test_trim_footer_never_returns_an_empty_phrase():
    """A degenerate case where the marker sits at position 0 (should not
    happen given _sentence always anchors on the match, but the guard must
    hold regardless) leaves the phrase untouched rather than blanking it."""
    from directory.facts import _trim_footer

    assert _trim_footer("Open positions only") == "Open positions only"
    assert _trim_footer("") == ""
