"""Adversarial invariant suite for the GROUNDING RULE — the one contract
every single-fact extractor in `directory/ai_extract.py` is built on.

`test_ai_extract.py` next door is example-based: it pins one grounded answer
and one ungrounded answer per extractor and documents what the module is
FOR. This file asks the other question — what model answer gets through that
should not — and it asks it of ALL FOUR grounded extractors at once, because
the failure that matters is not "the deadline pass has a hole", it is "one of
the four has a hole and the other three do not, and nobody noticed which".

Same discipline as `coverage_domain/tests/test_stress_invariants.py` and
`crm/tests/test_stress_crm.py`: NO `hypothesis`. The interesting space here
is a small enumerated cross-product — 4 extractors x ~20 hostile model
answers — walked EXHAUSTIVELY.

THE INVARIANTS

  1. THE FOUR EXTRACTORS ARE ONE RULE, NOT FOUR. Every grounded extractor
     must reject the same ungrounded answer. Parametrized over all four so a
     fifth added later either joins the parametrization or is visibly absent
     from it.

  2. A GROUNDED QUOTE IS A QUOTE, NOT A COINCIDENCE. A verbatim-substring
     check alone cannot tell a sentence from a fragment: "." and a bare year
     are substrings of almost every posting. The floor (`_MIN_QUOTE_CHARS`)
     is what stops a fabricated fact riding in on a real fragment.

  3. THE VALUE IS THE TYPE IT CLAIMS TO BE. `DeadlineGuess.value` is
     documented as an ISO date and assigned straight to a `DateField` by its
     caller. A date-SHAPED string that names no real day ("2026-02-30") is
     not an ISO date, and the module docstring already promises an
     "unparseable date" resolves to no answer.

  4. NO ANSWER IS ALWAYS AN OPTION. Unconfigured, empty input, malformed
     JSON, a wrong enum, a missing quote, an HTTP failure — every one of them
     is `None`, never a partial answer and never an exception out of a sync
     that has to keep going.
"""

from __future__ import annotations

import json

import pytest
from django.test import override_settings

from directory import ai_extract

KEY = "sk-test-key"

# The source text every extractor below is shown. Deliberately contains a
# bare year and a stray full stop, so a fragment-quoting model has something
# real to "ground" against.
SOURCE = (
    "2027 Summer Analyst Programme, Hong Kong. Applications must be received "
    "by October 30, 2026 at 11:59pm HKT. We do not sponsor employment visas "
    "for this position. Thank you for your interest."
)


def _text_response(text: str) -> dict:
    return {"content": [{"type": "text", "text": text}]}


def _reply(monkeypatch, payload):
    """Make the next API call return `payload` (a dict, or a raw string)."""
    body = payload if isinstance(payload, str) else json.dumps(payload)
    monkeypatch.setattr(
        ai_extract, "_post_json", lambda *a, **kw: _text_response(body)
    )


# One row per grounded extractor: (callable, the args it takes, the key its
# answer lives under, a value that key accepts). Adding a fifth extractor to
# the module without adding it here is the thing this table exists to make
# obvious.
EXTRACTORS = [
    ("deadline", lambda: ai_extract.extract_deadline_ai(SOURCE),
     "deadline_iso", "2026-10-30"),
    ("sponsorship", lambda: ai_extract.extract_sponsorship_ai(SOURCE),
     "sponsorship", "no"),
    ("application_event", lambda: ai_extract.extract_application_event_ai(SOURCE),
     "event", "rejected"),
    ("mail_fact", lambda: ai_extract.extract_mail_fact_ai(SOURCE),
     "fact", "out_of_office"),
]

EXTRACTOR_IDS = [name for name, *_ in EXTRACTORS]


def test_every_grounded_extractor_in_the_module_is_in_the_table():
    """The parametrization below is only as good as its coverage. A new
    `extract_*_ai` that returns a `*Guess` must join it."""
    grounded = {
        name for name in dir(ai_extract)
        if name.startswith("extract_") and name.endswith("_ai")
    }
    covered = {
        "extract_deadline_ai", "extract_sponsorship_ai",
        "extract_application_event_ai", "extract_mail_fact_ai",
    }
    assert grounded == covered, (
        "a grounded extractor was added or renamed; add it to EXTRACTORS"
    )


# ===========================================================================
# INVARIANT 1 — one rule, four extractors. The same hostile answer must be
# rejected by every one of them.
# ===========================================================================
UNGROUNDED_QUOTES = [
    "Applications close on the 30th of October.",   # paraphrase of a real line
    "The deadline is November 3rd.",                # invented outright
    "Applications  must  be received by October 30, 2026 at 11:59pm HKT!",  # typo "fixed"
    "APPLICATIONS MUST BE RECEIVED BY OCTOBER 30, 2026",  # case-folded
    "…must be received by October 30, 2026…",       # ellipsis-decorated
    "",
    None,
    "   ",
]


