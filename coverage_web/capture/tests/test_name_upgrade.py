"""A placeholder name gives way to the name the person signs with.

`capture` stores what it observed, and for someone first seen in the user's
own sent mail that is the address localpart — 86 of the founder's 282 live
contacts. When they reply, their From header carries a real display name;
`split_display_name` already reads it (it even kept the role half off
"Roach, Garrett - GCIB LA"), and the name half used to be dropped.
"""
from __future__ import annotations

import pytest

from capture.gmail import apply_findings
from crm.models import Contact
from directory.models import Firm
from django.contrib.auth import get_user_model


def _user(email="upgrade@example.com"):
    return get_user_model().objects.create_user(email=email, password="x" * 12)


def _finding(name, email, **over):
    base = {
        "name": name, "email": email, "found": True, "bounced": False,
        # `replied: False` — the name upgrade runs before the touch ladder and
        # does not depend on it, and a reply touch drags the whole domain
        # pipeline into a test about one string.
        "outreach_sent": False, "replied": False, "chat_status": "none",
        "chat_scheduled_at": None, "thread_id": "t1", "subject": "RE: chat",
    }
    base.update(over)
    return base


@pytest.fixture
def bofa(db):
    return Firm.objects.create(name="Bank of America", slug="bofa",
                               domains=["bofa.com", "baml.com"])


@pytest.mark.django_db
def test_a_reply_replaces_the_localpart_placeholder(bofa):
    user = _user()
    c = Contact.all_objects.create(user=user, name="garrett.roach",
                                   email="garrett.roach@bofa.com", firm=bofa, source="manual")

    apply_findings(user, [_finding("Roach, Garrett - GCIB LA",
                                   "garrett.roach@bofa.com")])

    c.refresh_from_db()
    assert c.name == "Garrett Roach"


@pytest.mark.django_db
def test_it_never_overwrites_a_name_a_human_typed(bofa):
    """The whole guard: a stored name that is not localpart-shaped is somebody's
    own words and is left alone, however the header spells them."""
    user = _user()
    c = Contact.all_objects.create(user=user, name="Freddy Guerrero",
                                   email="freddy.guerrero@bofa.com", firm=bofa, source="manual")

    apply_findings(user, [_finding("GUERRERO, FREDDY M - GCM",
                                   "freddy.guerrero@bofa.com")])

    c.refresh_from_db()
    assert c.name == "Freddy Guerrero"


@pytest.mark.django_db
def test_a_header_with_no_real_name_changes_nothing(bofa):
    """Plenty of senders put the address in the display slot. That is not an
    upgrade over the placeholder, it is the same information."""
    user = _user()
    c = Contact.all_objects.create(user=user, name="kevin.lee",
                                   email="kevin.lee@bofa.com", firm=bofa, source="manual")

    apply_findings(user, [_finding("kevin.lee@bofa.com", "kevin.lee@bofa.com")])

    c.refresh_from_db()
    assert c.name == "kevin.lee"


@pytest.mark.django_db
def test_a_dry_run_writes_nothing(bofa):
    user = _user()
    c = Contact.all_objects.create(user=user, name="garrett.roach",
                                   email="garrett.roach@bofa.com", firm=bofa, source="manual")

    apply_findings(user, [_finding("Roach, Garrett - GCIB LA",
                                   "garrett.roach@bofa.com")], dry_run=True)

    c.refresh_from_db()
    assert c.name == "garrett.roach"
