"""Numbers and claims that may not appear in the product.

Section 5.7 of `docs/plans/2026-09-01-product-plan.md` lists thirteen figures
and claims that are fabricated, unsourced, or prep-vendor marketing. The plan
says a grep should enforce them, and this is that grep, because the failure
mode they share is that each one READS like a fact: "65% of bulge-bracket
analysts come from target schools" has no underlying study and is recycled as
fact across every prep site, so an author who half-remembers it will type it in
good faith. A rule in a document nobody greps is a rule that gets broken by
somebody who never read the document.

P1 is the principle underneath: every fact the product states carries its
provenance, and a fact it cannot source is left blank rather than inferred. A
match here is not fixed by finding a citation — the plan's own instruction is
that the number "must be removed, not sourced".

SCOPE: templates and non-test Python under `coverage_web/`. Not `docs/`, which
is where these strings legitimately live (this file, the plan, and the research
that killed each one). Not tests, for the same reason.

EACH PATTERN IS NARROW ON PURPOSE. The point is to catch the claim, not every
sentence near it: "sponsorship" is a word the product uses constantly and "5 to
10%" is a shape that could legitimately describe something else one day. Where
a number alone would be ambiguous, the pattern requires the phrase that makes
it the blocked claim.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
WEB = REPO_ROOT / "coverage_web"

# (plan row, pattern, what it is and what killed it)
_BLOCKED: list[tuple[str, str, str]] = [
    ("51", r"semi[- ]target",
     "the 65/26/9 target-school split. No underlying study; recycled as fact "
     "across prep sites (research-nontarget-access.md, Grade D)."),
    ("52", r"high[- ]school alumni",
     "the response-rate tiers (5-10% cold, 20-30% college alumni, 50-85% high "
     "school alumni, 70-90% warm intro). Near-identical across four prep "
     "sites with no source anywhere (Grade D)."),
    ("53", r"middle[- ]market analysts are non[- ]target",
     "the 30-42% figure, same pattern as 51 (Grade D)."),
    ("54", r"250,?000 applications|2,?900 spots",
     "Goldman's 250,000/2,900/1.16%. Contradicted by Fortune's firm-sourced "
     "360,000 and about 2,600, which is 0.72%."),
    ("55", r"times more likely to convert",
     "the insight-programme conversion multiple. Prep-vendor origin, no "
     "methodology (research-us-ib-calendar.md §9, Grade D, 'actively "
     "reject')."),
    ("56", r"\bPymetrics\b|\bCasey\b",
     "consulting assessment-vendor names asserted by the product. BCG's own "
     "live page names neither (research-consulting-forums.md §2.2), so a "
     "product that says 'BCG uses Pymetrics' is repeating prep-vendor "
     "marketing. Reading the name out of a posting that prints it is the "
     "opposite thing and is exempted below."),
    ("57", r"80 in 8|115,?900",
     "quant online-assessment mechanics and acceptance rates. Grade C and D, "
     "prep vendors (research-st-quant.md §4, §6)."),
    ("58", r"hire 5 to 10 analysts",
     "Hong Kong IB division headcount. A single anonymous forum estimate; the "
     "research file says do not act on the number (research-hongkong.md §5)."),
    ("59", r"[Nn]etworking is essential in Hong Kong",
     "the passage is WSO's AI bot and contradicts the human guide in the same "
     "corpus (research-hongkong.md §6, Grade D)."),
    ("61", r"less competitive than (IB|investment banking)",
     "corporate banking against IB. Prep-course marketing and forum anecdote "
     "(research-am-corpbank.md §3.7, Grade C/D)."),
    ("62", r"target[- ]school student needs a calendar",
     "not supported: the target student needs no part of Coverage, the "
     "non-target needs all five (research-nontarget-access.md Verdict)."),
]


# Exempt, by (plan row, path). Each entry is a place the string appears because
# the product is READING it out of a posting rather than asserting it.
#
# `directory/facts.py::_ASSESSMENTS` maps a vendor name found in a posting's own
# text to a label, and hands back the sentence it found it in. That is the
# read-and-relabel `research-diversity-early-programs.md §10.7` explicitly asks
# for — "scrape the firm's own live page or say nothing" — and P2's rule that a
# posting's stated fact beats any inference. What row 56 blocks is the other
# direction: the product telling a student which vendor a firm uses when no
# firm page says so.
_EXEMPT: set[tuple[str, str]] = {
    ("56", "coverage_web/directory/facts.py"),
}


def _product_files():
    """Templates and non-test Python under `coverage_web/`."""
    for path in WEB.rglob("*.html"):
        yield path
    for path in WEB.rglob("*.py"):
        parts = path.parts
        if "tests" in parts or "migrations" in parts:
            continue
        if path.name.startswith("test_") or path.name == "conftest.py":
            continue
        yield path


@pytest.mark.parametrize(
    "row,pattern,why", _BLOCKED, ids=[row for row, _, _ in _BLOCKED])
def test_a_blocked_number_never_reaches_the_product(row, pattern, why):
    compiled = re.compile(pattern)
    hits = []
    for path in _product_files():
        if (row, str(path.relative_to(REPO_ROOT))) in _EXEMPT:
            continue
        for number, line in enumerate(
                path.read_text(encoding="utf-8", errors="ignore").splitlines(),
                start=1):
            if compiled.search(line):
                hits.append(f"{path.relative_to(REPO_ROOT)}:{number}: "
                            f"{line.strip()[:120]}")
    assert not hits, (
        f"do-not-say register row {row}: {why}\n\nThe plan's instruction is "
        f"that the number must be REMOVED, not sourced:\n  "
        + "\n  ".join(hits)
    )


def test_the_register_is_not_quietly_empty():
    """A guard on the guard, the same shape
    `test_stress_tenancy.py::test_the_discovery_actually_found_the_private_zone`
    uses: a parametrised check over an empty list is green while checking
    nothing, and so is one whose file walk finds no files."""
    assert len(_BLOCKED) >= 11
    files = list(_product_files())
    assert len(files) > 100, f"only walked {len(files)} product files"


def test_the_check_would_actually_catch_one():
    """Proof it fires. Run against a synthetic line rather than by writing a
    blocked claim into a real template, because a file written to prove a
    point is a file somebody later finds and believes."""
    fake = "  <p>About 65% come from target schools, 26% semi-target.</p>"
    assert any(re.search(pattern, fake) for _, pattern, _ in _BLOCKED)