@pytest.mark.parametrize("name,call,key,value", EXTRACTORS, ids=EXTRACTOR_IDS)
@pytest.mark.parametrize("quote", UNGROUNDED_QUOTES)
@override_settings(ANTHROPIC_API_KEY=KEY)
def test_no_extractor_trusts_an_ungrounded_quote(monkeypatch, name, call, key, value, quote):
    _reply(monkeypatch, {key: value, "quote": quote})
    assert call() is None, f"{name} accepted {quote!r}"


@pytest.mark.parametrize("name,call,key,value", EXTRACTORS, ids=EXTRACTOR_IDS)
@override_settings(ANTHROPIC_API_KEY=KEY)
def test_every_extractor_accepts_a_verbatim_quote(monkeypatch, name, call, key, value):
    """The gate has to let a real answer through, or the tests above prove
    only that the module is broken."""
    _reply(monkeypatch, {
        key: value,
        "quote": "Applications must be received by October 30, 2026 at 11:59pm HKT.",
    })
    guess = call()
    assert guess is not None, name
    assert guess.value == value
    assert guess.confidence == 0.5
    assert guess.phrase in SOURCE


@pytest.mark.parametrize("name,call,key,value", EXTRACTORS, ids=EXTRACTOR_IDS)
@override_settings(ANTHROPIC_API_KEY=KEY)
def test_reflowed_whitespace_is_accepted(
    monkeypatch, name, call, key, value
):
    """A model that copies a line but collapses the source's own line breaks
    is still quoting it."""
    _reply(monkeypatch, {
        key: value,
        "quote": "Applications must be received\n  by October 30, 2026\tat 11:59pm HKT.",
    })
    assert call() is not None, name


# ===========================================================================
# INVARIANT — a model that straightens the SOURCE's own typography is still
# grounded. Whitespace reflow and typographic-mark normalization (curly
# quotes, en/em dashes spelled as one character or as an ASCII "--"/"---"
# run, the single ellipsis character) are the only normalizations allowed;
# an actual paraphrase or a fabricated sentence still must not match.
#
# THE HOLE THIS CLOSES. `Opportunity.raw["detail_text"]` is real scraped
# HTML, decoded once at fetch time -- a curly apostrophe and an em dash are
# exactly what a posting's own copy looks like after that decode, never
# authored in plain ASCII. A model told to quote a sentence "verbatim"
# reliably straightens that punctuation back to ASCII while copying the
# words, their order, and their meaning exactly -- and the bare
# whitespace-only substring check saw the changed character and rejected a
# TRUE, correctly-cited answer the same as it would an invented one. Found
# live: `_grounded("We're looking for driven students to join our Summer
# Analyst Program.", source_with_a_curly_apostrophe)` returned `False`.
# ===========================================================================
SOURCE_TYPOGRAPHIC = (
    "2027 Summer Analyst Programme, Hong Kong. We’re looking for driven "
    "students — applications must be received by October 30, 2026 at "
    "11:59pm HKT. We do not sponsor employment visas for this position."
)

EXTRACTORS_TYPO = [
    ("deadline", lambda: ai_extract.extract_deadline_ai(SOURCE_TYPOGRAPHIC),
     "deadline_iso", "2026-10-30"),
    ("sponsorship", lambda: ai_extract.extract_sponsorship_ai(SOURCE_TYPOGRAPHIC),
     "sponsorship", "no"),
    ("application_event", lambda: ai_extract.extract_application_event_ai(SOURCE_TYPOGRAPHIC),
     "event", "rejected"),
    ("mail_fact", lambda: ai_extract.extract_mail_fact_ai(SOURCE_TYPOGRAPHIC),
     "fact", "out_of_office"),
]


@pytest.mark.parametrize("name,call,key,value", EXTRACTORS_TYPO, ids=EXTRACTOR_IDS)
@override_settings(ANTHROPIC_API_KEY=KEY)
def test_a_model_that_straightens_the_sources_own_typography_is_still_grounded(
    monkeypatch, name, call, key, value
):
    """Same words, same order, same meaning as `SOURCE_TYPOGRAPHIC`'s own
    sentence -- only the apostrophe and the dash were straightened to plain
    ASCII, exactly what a real model does when asked to copy real scraped
    text verbatim. This is a citation, not a paraphrase, and must be
    accepted."""
    _reply(monkeypatch, {
        key: value,
        "quote": (
            "We're looking for driven students -- applications must be "
            "received by October 30, 2026 at 11:59pm HKT."
        ),
    })
    guess = call()
    assert guess is not None, (
        f"{name} rejected a quote that only straightened the source's own "
        "typography back to ASCII"
    )
    assert guess.value == value


