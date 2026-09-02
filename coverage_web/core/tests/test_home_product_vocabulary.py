"""The landing page may only use words the product still says.

The hero mock and the "one card, opened" vignette on `/` are pictures of the
Today queue, and they were pictures of a version of it that no longer ships.
docs/specs/today-page.md retired two verbs and one whole lane vocabulary:

  F5  "Sent" logged an outreach touch in one click with no compose having
      happened, which invited a sweep down 29 cards that left the CRM
      believing 29 notes went out. The button is "Done" now, an
      attestation. "Reply" read as "compose a reply" and mislogged; the
      button is "They replied", an event about the other person.
  F6  The three lanes were a priority-number echo (`priority 0 -> Overdue`),
      six of eight action kinds landed in one of them, and "Keep Warm"
      actually meant "parked". `crm/today.py::_TODAY_LANES` names them by the
      work now: "Don't lose these", "Move it forward", "Cold follow-ups".

The app followed the spec; the landing page did not, so the first screen a
student ever saw taught them a vocabulary the product would then contradict.
These tests fail on any retired verb reappearing in the mock, whoever puts it
there and however the copy is reworded around it.

WHY THE ASSERTIONS ARE SCOPED TO THE MOCK RATHER THAN THE PAGE. "Sent" and
"Reply" are ordinary English and will legitimately turn up in prose ("a reply
lands and warmth updates itself" is a true sentence about Gmail Live). What
must not happen is either word appearing as a CONTROL: inside `.desk-go`,
`.v-rail` or `.v-flag`, the three classes that draw the product's own
buttons and lane flag.
"""

from __future__ import annotations

import re

import pytest

def _control_labels(body: str) -> list[str]:
    """Every string the landing mock draws as a button or a lane flag."""
    labels: list[str] = []
    for cls in ("desk-go", "v-flag"):
        labels += re.findall(rf'<span class="{cls}[^"]*"[^>]*>\s*([^<]*?)\s*</span>', body)
    rail = re.search(r'<div class="v-rail">(.*?)</div>', body, re.S)
    if rail:
        labels += [
            s.strip() for s in re.findall(r"<span[^>]*>([^<]*)</span>", rail.group(1))
        ]
    return [label for label in labels if label]


@pytest.mark.django_db
def test_landing_mock_draws_only_live_product_verbs(client):
    labels = _control_labels(client.get("/").content.decode())

    assert labels, "the landing mock should render controls to check"
    for retired in ("Sent", "Reply", "Overdue", "Keep Warm", "Due Now"):
        assert retired not in labels, (
            f"{retired!r} is a control label the product retired "
            f"(today-page.md F5/F6); the mock still draws it: {labels}"
        )


@pytest.mark.django_db
def test_landing_mock_uses_the_act_cards_own_verbs(client):
    labels = _control_labels(client.get("/").content.decode())

    for live in ("Done", "They replied"):
        assert live in labels, (
            f"{live!r} is what crm/_act_card.html draws; the mock should "
            f"show the same word. Found: {labels}"
        )


@pytest.mark.django_db
def test_landing_lane_flag_is_a_semantic_lane_name(client):
    from crm.today import _TODAY_LANES

    body = client.get("/").content.decode()
    flags = re.findall(r'<span class="v-flag"[^>]*>\s*([^<]*?)\s*</span>', body)

    assert flags, "the opened-card vignette should carry a lane flag"
    lane_labels = {label for _key, label in _TODAY_LANES}
    for flag in flags:
        assert flag in lane_labels, (
            f"{flag!r} is not one of Today's own lanes {sorted(lane_labels)}. "
            "The mock must read off _TODAY_LANES, not invent a fourth set."
        )


@pytest.mark.django_db
def test_landing_feature_list_does_not_advertise_the_retired_one_click_verbs(client):
    body = client.get("/").content.decode()
    points = re.findall(r'<ul class="feature-points">(.*?)</ul>', body, re.S)
    joined = " ".join(points)

    assert "One-click Sent, Reply" not in joined
    assert "Done, They replied" in joined, (
        "the Today feature bullet should name the buttons the app draws"
    )
