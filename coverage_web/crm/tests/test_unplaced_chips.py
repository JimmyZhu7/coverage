"""The Unplaced tab asks a two-way question with a two-way control (WS-CRM-15).

`audit-crm-lifecycle.md` E5, measured on the founder's board: 94 of 265 live
rows carry a blank region, 90 of them sit at a firm that recruits in both us
and hk, exactly 1 has no firm at all, and 0 sit at a single-market firm. The
deterministic rule (`Contact.default_region_from_firm`) had therefore already
fired everywhere it could, and every row left over was a genuine two-way
question being asked with three chips.

The chips are narrowed per group, never removed from the page: the three-verb
bulk bar under the groups still carries all three, because "Other countries"
is the only way a human ever says London and no firm's market list rules it
out (P4).
"""

from __future__ import annotations

import re

import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse

from crm.models import Contact
from crm.views import _group_unplaced
from directory.models import Firm

User = get_user_model()


def _user(regions=None, email="student@example.com"):
    return User.objects.create_user(
        email=email, password="x", regions=list(regions or [])
    )


def _firm(regions, slug="f", name="A Firm"):
    return Firm.objects.create(slug=slug, name=name, regions=list(regions))


def _chips(groups, label):
    return [g["chips"] for g in groups if g["label"] == label][0]


@pytest.mark.django_db
def test_a_two_market_firm_offers_two_chips_and_no_firm_offers_three():
    """The founder's shape, both halves of it: 90 rows at a both-markets firm
    and 1 row with no firm at all."""
    user = _user(["hk", "us"])
    both = _firm(["us", "hk"], slug="both", name="Both Markets Bank")
    at_firm = Contact.all_objects.create(user=user, name="Pat", firm=both)
    loose = Contact.all_objects.create(user=user, name="Sam", firm_text="")

    groups = _group_unplaced([at_firm, loose])

    assert [v for v, _ in _chips(groups, "Both Markets Bank")] == [
        "region_us", "region_hk",
    ]
    assert [v for v, _ in _chips(groups, "No firm")] == [
        "region_us", "region_hk", "region_other",
    ]


@pytest.mark.django_db
def test_a_single_market_firm_offers_one_chip():
    """The case the founder's board has none of, and the reason the rule is
    written on the firm's markets rather than on a count."""
    user = _user(["hk", "us"])
    us_only = _firm(["us"], slug="usonly", name="US Only LLC")
    c = Contact.all_objects.create(user=user, name="Pat", firm=us_only,
                                   region="", region_source="")
    assert [v for v, _ in _chips(_group_unplaced([c]), "US Only LLC")] == [
        "region_us",
    ]


@pytest.mark.django_db
def test_a_firm_with_no_deadline_markets_keeps_all_three():
    """P3. `['sg', 'eu']` entails nothing about us or hk, so the tab degrades
    to exactly the three chips it offered before this change."""
    user = _user(["hk", "us"])
    elsewhere = _firm(["sg", "eu"], slug="sgeu", name="Elsewhere Partners")
    c = Contact.all_objects.create(user=user, name="Pat", firm=elsewhere)
    assert [v for v, _ in _chips(_group_unplaced([c]), "Elsewhere Partners")] == [
        "region_us", "region_hk", "region_other",
    ]


@pytest.mark.django_db
def test_the_tab_renders_the_narrowed_chips_and_keeps_the_full_bar(client):
    user = _user(["hk", "us"])
    both = _firm(["us", "hk"], slug="both", name="Both Markets Bank")
    Contact.all_objects.create(user=user, name="Pat", firm=both)
    client.force_login(user)
    body = client.get(reverse("crm:contact_list"),
                      {"scope": "unplaced"}).content.decode()

    chips = body.split('data-place-chips')[1].split("</div>")[0]
    assert 'value="region_us"' in chips and 'value="region_hk"' in chips
    assert 'value="region_other"' not in chips

    # The bulk bar below the groups is untouched: all three verbs, always.
    bar = body.split('class="bulk-bar"')[1]
    for verb in ("region_us", "region_hk", "region_other"):
        assert f'value="{verb}"' in bar
    assert "Other countries" in bar


@pytest.mark.django_db
def test_the_chips_are_hidden_until_the_script_reveals_them(client):
    """Same progressive-enhancement contract as "Select all": with JS off the
    checkboxes and the three-verb bar are the whole control, because a chip
    that posts without the script would file whatever was ticked elsewhere on
    the page rather than what its label says."""
    user = _user(["hk", "us"])
    Contact.all_objects.create(user=user, name="Pat",
                               firm=_firm(["us", "hk"], slug="b", name="B Bank"))
    client.force_login(user)
    body = client.get(reverse("crm:contact_list"),
                      {"scope": "unplaced"}).content.decode()
    assert re.search(r'data-place-chips\s+hidden', body)
    # AND the CSS pairing that makes the attribute mean anything. The row
    # sets its own `display: flex`, which ties with the browser's
    # `[hidden] { display: none }` and wins on cascade order — the exact bug
    # `.bulk-bar[hidden]` was written to fix. Without this rule the chips
    # render visible with JS off and post the wrong selection.
    assert ".net-group-chips[hidden] { display: none; }" in body


@pytest.mark.django_db
def test_the_button_words_come_from_one_place(client):
    """P5. The bulk bar used to spell "United States" in an `{% if %}` chain
    of its own while `crm.views` held the verbs; the per-group chips would
    have made that a third copy."""
    from crm.views import REGION_BULK_LABELS, REGION_BULK_VERBS

    assert set(REGION_BULK_LABELS) == set(REGION_BULK_VERBS)
    user = _user(["hk", "us"])
    Contact.all_objects.create(user=user, name="Pat",
                               firm=_firm(["us", "hk"], slug="b", name="B Bank"))
    client.force_login(user)
    body = client.get(reverse("crm:contact_list"),
                      {"scope": "unplaced"}).content.decode()
    for label in REGION_BULK_LABELS.values():
        assert label in body