@override_settings(ANTHROPIC_API_KEY=KEY)
def test_a_paraphrase_is_still_rejected_even_in_typographic_ascii(monkeypatch):
    """The fix only widens what counts as the SAME mark -- it must never let
    an actual paraphrase or a fabricated sentence through, typographic
    dressing or not."""
    _reply(monkeypatch, {
        "deadline_iso": "2026-10-30",
        "quote": "The programme's deadline is around the end of October -- don't miss it.",
    })
    assert ai_extract.extract_deadline_ai(SOURCE_TYPOGRAPHIC) is None


def test_typographic_punctuation_normalizes_to_the_same_mark():
    """Direct coverage of `_grounded`'s own contract: curly quotes, an em
    dash spelled as one character or as an ASCII "--"/"---" run, and the
    ellipsis character all compare equal to their plain-ASCII spelling, in
    either direction."""
    source = "We’re open — read the “full” brief… today."
    assert ai_extract._grounded("We're open -- read the \"full\" brief... today.", source)
    assert ai_extract._grounded("We're open - read the \"full\" brief... today.", source)
    assert ai_extract._grounded("We're open --- read the \"full\" brief... today.", source)
    # And the reverse direction: an ASCII quote is still grounded against a
    # source that itself uses the Unicode marks.
    ascii_source = "We're open -- read the \"full\" brief... today."
    assert ai_extract._grounded("We’re open — read the “full” brief… today.", ascii_source)


def test_typographic_normalization_cannot_shrink_a_quote_under_the_floor():
    """Canonicalization only ever WIDENS a character (the ellipsis character
    becomes three periods) -- it must never let a quote that reads as a bare
    fragment after normalization sneak under `_MIN_QUOTE_CHARS`."""
    assert not ai_extract._grounded("…", "See the posting… for details.")
    assert not ai_extract._grounded("--", "Roles open now -- apply today.")


def test_mailfacts_grounded_agrees_with_ai_extract_grounded_on_typography():
    """`capture.mailfacts._detect_ai` re-verifies, over the identical text, a
    quote `extract_mail_fact_ai` already accepted -- through its OWN
    `_grounded`, a separate function. If the two didn't normalize
    punctuation identically, a quote the AI layer just verified as grounded
    could be thrown away one call later for a reason that has nothing to do
    with whether it is a real citation. See `capture.mailfacts._grounded`'s
    own docstring."""
    from capture import mailfacts

    source = "Priya is no longer with the firm — please contact Dan instead."
    quote = "Priya is no longer with the firm -- please contact Dan instead."
    assert ai_extract._grounded(quote, source)
    assert mailfacts._grounded(quote, source)


# ===========================================================================
# INVARIANT 2 — a grounded quote is a quote, not a coincidence.
#
# THE HOLE THIS CLOSES. `_grounded` was a bare substring test, so a model
# answering {"deadline_iso": "2027-01-15", "quote": "2027"} cleared it on a
# page whose only "2027" is the programme's NAME — a fabricated deadline
# riding in on a real fragment, which is precisely what the grounding rule
# exists to make impossible.
# ===========================================================================
FRAGMENTS = [".", " ", "2027", "by", "not", "HKT", "Thank", "11:59pm", "October"]


@pytest.mark.parametrize("name,call,key,value", EXTRACTORS, ids=EXTRACTOR_IDS)
@pytest.mark.parametrize("fragment", FRAGMENTS)
@override_settings(ANTHROPIC_API_KEY=KEY)
def test_a_fragment_is_not_a_citation_however_verbatim_it_is(
    monkeypatch, name, call, key, value, fragment
):
    assert fragment in SOURCE, "the fragment must genuinely be in the source"
    _reply(monkeypatch, {key: value, "quote": fragment})
    assert call() is None, f"{name} accepted the fragment {fragment!r}"


def test_the_quote_floor_sits_below_the_shortest_real_deadline_sentence():
    """The floor only tightens the gate. It must not be so high that a
    genuinely short but real citation is thrown away."""
    assert ai_extract._grounded("Closes 30/10/2026", "Closes 30/10/2026 — apply now")
    assert ai_extract._MIN_QUOTE_CHARS <= len("Closes 30/10/2026")


