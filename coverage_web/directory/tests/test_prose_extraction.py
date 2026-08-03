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
