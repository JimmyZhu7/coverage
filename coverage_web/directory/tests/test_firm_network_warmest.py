"""Firm detail's "Your Network Here" card: the "Warmest here:" callout must
not restate the first row of the warmth-sorted contact list rendered right
below it — see _my_network_at's my_next_restates_top_row comment.

`my_next` (the callout) and `my_contacts[0]` (the list's first row) are both
picked from the same warmth-tier-first ordering, so whenever the top tier
holds exactly one person they name that same person twice, adjacently. This
was live on /firms/hsbc/: "Warmest here: James Bai · IB Associate" directly
above a list whose first row was James Bai again.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone

from crm.models import Contact, Touch
from directory.models import Firm
from directory.views import _my_network_at

User = get_user_model()


@pytest.fixture
def user(db):
    return User.objects.create_user(email="net@example.com", password="x")


def _touch(user, contact, days_ago):
    Touch.all_objects.create(
        user=user, contact=contact, kind="chat", source="manual",
        ts=timezone.now() - timedelta(days=days_ago),
    )


@pytest.mark.django_db
def test_sole_top_tier_contact_restates_the_list_and_is_flagged():
    """The exact HSBC live repro: one advocate, nobody else in that tier —
    `my_next` and the list's first row are the same person."""
    user = User.objects.create_user(email="a@example.com", password="x")
    firm = Firm.objects.create(slug="hsbc", name="HSBC")
    james = Contact.all_objects.create(user=user, firm=firm, name="James Bai",
                                   role="IB Associate", warmth="advocate")
    _touch(user, james, 22)

    ctx = _my_network_at(user, firm, today=timezone.localdate())

    assert ctx["my_next"].id == james.id
    assert ctx["my_contacts"][0]["c"].id == james.id
    assert ctx["my_next_restates_top_row"] is True


@pytest.mark.django_db
def test_two_top_tier_contacts_the_callout_earns_its_line():
    """Alice sorts first alphabetically within the tier; Bob has waited
    longer since last contact and wins the days-since tie-break, so the
    callout names someone OTHER than the list's first row — genuinely new
    information, and the flag must be False."""
    user = User.objects.create_user(email="b@example.com", password="x")
    firm = Firm.objects.create(slug="hsbc", name="HSBC")
    alice = Contact.all_objects.create(user=user, firm=firm, name="Alice Wong",
                                   role="Analyst", warmth="advocate")
    bob = Contact.all_objects.create(user=user, firm=firm, name="Bob Chen",
                                 role="Associate", warmth="advocate")
    _touch(user, alice, 3)
    _touch(user, bob, 40)

    ctx = _my_network_at(user, firm, today=timezone.localdate())

    assert ctx["my_contacts"][0]["c"].id == alice.id  # alphabetically first
    assert ctx["my_next"].id == bob.id                # longest-waiting advocate
    assert ctx["my_next_restates_top_row"] is False


@pytest.mark.django_db
def test_firm_page_hides_the_callout_when_it_would_restate_the_list(client):
    user = User.objects.create_user(email="c@example.com", password="x")
    firm = Firm.objects.create(slug="hsbc", name="HSBC")
    james = Contact.all_objects.create(user=user, firm=firm, name="James Bai",
                                   role="IB Associate", warmth="advocate")
    _touch(user, james, 22)
    client.force_login(user)

    body = client.get("/firms/hsbc/").content.decode()

    assert "Warmest here" not in body
    assert "James Bai" in body  # still shown once, in the list itself


@pytest.mark.django_db
def test_firm_page_shows_the_callout_when_it_names_someone_new(client):
    user = User.objects.create_user(email="d@example.com", password="x")
    firm = Firm.objects.create(slug="hsbc", name="HSBC")
    alice = Contact.all_objects.create(user=user, firm=firm, name="Alice Wong",
                                   role="Analyst", warmth="advocate")
    bob = Contact.all_objects.create(user=user, firm=firm, name="Bob Chen",
                                 role="Associate", warmth="advocate")
    _touch(user, alice, 3)
    _touch(user, bob, 40)
    client.force_login(user)

    body = client.get("/firms/hsbc/").content.decode()

    assert "Warmest here" in body
    assert "Bob Chen" in body