def test_grounded_is_total_over_junk():
    for quote, source in [
        (None, SOURCE), ("", SOURCE), ("x", ""), (SOURCE, ""),
        ("a" * 5000, SOURCE), (SOURCE, SOURCE),
        # A quote that is not a STRING. `re.sub` raises `TypeError` on these,
        # and a non-empty list clears the falsiness check.
        (["a sentence", "another"], SOURCE), (42, SOURCE), (True, SOURCE),
        ({"quote": "x"}, SOURCE), (["x"], SOURCE),
    ]:
        assert isinstance(ai_extract._grounded(quote, source), bool)


@pytest.mark.parametrize("name,call,key,value", EXTRACTORS, ids=EXTRACTOR_IDS)
@pytest.mark.parametrize("quote", [
    ["Applications must be received by October 30, 2026 at 11:59pm HKT."],
    42, True, {"text": "Applications must be received by October 30, 2026."},
    ["a", "b"],
])
@override_settings(ANTHROPIC_API_KEY=KEY)
def test_a_quote_that_is_not_a_string_is_no_answer(monkeypatch, name, call, key, value, quote):
    """JSON has more types than the prompt asks for. A list wrapping the
    right sentence is the realistic one — and it is truthy, so it reaches the
    grounding check and used to raise a `TypeError` out of it."""
    _reply(monkeypatch, {key: value, "quote": quote})
    assert call() is None, (name, quote)


# ===========================================================================
# INVARIANT 3 — `DeadlineGuess.value` is a real calendar date.
#
# The regex `20\d{2}-\d{2}-\d{2}` is a SHAPE check. Its caller assigns the
# result to a `DateField`; a date-shaped non-date raises on save, and one bad
# answer used to take down the rest of a paid batch run partway through.
# ===========================================================================
IMPOSSIBLE_DATES = [
    "2026-02-30",   # February never has 30 days
    "2026-13-01",   # there is no month 13
    "2026-00-10",   # nor a month 0
    "2026-10-00",   # nor a day 0
    "2026-11-31",   # November has 30
    "2027-02-29",   # 2027 is not a leap year
    "2026-99-99",
]


@pytest.mark.parametrize("value", IMPOSSIBLE_DATES)
@override_settings(ANTHROPIC_API_KEY=KEY)
def test_a_date_shaped_non_date_is_no_answer(monkeypatch, value):
    _reply(monkeypatch, {
        "deadline_iso": value,
        "quote": "Applications must be received by October 30, 2026 at 11:59pm HKT.",
    })
    assert ai_extract.extract_deadline_ai(SOURCE) is None, value


@pytest.mark.parametrize("value", [
    "30/10/2026", "October 30, 2026", "2026-10-30T00:00:00", "1926-10-30",
    "0026-10-30", "26-10-30", "2026-10-3", " 2026-10-30", "2026-10-30 ",
    True, 20261030, None, "", [], {},
])
@override_settings(ANTHROPIC_API_KEY=KEY)
def test_only_a_bare_iso_day_in_this_century_is_accepted(monkeypatch, value):
    _reply(monkeypatch, {
        "deadline_iso": value,
        "quote": "Applications must be received by October 30, 2026 at 11:59pm HKT.",
    })
    assert ai_extract.extract_deadline_ai(SOURCE) is None, value


@override_settings(ANTHROPIC_API_KEY=KEY)
def test_the_accepted_value_round_trips_through_the_date_constructor(monkeypatch):
    """The promise the dataclass makes, stated as the thing its caller does."""
    from datetime import date

    _reply(monkeypatch, {
        "deadline_iso": "2026-10-30",
        "quote": "Applications must be received by October 30, 2026 at 11:59pm HKT.",
    })
    guess = ai_extract.extract_deadline_ai(SOURCE)
    assert date.fromisoformat(guess.value) == date(2026, 10, 30)


# ===========================================================================
# INVARIANT 4 — no answer is always an option, and never an exception.
# ===========================================================================
MALFORMED_REPLIES = [
    "",
    "   ",
    "not json at all",
    "{",
    "null",
    "[]",
    '"a string"',
    "42",
    '{"deadline_iso": "2026-10-30"}',                 # no quote key
    '{"quote": "Applications must be received by October 30, 2026 at 11:59pm HKT."}',
    '{"deadline_iso": null, "quote": null}',
    '{"sponsorship": "maybe", "quote": "We do not sponsor employment visas for this position."}',
    '{"event": "ghosted", "quote": "Thank you for your interest."}',
    '{"fact": "promoted", "quote": "Thank you for your interest."}',
]


