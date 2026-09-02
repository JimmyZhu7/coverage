"""The copy-names control on a queue lane (2026-09-02, the founder's ask).

A lane is a list of people he is about to write to somewhere else, and a
page that already holds the names should not make anyone retype them.
"""

import re
from datetime import timedelta

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone

from crm.models import Contact, Touch, UserFirm
from directory.models import Firm

pytestmark = pytest.mark.django_db


def _queue_user(email="lanecopy@example.com"):
    """A user whose queue actually builds a lane: a targeted firm, and cold
    contacts touched long enough ago to be due a follow-up."""
    user = get_user_model().objects.create_user(
        email=email, password="pw12345!", weekly_touch_goal=14)
    firm = Firm.objects.create(slug="nomura-copy", name="Nomura")
    UserFirm.all_objects.create(user=user, firm=firm, tier=1)
    for i in range(3):
        c = Contact.all_objects.create(user=user, name=f"Cold {i:02d}", firm=firm)
        Touch.all_objects.create(
            user=user, contact=c, kind="outreach", channel="email",
            ts=timezone.now() - timedelta(days=20))
    return user


def _lane_headings(body: str) -> list[str]:
    return re.findall(r'<h2 class="lane-title">(.*?)</h2>', body, re.S)


def test_a_lane_offers_a_copy_control_naming_its_own_count(client):
    user = _queue_user()
    client.force_login(user)
    res = client.get("/app/")
    assert res.context["lanes"], "fixture built no lane to copy from"

    headings = _lane_headings(res.content.decode())
    assert headings, "no lane heading rendered"
    assert any("data-lane-copy" in h for h in headings), (
        "a lane heading carries no copy control"
    )
    # The accessible name says which lane and how many people, because
    # "Copy names" alone is identical on every lane of the page.
    assert any(re.search(r'aria-label="Copy the \d+ names? in ', h)
               for h in headings)


def test_the_names_are_read_from_the_cards_not_baked_into_the_page(client):
    """Read at click time from `.act-name`, so a lane that has since lost a
    card to a Snooze cannot copy a name that is no longer on screen. A list
    rendered into an attribute here would be exactly that stale snapshot,
    because every quick action swaps this subtree."""
    user = _queue_user("lanecopy2@example.com")
    client.force_login(user)
    body = client.get("/app/").content.decode()

    assert 'querySelectorAll(".act-name")' in body
    assert "data-lane-names" not in body, "the names were baked into the markup"


def test_the_handler_is_delegated_so_it_survives_a_queue_swap(client):
    """Bound handlers die on the first Snooze: the cockpit's innerHTML is
    replaced, taking the button with it. The calendar hit exactly this on
    2026-09-02 with its Today control."""
    user = _queue_user("lanecopy3@example.com")
    client.force_login(user)
    body = client.get("/app/").content.decode()

    assert 'cockpit.addEventListener("click"' in body
    assert 'closest("[data-lane-copy]")' in body
