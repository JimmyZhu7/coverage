"""A reply from another domain the same firm owns is the same person.

The founder emailed `freddy.guerrero@baml.com` and Freddy replied from
`freddy.guerrero@bofa.com`; Bank of America rewrites the legacy Merrill
address outbound and stamps the original into the subject. Nothing attached
the reply to the contact, so the row sat cold and no-reply while a call was
being arranged on it.
"""
from __future__ import annotations

import pytest

from capture.mailfacts import _contact_for
from crm.models import Contact
from directory.models import Firm
from django.contrib.auth import get_user_model


def _user(email="alias@example.com"):
    return get_user_model().objects.create_user(email=email, password="x" * 12)


@pytest.fixture
def bofa(db):
    return Firm.objects.create(
        name="Bank of America", slug="bofa",
        domains=["bankofamerica.com", "bofaml.com", "bofa.com", "baml.com"],
    )


@pytest.mark.django_db
def test_a_reply_from_a_sibling_domain_finds_the_contact(bofa):
    user = _user()
    c = Contact.all_objects.create(
        user=user, name="Freddy Guerrero",
        email="freddy.guerrero@baml.com", firm=bofa)

    assert _contact_for(user, "freddy.guerrero@bofa.com") == c
    # The exact address still wins, and still works.
    assert _contact_for(user, "freddy.guerrero@baml.com") == c


@pytest.mark.django_db
def test_it_never_matches_across_firms(bofa):
    """Same localpart, different firm, is a different person. `j.smith` is not
    rare enough to guess on."""
    user = _user()
    gs = Firm.objects.create(name="Goldman Sachs", slug="gs", domains=["gs.com"])
    Contact.all_objects.create(user=user, name="J Smith",
                               email="j.smith@gs.com", firm=gs)

    assert _contact_for(user, "j.smith@bofa.com") is None


@pytest.mark.django_db
def test_an_ambiguous_localpart_refuses_rather_than_picking(bofa):
    """Two contacts at the same firm sharing a localpart across its domains is
    unresolvable, and this file's rule is that refusing beats guessing."""
    user = _user()
    Contact.all_objects.create(user=user, name="A", email="chris@baml.com", firm=bofa)
    Contact.all_objects.create(user=user, name="B", email="chris@bofaml.com", firm=bofa)

    assert _contact_for(user, "chris@bofa.com") is None


@pytest.mark.django_db
def test_a_contact_with_no_firm_is_not_matched(bofa):
    """The firm IS the evidence the two domains are one place. Without it
    there is nothing relating them."""
    user = _user()
    Contact.all_objects.create(user=user, name="Loose",
                               email="freddy.guerrero@baml.com", firm=None)

    assert _contact_for(user, "freddy.guerrero@bofa.com") is None


@pytest.mark.django_db
def test_an_unrelated_domain_is_still_a_stranger(bofa):
    user = _user()
    Contact.all_objects.create(user=user, name="Freddy Guerrero",
                               email="freddy.guerrero@baml.com", firm=bofa)

    assert _contact_for(user, "freddy.guerrero@gmail.com") is None
