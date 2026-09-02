"""WS-CRM-14 (part) — the pre-send mismatch rule.

"I get 10+ resumes a season with the wrong bank or wrong name (or both)", and
wrong-name or wrong-bank "narrows the field down way more than it should"
(`research-outreach-mechanics.md §5b`, Grade A). §9.3 of the same file is the
product consequence: a merge-field drafting system without this guard is a
machine for producing that error class at scale.

The rule is pure and lives in `assistant/mismatch.py` so that
`drafts.flag_reason` and the chat page's JavaScript mirror can hold to the
same fixture set — the existing index-pairing requirement means the two
implementations must agree segment for segment or a card's log-touch chip
lands on the wrong person.

NOT YET WIRED into `drafts.split`: see the run report. These cases pin the
rule so the wiring is a call and not a rewrite.
"""

from __future__ import annotations

import pytest

from assistant.mismatch import mismatch_reason

CITI = {"name": "Dana Reed", "firm": "Citi"}


def test_a_draft_naming_another_bank_is_a_mismatch():
    reason = mismatch_reason(
        "Quick question",
        "Hi Dana,\n\nI have been following Goldman Sachs's healthcare team...",
        CITI,
    )
    assert reason is not None
    assert "Goldman Sachs" in reason
    assert "Dana Reed" in reason
    assert "Citi" in reason


def test_a_correct_draft_is_unaffected():
    assert mismatch_reason(
        "Quick question",
        "Hi Dana,\n\nI have been following Citi's healthcare team...",
        CITI,
    ) is None


def test_the_recipients_own_firm_never_convicts_under_another_spelling():
    """The firm arrives as free text, so "JPMorgan Chase", "J.P. Morgan" and
    "jpm" all have to subtract the same canonical name."""
    for stored in ("J.P. Morgan", "JPMorgan Chase & Co.", "JPM"):
        assert mismatch_reason(
            "Hello", "Hi Dana,\n\nJPMorgan's coverage team...",
            {"name": "Dana Reed", "firm": stored},
        ) is None


def test_the_greeting_name_must_match_the_recipient():
    reason = mismatch_reason(
        "Quick question", "Hi Priya,\n\nThanks for your time.", CITI)
    assert reason is not None
    assert "Priya" in reason
    assert "Dana Reed" in reason


def test_a_first_name_greeting_clears_the_check():
    assert mismatch_reason(
        "Quick question", "Hi Dana,\n\nThanks for your time.", CITI) is None


def test_a_surname_greeting_clears_the_check():
    assert mismatch_reason(
        "Quick question", "Dear Ms. Reed,\n\nThanks for your time.",
        CITI) is None


def test_a_third_party_named_in_the_body_is_not_a_mismatch():
    """Only the greeting is checked. A body legitimately names other people,
    and convicting on that would demote exactly the drafts with a real
    referral in them."""
    assert mismatch_reason(
        "Intro from Priya",
        "Hi Dana,\n\nPriya Nair suggested I reach out to you.",
        CITI,
    ) is None


def test_the_subject_line_is_checked_too():
    reason = mismatch_reason(
        "Goldman Sachs summer analyst question",
        "Hi Dana,\n\nThanks for your time.",
        CITI,
    )
    assert reason is not None
    assert "Goldman Sachs" in reason


def test_no_recipient_means_no_check():
    """P3: every existing caller passes nothing and gets exactly today's
    behaviour. Inventing a recipient to compare against would be worse than
    not checking (P1)."""
    assert mismatch_reason("Hi", "Hi Priya, about Goldman Sachs...", None) is None
    assert mismatch_reason("Hi", "Hi Priya, about Goldman Sachs...", {}) is None


@pytest.mark.parametrize("body", [
    "Hi Dana,\n\nThings are going well.",
    "Hi Dana,\n\nI am open to any group.",
    "Hi Dana,\n\nBainbridge Island is where I grew up.",
])
def test_a_firm_alias_inside_an_ordinary_word_never_convicts(body):
    """`\\b` on both sides is what keeps "gs" out of "things", "bain" out of
    "Bainbridge" and "ubs" out of "suburbs". A false positive costs the Copy
    button on a correct draft, which is the expensive direction because it is
    invisible."""
    assert mismatch_reason("Quick question", body, CITI) is None


def test_a_recipient_firm_the_table_does_not_know_still_convicts_on_others():
    """Subtracting nothing is the safe direction: the draft is convicted only
    if it names some OTHER firm outright."""
    reason = mismatch_reason(
        "Hello", "Hi Dana,\n\nGoldman Sachs's team...",
        {"name": "Dana Reed", "firm": "Some Boutique LLP"},
    )
    assert reason is not None
    assert "Goldman Sachs" in reason
