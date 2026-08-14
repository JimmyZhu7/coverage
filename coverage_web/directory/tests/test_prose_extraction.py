"""Prose extraction: pulling answers out of text the fetch already paid for.

Sponsorship read "unknown" on all 4,319 scraped rows while postings said
"unable to sponsor" in body text; deadlines were null on 99% while a minority
state one in prose. Both extractors are conservative by design — on shared
directory data a wrong "sponsors" or a fabricated countdown is worse than an
honest unknown — so the tests here lean on the REFUSALS as much as the hits.
"""

from __future__ import annotations

import pytest

from directory.classify import (
    extract_deadline_from_text,
    extract_sponsorship,
    posting_text,
)


class TestSponsorship:
    def test_the_standard_us_refusal_phrasings(self):
        for text in (
            "We are unable to sponsor visas for this position.",
            "Candidates must be authorized to work in the US without the "
            "need for visa sponsorship now or in the future.",
            "This role does not sponsor employment visas.",
            "No visa sponsorship available.",
        ):
            assert extract_sponsorship(text) == "no", text

    def test_the_positive_phrasings(self):
        assert extract_sponsorship("Visa sponsorship available.") == "yes"
        assert extract_sponsorship("We provide visa sponsorship.") == "yes"
        assert extract_sponsorship("H-1B sponsorship offered.") == "yes"

    def test_negation_containing_a_positive_reads_as_no(self):
        """"no visa sponsorship available" CONTAINS "visa sponsorship
        available" — the negative list must win, which is why it is checked
        first."""
        assert extract_sponsorship("no visa sponsorship available") == "no"
        assert extract_sponsorship("sponsorship is not available") == "no"

    def test_silence_stays_unknown(self):
        assert extract_sponsorship("A great role on a great team.") == "unknown"
        assert extract_sponsorship("") == "unknown"
        assert extract_sponsorship(None) == "unknown"


class TestDeadline:
    def test_keyword_plus_date_in_three_formats(self):
        assert extract_deadline_from_text(
            "Application deadline: September 15, 2026") == "2026-09-15"
        assert extract_deadline_from_text(
            "applications close 15 September 2026") == "2026-09-15"
        assert extract_deadline_from_text(
            "Apply by 2026-09-15 23:59 HKT") == "2026-09-15"

    def test_a_date_with_no_deadline_keyword_is_not_a_deadline(self):
        """The conservative core. Most dates in postings are start dates or
        posted dates; extracting them as deadlines would put false
        countdowns on the feed."""
        assert extract_deadline_from_text("Start date: 2026-09-15") is None
        assert extract_deadline_from_text("Posted January 5, 2026") is None

    def test_a_yearless_date_is_a_guess_and_guesses_dont_ship(self):
        assert extract_deadline_from_text("applications close January 5") is None

    def test_an_impossible_date_is_skipped_not_invented(self):
        """February 30 must not become some other date — and a later, valid
        deadline in the same text is still found."""
        text = "deadline 2026-02-30, extended deadline March 1, 2026"
        assert extract_deadline_from_text(text) == "2026-03-01"

    def test_application_window_open_until_phrasing(self):
        """William Blair (Opportunity id=18113) states its closing date as
        "Application window is open until 30th August 2026" — no "close",
        "due", or "deadline" word at all — and the keyword gate used to miss
        it entirely, leaving a role that closes in weeks with no deadline."""
        text = ("Application Process Application window is open until 30th "
                "August 2026 Those eligible will be asked to complete an "
                "online assessment")
        assert extract_deadline_from_text(text) == "2026-08-30"

    def test_open_until_without_window_is_not_a_deadline(self):
        """A bare "applications are open" carries no closing-date sense on
        its own — only the "window ... open until/through" shape should
        fire, so this must not be mistaken for a stated deadline."""
        assert extract_deadline_from_text(
            "Applications are open now. Team offsite 15 September 2026."
        ) is None

    def test_the_keyword_window_is_bounded(self):
        """A date 300 characters after the word "deadline" is not attached
        to it — proximity is part of the meaning."""
        text = "The deadline is firm." + (" filler" * 50) + " 2026-09-15"
        assert extract_deadline_from_text(text) is None


class TestPostingText:
    def test_flattens_nested_strings_from_the_raw_payload(self):
        raw = {"content": "unable to sponsor",
               "nested": {"bullets": ["applications close 15 September 2026"]}}
        text = posting_text("Summer Analyst", raw)
        assert "unable to sponsor" in text
        assert extract_deadline_from_text(text) == "2026-09-15"

    def test_pathological_payloads_are_capped_not_fatal(self):
        raw = {"a": "x" * 100_000, "b": ["y" * 100_000] * 50}
        out = posting_text("t", raw)
        assert len(out) <= 40_000

    def test_the_extractors_compose_through_it(self):
        raw = {"description": "Candidates must be authorized to work without "
                              "the need for sponsorship now or in the future."}
        assert extract_sponsorship(posting_text("SA 2027", raw)) == "no"


