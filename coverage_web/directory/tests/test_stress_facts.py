"""Adversarial invariant suite for `directory/facts.py`'s requirement
extractors — the vocabulary that produces a role card's chips ("GPA 3.2",
"French needed", "Transcript").

WHAT MAKES THIS DIFFERENT from `test_facts.py` next door. That file is
example-based: it pins one confirmed-live defect per extractor and documents
what each pattern is FOR. This file asks the cross-cutting question a
per-extractor test cannot: does every extractor that claims to honour a
negation actually honour EVERY SPELLING of one, and does the GPA extractor
resolve every ordering of a dual-scale statement to the same reading? Two
confirmed live defects fed this suite — 42 of 214 open `language` facts
(PwC Canada, "however this isn't a requirement") and 2 of the corpus's
`gpa` facts (HSBC, "a minimum GPA of 4.0/5.0 or 3.2/4.0") — both shaped
exactly like "one extractor has a hole and its siblings do not, and nobody
noticed which extractor". `extract_cover_letter`/`extract_transcript` share
the negation contract but had zero live hits when this suite was written;
they are exercised here on the same footing as `extract_languages` so a
regression in any of the three is caught before it ships, not after an
audit finds it live.

Same discipline as `test_stress_ai_extract.py`, `test_stress_recommend.py`
and `crm/tests/test_stress_crm.py`: NO `hypothesis`. The interesting spaces
here are small enumerated cross-products — 9 negation spellings x 3
requirement extractors x 2 clause shapes, 2 scale orderings x 3 denominator
placements — walked EXHAUSTIVELY.

THE INVARIANTS

  1. A NEGATION IS A NEGATION REGARDLESS OF SPELLING. "not", and every
     common contraction that ends "n't" (isn't/doesn't/aren't/wasn't/
     weren't/wouldn't/won't/can't), must suppress a requirement chip the
     same way — for all three extractors that share this contract, not just
     the one an audit happened to check.

  2. A NEGATION SUPPRESSES REGARDLESS OF CLAUSE SHAPE. Whether the negation
     sits AHEAD of the trigger word ("we do not require a transcript") or
     FOLDED INSIDE the matched span ("a transcript isn't required"), the
     chip must not fire.

  3. A REAL REQUIREMENT IS UNAFFECTED. The widened negation window must not
     swallow a genuine, unhedged requirement — every positive counterpart of
     each cross-product case above still produces a chip.

  4. A DUAL-SCALE GPA STATEMENT RESOLVES TO ITS 4.0-SCALE READING regardless
     of which fraction the posting states first — "4.0/5.0 or 3.2/4.0" and
     "3.2/4.0 or 4.0/5.0" must produce the identical chip.
"""

from __future__ import annotations

import pytest

from directory.facts import (extract_cover_letter, extract_gpa,
                             extract_languages, extract_transcript)

# ---------------------------------------------------------------------------
# 1 & 2 & 3. Negation spelling x clause shape, across every extractor that
# shares the negation contract.
# ---------------------------------------------------------------------------

# Every common negation this module claims to honour: the bare word, plus
# every contraction ending "n't" that a posting's own template has been
# observed to use (PwC Canada's "isn't a requirement" is the live one;
# the rest are the same mechanism — `n't\b` — applied to every auxiliary a
# posting could plausibly pair it with).
NEGATIONS = ("not", "isn't", "doesn't", "wasn't", "weren't", "aren't",
             "wouldn't", "won't", "can't")

# One (positive, negated-after, negated-before) template triple per
# extractor. `{neg}` sits where a posting's own auxiliary would; the
# extractors are character-level regexes with no grammar of their own, so a
# contraction standing in for a mismatched auxiliary ("this weren't a
# requirement") is exactly as valid a stress input as a grammatical one —
# what is under test is whether "n't" near the trigger word suppresses the
# match, not English usage.
#
# "after": the negation sits AFTER the anchor phrase the YES-pattern
# matches on — PwC Canada's live shape ("bilingual in French, however this
# ISN'T A REQUIREMENT") and its cover_letter/transcript mirror ("a cover
# letter ISN'T REQUIRED").
# "before": the negation sits ENTIRELY AHEAD of the anchor phrase — "we do
# NOT REQUIRE a transcript".
_CASES = {
    "cover_letter": (
        extract_cover_letter,
        "Please submit a cover letter with your application.",
        "A cover letter {neg} required for this application.",
        "We do {neg} require a cover letter for this role.",
    ),
    "transcript": (
        extract_transcript,
        "Please upload an unofficial transcript with your application.",
        "A transcript {neg} required for this application.",
        "We do {neg} require a transcript for this role.",
    ),
    "language": (
        extract_languages,
        "Fluency in Mandarin is required for this role.",
        "Fluency in Mandarin — this {neg} a requirement for this role.",
        None,  # see BEFORE_CASES below: not a shape this extractor's
               # anchor (the language NAME) can plausibly sit ahead of.
    ),
}

