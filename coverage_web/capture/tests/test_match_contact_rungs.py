"""`_match_contact` — the batch path — finds a contact by a sibling firm
domain and by a role-suffixed display name.

Freddy Guerrero's thread proved both gaps at once: stored at baml.com,
writing from bofa.com, signing "Guerrero, Freddy M - GCM". The live path had
learned the alias; the batch path — the one that turns a scheduled chat into
a CalendarEvent — still called him unmatched.
"""
from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model

from capture.gmail import _match_contact
from crm.models import Contact
from directory.models import Firm


def _user(email="rungs@example.com"):
    return get_user_model().objects.create_user(email=email, password="x" * 12)


@pytest.fixture
def bofa(db):
    return Firm.objects.create(name="Bank of America", slug="bofa",
                               domains=["bankofamerica.com", "bofaml.com", "bofa.com", "baml.com"])


@pytest.mark.django_db
def test_a_sibling_firm_domain_matches_by_email(bofa):
    user = _user()
    c = Contact.all_objects.create(user=user, name="Freddy Guerrero",
                                   email="freddy.guerrero@baml.com", firm=bofa, source="manual")
    assert _match_contact(user, {"email": "freddy.guerrero@bofa.com", "name": "x y"}) == c


@pytest.mark.django_db
def test_a_role_suffixed_inverted_header_matches_by_name(bofa):
    """The email rungs miss (a personal address, on no firm), so the name
    rung has to carry it — and it only can once the role suffix is split
    off. The address is still passed: `_inverted_reading` inverts
    "Guerrero, Freddy" ONLY when the mailbox's own localpart corroborates it,
    so a finding with no email at all deliberately does not invert. That is
    the design refusing to guess, not a gap."""
    user = _user()
    c = Contact.all_objects.create(user=user, name="Freddy Guerrero",
                                   email="freddy.guerrero@baml.com", firm=bofa, source="manual")
    found = _match_contact(user, {"email": "freddy.guerrero@gmail.com",
                                  "name": "Guerrero, Freddy M - GCM"})
    assert found == c


@pytest.mark.django_db
def test_a_role_suffixed_header_with_no_address_still_matches_nobody(bofa):
    """The raw string fails on the " - GCM" suffix; the split, with no address
    to corroborate an inversion, yields a bare "Guerrero". Neither reading
    equates to "Freddy Guerrero", so the rung refuses — which is correct: it
    is the suffix that blocks this, not the comma. The bare comma form
    without a suffix still matches with no address at all
    (`test_matches_inverted_last_first_display_name` in test_gmail.py)."""
    user = _user()
    Contact.all_objects.create(user=user, name="Freddy Guerrero",
                               email="", firm=bofa, source="manual")
    assert _match_contact(user, {"email": "", "name": "Guerrero, Freddy M - GCM"}) is None


@pytest.mark.django_db
def test_the_bare_comma_form_still_matches_with_no_address(bofa):
    """The regression this file exists to pin: the split reading must never
    REPLACE the raw one."""
    user = _user()
    c = Contact.all_objects.create(user=user, name="Vanessa Nunley",
                                   email="", firm=bofa, source="manual")
    assert _match_contact(user, {"email": "", "name": "Nunley, Vanessa N"}) == c


@pytest.mark.django_db
def test_it_still_never_crosses_firms(bofa):
    user = _user()
    gs = Firm.objects.create(name="Goldman Sachs", slug="gs", domains=["gs.com"])
    Contact.all_objects.create(user=user, name="J Smith", email="j.smith@gs.com", firm=gs, source="manual")
    assert _match_contact(user, {"email": "j.smith@bofa.com", "name": "Smith, J"}) is None
