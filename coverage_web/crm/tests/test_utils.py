"""`crm.utils.FIRM_DATE_LABELS` — cross-surface consistency audit, finding D.

Until 2026-09-01 this was a second, hand-typed copy of
`directory.timeline.EVENT_LABELS` — the firm timeline's own vocabulary for
`FirmDate.event_kind`. It covered 4 of the 8 kinds (the rest fell through
five CRM call sites' own `event_kind.replace("_", " ")` fallback) and still
held `"insight_deadline": "Insight deadline"`, the exact sentence-cased-raw-
slug bug `directory.timeline.EVENT_LABELS`'s own docstring records fixing
for the identical key. `FIRM_DATE_LABELS` is now DERIVED from
`EVENT_LABELS`, sentence-cased to match the CRM's own pill/phrase
convention (`ACTION_LABELS` — "Send thank-you", "Keep warm") rather than the
firm timeline's Title Case table header.
"""

from __future__ import annotations

from crm.utils import FIRM_DATE_LABELS
from directory.timeline import EVENT_LABELS


def test_firm_date_labels_covers_every_event_kind_the_timeline_knows():
    """The old map covered 4 of 8 kinds; the other 4 fell through a raw
    `event_kind.replace("_", " ")` fallback at five CRM call sites (an
    `app_deadline` row would have printed "app deadline")."""
    assert set(FIRM_DATE_LABELS) == set(EVENT_LABELS)


def test_firm_date_labels_fixed_the_insight_deadline_regression():
    """The one kind the old map DID cover still disagreed with the firm
    timeline about its own wording — this is the bug the audit found."""
    assert FIRM_DATE_LABELS["insight_deadline"] == "Insight programme deadline"
    assert EVENT_LABELS["insight_deadline"] == "Insight Programme Deadline"
    assert FIRM_DATE_LABELS["insight_deadline"] != "Insight deadline"


def test_firm_date_labels_is_sentence_case_not_title_case():
    """Judgment call: the firm timeline (`directory/views.py`'s
    `_firm_date_row`) renders `EVENT_LABELS` Title Case in a table column.
    Every CRM call site prints this map's value as a short pill or inline
    phrase beside `crm.utils.ACTION_LABELS` ("Send thank-you", "Keep warm")
    on the same card (`templates/crm/_cockpit.html`'s `p.firm_date_label`
    sits next to `a.label`) — sentence case there, so this map keeps
    sentence case rather than adopting the timeline's Title Case."""
    from crm.utils import ACTION_LABELS

    assert FIRM_DATE_LABELS["app_close"] == "Applications close"
    assert FIRM_DATE_LABELS["app_deadline"] == "Application deadline"
    # Same casing shape as the CRM's own action vocabulary: capitalized
    # first word, lowercase the rest.
    assert ACTION_LABELS["maintain"] == "Keep warm"


def test_firm_date_labels_still_reads_as_one_word_for_a_single_word_kind():
    """`_sentence_case` must not lowercase a label that IS just one word
    ("Interviews", "Interviews" -> "Interviews", not an empty/garbled
    string) -- the naive `words[1:]` case has zero elements here."""
    assert FIRM_DATE_LABELS["interview"] == "Interviews"
