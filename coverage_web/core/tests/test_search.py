"""The Cmd-K palette's endpoint: one box, three tables, tenant walls intact."""

from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from directory.models import Firm, Opportunity
from crm.models import Contact

pytestmark = pytest.mark.django_db


@pytest.fixture
def user(django_user_model):
    return django_user_model.objects.create_user(
        email="palette@example.com", password="x"
    )


def test_search_spans_contacts_firms_and_roles(client, user):
    firm = Firm.objects.create(slug="gs", name="Goldman Sachs")
    Contact.all_objects.create(user=user, name="Golda Meyer", firm=firm)
    Opportunity.objects.create(firm=firm, title="Gold Desk Intern",
                               url="https://x/1", status="open", bucket="internship")
    client.force_login(user)
    data = client.get("/search/", {"q": "gold"}).json()
    assert [c["name"] for c in data["contacts"]] == ["Golda Meyer"]
    assert [f["name"] for f in data["firms"]] == ["Goldman Sachs"]
    assert [r["title"] for r in data["roles"]] == ["Gold Desk Intern"]


def test_another_tenants_contacts_never_appear(client, user, django_user_model):
    other = django_user_model.objects.create_user(
        email="other@example.com", password="x")
    Contact.all_objects.create(user=other, name="Golda Private")
    client.force_login(user)
    assert client.get("/search/", {"q": "golda"}).json()["contacts"] == []


def test_signed_out_gets_shared_zone_only(client):
    Firm.objects.create(slug="gs", name="Goldman Sachs")
    data = client.get("/search/", {"q": "gold"}).json()
    assert data["contacts"] == []
    assert [f["name"] for f in data["firms"]] == ["Goldman Sachs"]


def test_one_character_returns_nothing(client, user):
    """The palette fires on every keystroke; a one-letter icontains over
    four tables is all noise and no answer."""
    client.force_login(user)
    data = client.get("/search/", {"q": "g"}).json()
    assert data == {"contacts": [], "firms": [], "roles": []}


def test_results_are_capped(client, user):
    firm = Firm.objects.create(slug="gs", name="Goldman Sachs")
    for i in range(20):
        Opportunity.objects.create(firm=firm, title=f"Gold Role {i:02d}",
                                   url=f"https://x/{i}", status="open",
                                   bucket="internship")
    client.force_login(user)
    assert len(client.get("/search/", {"q": "gold"}).json()["roles"]) == 8
