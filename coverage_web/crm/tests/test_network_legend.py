"""The Network board's mini-legend — cross-surface consistency audit,
finding E.

`templates/crm/contact_list.html`'s legend hardcoded "Emailed, no reply" /
"Emailed, replied" as lowercase literals while the very same board's section
headings, a scroll below, render the Title Case canonical
"Emailed, No Reply" / "Emailed, Replied" off `crm.views._WARMTH_SECTIONS`
(`.net-group-head` in `contact_list.html`). The legend now reads
`warmth_labels` (`crm.views.contact_list`'s context, built by
`_warmth_labels()`) instead of retyping the words.
"""

from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from django.test import Client

pytestmark = pytest.mark.django_db

NETWORK = "/app/contacts/"


def test_the_legend_reads_the_canonical_title_case_labels():
    user = get_user_model().objects.create_user(
        email="net-legend@example.com", password="x" * 14
    )
    client = Client()
    client.force_login(user)
    html = client.get(NETWORK).content.decode()

    legend = html[html.index('class="net-legend-mini"'):]
    legend = legend[: legend.index("</p>")]

    # The canonical Title Case wording — same as `.net-group-head` renders
    # for the section headings themselves.
    assert "Emailed, No Reply" in legend
    assert "Emailed, Replied" in legend
    # The old hardcoded lowercase literals must be gone.
    assert "Emailed, no reply" not in legend
    assert "Emailed, replied" not in legend


def test_the_key_carries_one_entry_per_mark_a_card_can_wear():
    """The key's job is completeness, not decoration: a mark a card can draw
    and the key cannot explain is an abbreviation with no reading.

    Six entries as of 2026-09-02: the four warmth dots, "SP", and "CG", which
    arrived when the Coverage Gaps strip was deleted and its status moved
    onto the cards. The two swatches are both `.pill` and both `aria-hidden`,
    because the words beside them are the accessible content.
    """
    user = get_user_model().objects.create_user(
        email="net-legend-marks@example.com", password="x" * 14
    )
    client = Client()
    client.force_login(user)
    html = client.get(NETWORK).content.decode()

    legend = html[html.index('class="net-legend-mini"'):]
    legend = legend[: legend.index("</p>")]

    assert legend.count("<span><") == 6, "the key gained or lost an entry"
    assert legend.count("aria-hidden") == 2, (
        "a swatch pill is announcing its own abbreviation to a screen reader "
        "alongside the words that explain it"
    )
    for swatch, words in (("fc-spon", "Sponsors visas"),
                          ("fc-cg", "Coverage gap, nobody warm yet")):
        assert f'class="pill {swatch}"' in legend and words in legend, (
            f"{swatch} has a swatch or its words, not both"
        )