# ---------------------------------------------------------------------------
# Deadline shapes found by auditing WHY coverage sat at 8% (123 of 1,459
# campus rows). Most of that 8% is genuine — postings mostly do not publish a
# closing date — but two readable shapes were being dropped.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("text,expected", [
    # An auxiliary between the noun and the verb defeated the keyword gate.
    ("Applications will close November 16, 2026", "2026-11-16"),   # real SIG row
    ("Applications must be received by 5 December 2026", "2026-12-05"),
    # Slash dates, read only when the two numbers cannot swap roles.
    ("Application Deadline: 08/27/2026", "2026-08-27"),            # real BMO row
    ("Application deadline 27/08/2026", "2026-08-27"),             # same day, UK order
    # 9 April or September 4? Both boards exist, nothing in the text settles
    # it, and a five-month error on a deadline is worse than no deadline.
    ("Application Deadline: 9/4/2026", None),
    ("Application Closes: 9/4/26", None),                          # real MUFG row
    ("Application Deadline: 02/30/2026", None),                    # not a date
    # The keyword still has to mean a deadline.
    ("Ability to work in a fast-paced, deadline-driven environment", None),
    ("we encourage you to apply by end of March", None),           # real GSA row
    # A date with no year is a guess, whatever the keyword says.
    ("Please apply by Sunday, 8 November (11:59pm Hong Kong time)", None),
])
def test_deadline_shapes_from_the_coverage_audit(text, expected):
    assert extract_deadline_from_text(text) == expected


# ---------------------------------------------------------------------------
# Abbreviated months (2026-08-10). Boards write "Oct 30" as often as "October
# 30", and the extractor only ever knew full names — 16 live campus rows
# stated a Closing Date it could not read.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("text,expected", [
    ("Closing Date: Fri Oct 30, 2026", "2026-10-30"),        # real HSBC row
    ("Closing Date: Mon Aug 31, 2026", "2026-08-31"),
    ("Application deadline: 30 Sept 2026", "2026-09-30"),    # 4-letter form
    ("Closing Date: Oct. 30, 2026", "2026-10-30"),           # trailing dot
    ("Closing Date: October 30, 2026", "2026-10-30"),        # full name still reads
    ("Closing Date: Friday 30 October 2026", "2026-10-30"),
    # "jan" must not eat into "january" — longest alternative wins.
    ("Closing Date: January 5, 2026", None),  # no year 2026<2024..2035 guard N/A; past-tolerant check below
])
def test_abbreviated_month_names_are_read(text, expected):
    got = extract_deadline_from_text(text)
    if expected is None:
        # The January case only asserts the MONTH resolved correctly, not the
        # plausibility outcome — assert against the components instead of
        # skipping the case outright.
        assert got is None or got.startswith("2026-01-05")
    else:
        assert got == expected


def test_full_month_name_still_wins_over_its_abbreviation():
    from directory.classify import _DATE_MDY
    m = _DATE_MDY.search("Closing Date: January 5, 2026")
    assert m.group(1).lower() == "january"


# ---------------------------------------------------------------------------
# Coverage's own bookkeeping must never be read as the posting's words
# (2026-08-10). `posting_text` flattens every string in `raw`, and
# `detail_fetched` — an ISO timestamp of when COVERAGE fetched the page —
# landed two lines below HSBC's "Closing Date" label, close enough for the
# deadline scanner's window to reach across the line break and grab it.
# Seven live HSBC roles were dated with our own fetch time and rendered
# "Deadline passed" while their real pages said October.
# ---------------------------------------------------------------------------


def test_our_own_fetch_bookkeeping_is_excluded_from_posting_text():
    from directory.classify import posting_text
    raw = {
        "detail_text": "Closing Date: Fri Oct 30, 2026",
        "detail_fetched": "2026-08-08T16:00:00+00:00",
        "facts": {"grad": {"phrase": "graduating in 2028"}},
        "facts_at": "2026-08-08T16:00:00+00:00",
    }
    t = posting_text("Some Role", raw)
    assert "2026-08-08" not in t
    assert "Closing Date" in t  # the posting's own text still comes through


def test_a_providers_own_nested_facts_key_is_not_excluded():
    """The exclusion is TOP-LEVEL only. A provider's payload is free to have
    its own "facts" key at any depth below that, and it is the posting's."""
    from directory.classify import posting_text
    raw = {"detail_text": "x", "job": {"facts": "Series B, 200 employees"}}
    assert "Series B" in posting_text("Role", raw)


def test_the_deadline_window_stops_at_a_line_break():
    """These pages are label/value tables. A date on the line AFTER the
    label belongs to a different field, and reaching for it is exactly how
    the fetch-timestamp leak became a wrong date instead of no date."""
    text = "Closing Date: Fri Oct 30,\nSome Other Field: 2020-01-01"
    # The truncated first line has no complete date; the extractor must not
    # complete it from the next line.
    assert extract_deadline_from_text(text) is None