@pytest.mark.parametrize("name,call,key,value", EXTRACTORS, ids=EXTRACTOR_IDS)
@pytest.mark.parametrize("raw", MALFORMED_REPLIES)
@override_settings(ANTHROPIC_API_KEY=KEY)
def test_a_malformed_reply_is_no_answer_not_a_crash(monkeypatch, name, call, key, value, raw):
    _reply(monkeypatch, raw)
    assert call() is None, (name, raw)


@pytest.mark.parametrize("name,call,key,value", EXTRACTORS, ids=EXTRACTOR_IDS)
@override_settings(ANTHROPIC_API_KEY=KEY)
def test_a_code_fenced_reply_is_still_read(monkeypatch, name, call, key, value):
    """Models wrap JSON in a fence despite the instruction. Unwrapping it is
    not loosening the gate — the grounding check still runs on what's inside."""
    body = json.dumps({
        key: value,
        "quote": "Applications must be received by October 30, 2026 at 11:59pm HKT.",
    })
    _reply(monkeypatch, f"```json\n{body}\n```")
    assert call() is not None, name


@pytest.mark.parametrize("name,call,key,value", EXTRACTORS, ids=EXTRACTOR_IDS)
@override_settings(ANTHROPIC_API_KEY="")
def test_nothing_reaches_the_network_without_a_key(monkeypatch, name, call, key, value):
    calls = []
    monkeypatch.setattr(ai_extract, "_post_json", lambda *a, **kw: calls.append(1))
    assert call() is None, name
    assert calls == [], name


@pytest.mark.parametrize("name,call,key,value", EXTRACTORS, ids=EXTRACTOR_IDS)
@override_settings(ANTHROPIC_API_KEY=KEY)
def test_an_api_failure_never_escapes_a_sync(monkeypatch, name, call, key, value):
    """The two mail-side extractors run inside a mailbox sync whose job is to
    keep going, and swallow the error themselves. The two board-side ones are
    run by a management command that catches `AIExtractError` per row. Either
    way, one failed call must not be able to take down a batch."""
    def boom(*a, **kw):
        raise ai_extract.AIExtractError(RuntimeError("upstream 503"))

    monkeypatch.setattr(ai_extract, "_post_json", boom)
    if name in ("deadline", "sponsorship"):
        with pytest.raises(ai_extract.AIExtractError):
            call()
    else:
        assert call() is None


@pytest.mark.parametrize("name,call,key,value", EXTRACTORS, ids=EXTRACTOR_IDS)
@override_settings(ANTHROPIC_API_KEY=KEY)
def test_empty_input_costs_nothing(monkeypatch, name, call, key, value):
    calls = []
    monkeypatch.setattr(ai_extract, "_post_json", lambda *a, **kw: calls.append(1))
    assert ai_extract.extract_deadline_ai("") is None
    assert ai_extract.extract_deadline_ai(None) is None
    assert ai_extract.extract_sponsorship_ai("   ") is None
    assert ai_extract.extract_application_event_ai("", "") is None
    assert ai_extract.extract_mail_fact_ai(None, None) is None
    assert calls == []


# ===========================================================================
# INVARIANT 5 — the input the model is shown is bounded, and the quote is
# checked against THAT text, not against the untruncated original. Checking
# against the full text would let a model quote a sentence it was never
# shown — grounding against evidence nobody looked at.
# ===========================================================================
@override_settings(ANTHROPIC_API_KEY=KEY)
def test_a_quote_from_beyond_the_input_cap_is_not_grounded(monkeypatch):
    tail = "Applications must be received by October 30, 2026."
    text = ("x" * ai_extract.MAX_INPUT_CHARS) + " " + tail
    seen = {}

    def capture(payload, **kw):
        seen["prompt"] = payload["messages"][0]["content"]
        return _text_response(json.dumps({"deadline_iso": "2026-10-30", "quote": tail}))

    monkeypatch.setattr(ai_extract, "_post_json", capture)
    assert ai_extract.extract_deadline_ai(text) is None
    assert tail not in seen["prompt"], "the model was shown text it should not have been"


@override_settings(ANTHROPIC_API_KEY=KEY)
def test_complete_text_is_documented_as_ungrounded_and_returns_none_on_failure(monkeypatch):
    """`complete_text` is the prose escape hatch and has NO grounding check by
    design (there is no single quotable fact in a brief). What it does owe its
    callers is the same never-raise contract."""
    def boom(*a, **kw):
        raise ai_extract.AIExtractError(RuntimeError("upstream 503"))

    monkeypatch.setattr(ai_extract, "_post_json", boom)
    assert ai_extract.complete_text("anything") is None
    monkeypatch.setattr(ai_extract, "_post_json", lambda *a, **kw: _text_response(""))
    assert ai_extract.complete_text("anything") is None
    assert ai_extract.complete_text("") is None