# The "before" shape — "we do NOT REQUIRE a transcript", negation entirely
# ahead of the anchor phrase — only applies to `cover_letter`/`transcript`.
# `extract_languages` anchors its match on the LANGUAGE NAME itself
# (`_LANG_REQ` requires a keyword like "fluent"/"speak" immediately before
# it, or "required" immediately after), so there is no realistic posting
# shape where a negation sits entirely ahead of "Mandarin" and still within
# the extractor's own 20-character lookback — that geometry does not exist
# for this extractor the way it does for the other two, which admit
# "require ... <noun>" in either order.
BEFORE_CASES = {k: v for k, v in _CASES.items() if v[3] is not None}


@pytest.mark.parametrize("kind", sorted(_CASES))
def test_a_real_requirement_still_fires(kind):
    """Invariant 3, the control: before any negation is layered in, the
    plain positive sentence for each extractor must produce a chip. If this
    fails, every negated case below is meaningless (the extractor never
    matched anything to suppress)."""
    fn, positive, _, _ = _CASES[kind]
    assert fn(positive) is not None, f"{kind} did not fire on its own positive control"


@pytest.mark.parametrize("neg", NEGATIONS)
@pytest.mark.parametrize("kind", sorted(_CASES))
def test_negation_after_the_anchor_suppresses_every_extractor(kind, neg):
    """Invariant 1 + 2 (after-the-anchor shape): "a transcript ISN'T
    required", "however this ISN'T A REQUIREMENT" — the negation sits
    between (or right after) the anchor phrase and the requirement word,
    the confirmed-live PwC Canada shape."""
    fn, _, after_template, _ = _CASES[kind]
    sentence = after_template.format(neg=neg)
    assert fn(sentence) is None, sentence


@pytest.mark.parametrize("neg", NEGATIONS)
@pytest.mark.parametrize("kind", sorted(BEFORE_CASES))
def test_negation_ahead_of_the_anchor_suppresses_cover_letter_and_transcript(kind, neg):
    """Invariant 1 + 2 (ahead-of-anchor shape): "we do NOT REQUIRE a
    transcript" — the negation sits entirely before the anchor phrase.
    See `BEFORE_CASES` for why `language` is not part of this
    cross-product."""
    fn, _, _, before_template = BEFORE_CASES[kind]
    sentence = before_template.format(neg=neg)
    assert fn(sentence) is None, sentence


# ---------------------------------------------------------------------------
# 4. Dual-scale GPA statements resolve to the 4.0-scale reading regardless
#    of which fraction the posting states first.
# ---------------------------------------------------------------------------

# (first fraction, second fraction) pairs a posting might state, and the
# 4.0-scale value either order must resolve to. Every pair states the SAME
# underlying bar on two different grading scales — the live HSBC shape
# (5.0-scale first) and its mirror (4.0-scale first), plus a 4.3-scale
# variant since some schools grade on that denominator instead of 5.0.
DUAL_SCALE_PAIRS = (
    ("4.0/5.0", "3.2/4.0", "3.2"),
    ("3.2/4.0", "4.0/5.0", "3.2"),
    ("4.3/5.0", "3.4/4.0", "3.4"),
    ("3.4/4.0", "4.3/5.0", "3.4"),
)


@pytest.mark.parametrize("first,second,expected", DUAL_SCALE_PAIRS)
def test_dual_scale_gpa_resolves_to_the_4_0_reading_either_order(first, second, expected):
    text = f"Have obtained, or expect to achieve, a minimum GPA of {first} or {second}."
    got = extract_gpa(text)
    assert got is not None, text
    assert got["value"] == expected, text


def test_a_single_fraction_gpa_is_never_treated_as_dual_scale():
    """The dual-scale reading must never fire on the ordinary single-scale
    shape — a denominator with no "or <fraction>" continuation is just a
    scale statement, not a disjunction to resolve."""
    for denom in ("4.0", "4.3", "5.0"):
        got = extract_gpa(f"Minimum cumulative GPA of 3.0/{denom} required.")
        assert got is not None
        assert got["value"] == "3.0"
