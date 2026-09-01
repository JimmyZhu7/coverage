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


# ---------------------------------------------------------------------------
# The palette renders whatever the serializer hands it, verbatim, so the
# tidying has to happen server-side. A cross-surface audit (2026-09-01) found
# it shipping three raw database values to the student at once: an email
# local part stored as a name by capture, the warmth slug, and a role title
# in whatever case the firm's board shouted it in.
# ---------------------------------------------------------------------------
@pytest.mark.django_db
def test_the_palette_never_ships_a_raw_name_or_warmth_slug(client):
    from django.contrib.auth import get_user_model
    from crm.models import Contact
    from directory.models import Firm
    import json

    User = get_user_model()
    user = User.objects.create_user(email="palette@example.com", password="x")
    firm = Firm.objects.create(slug="palette-co", name="PALETTE CAPITAL")
    Contact.all_objects.create(
        user=user, name="jude.yoon", email="jude.yoon@palette.example",
        firm=firm, warmth="chatted",
    )

    client.force_login(user)
    data = json.loads(client.get("/search/?q=yoon").content)
    person = data["contacts"][0]

    # The local part becomes a readable name, and the stored row is untouched.
    assert person["name"] == "Jude Yoon"
    assert Contact.all_objects.get(email="jude.yoon@palette.example").name == "jude.yoon"
    # The warmth slug becomes the words every other surface already uses.
    assert person["warmth"] == "Chatted"
    assert person["warmth"] != "chatted"
    # A shouting firm name is tidied like it is everywhere else.
    assert person["firm"] == "Palette Capital"


@pytest.mark.django_db
def test_the_palette_reads_a_whole_address_stored_as_a_name(client):
    """Capture usually keeps only the local part, but two of the founder's
    rows hold the entire address ('victoria.hsu@gs.com', source="capture"),
    and the palette shipped them as "Victoria.hsu@gs.com" -- `smart_title`
    capitalising the first letter of what is, to it, one long word.

    Pinned here and not only in `test_textstyle.py` because the palette
    builds its strings in `core/views.py` rather than in a template: a
    filter-level fix that never got wired into the serializer would pass
    the unit test and change nothing a student sees.
    """
    User = get_user_model()
    user = User.objects.create_user(email="addr@example.com", password="x")
    firm = Firm.objects.create(slug="addr-co", name="Addr Capital")
    Contact.all_objects.create(
        user=user, name="victoria.hsu@addr.example",
        email="victoria.hsu@addr.example", firm=firm, warmth="chatted",
    )

    client.force_login(user)
    person = client.get("/search/", {"q": "victoria"}).json()["contacts"][0]

    assert person["name"] == "Victoria Hsu"
    assert "@" not in person["name"]
    # The stored row keeps exactly what capture observed. The honesty rule
    # is not suspended just because the evidence renders badly.
    stored = Contact.all_objects.get(email="victoria.hsu@addr.example")
    assert stored.name == "victoria.hsu@addr.example"
