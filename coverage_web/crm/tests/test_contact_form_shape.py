"""The add-a-contact form's shape (WS-CRM-17).

`audit-first-visit-a11y.md` §1.5 D11 measured fourteen fields on this page,
only one of them required, and no sentence anywhere saying so. Seven of the
fourteen — Region, the two rule overrides, the campaign hatch, Angle, Opener
and Notes — only start mattering after the first touch, and on a 375x812
phone they pushed Save off the bottom of the screen.

These tests pin the fold: seven controls in front, the rest behind one
`<details>`, a name still being all the form needs, and the `?quick=1` path
untouched.
"""

from __future__ import annotations

import re

import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse

from crm.models import Contact

User = get_user_model()

_CONTROL = re.compile(r"<(input|select|textarea)\b", re.I)
_DISCLOSURE = '<details class="cf-more"'


def _user(email="student@example.com"):
    return User.objects.create_user(email=email, password="x")


def _controls_before_the_disclosure(body: str) -> int:
    """Count the form controls a student meets before opening "More".

    The CSRF token is excluded: it is a hidden input Django writes, not a
    question the form asks.
    """
    head = body.split(_DISCLOSURE)[0]
    head = head.replace('<input type="hidden" name="csrfmiddlewaretoken"', "")
    return len(_CONTROL.findall(head))


@pytest.mark.django_db
def test_the_add_form_shows_five_controls_and_folds_the_rest(client):
    client.force_login(_user())
    body = client.get(reverse("crm:contact_new")).content.decode()

    assert _DISCLOSURE in body
    assert "<summary>More</summary>" in body
    # Name, Firm, Firm (if not listed), Role, Email — the five a first
    # contact is actually made of.
    assert _controls_before_the_disclosure(body) == 5

    # Folded, not dropped (P4): every one of the nine is still on the page,
    # still posts under its own name, still editable.
    for field in ("school", "linkedin", "region", "recruiting_contact",
                  "recruitment_related", "campaign_exempt", "angle", "opener",
                  "notes"):
        assert f'name="{field}"' in body


@pytest.mark.django_db
def test_the_add_form_says_only_a_name_is_required(client):
    client.force_login(_user())
    body = client.get(reverse("crm:contact_new")).content.decode()
    assert "Only a name is required." in body


@pytest.mark.django_db
def test_a_name_on_its_own_still_saves(client):
    """The sentence has to be true. A POST carrying nothing but a name is
    the whole contract of the page."""
    user = _user()
    client.force_login(user)
    resp = client.post(reverse("crm:contact_new"), {"name": "Ada Lovelace"})
    assert resp.status_code == 302
    assert Contact.all_objects.filter(user=user, name="Ada Lovelace").exists()


@pytest.mark.django_db
def test_the_edit_form_opens_the_disclosure(client):
    """Editing is the one moment the folded seven are the reason you came.
    A field already filled in is not a field to hide."""
    user = _user()
    contact = Contact.all_objects.create(user=user, name="Grace", region="us")
    client.force_login(user)
    body = client.get(reverse("crm:contact_edit", args=[contact.pk])).content.decode()
    assert f'{_DISCLOSURE} open>' in body


@pytest.mark.django_db
def test_quick_add_is_unchanged(client):
    """P3: the fast path had three questions and no disclosure before this
    change and has three questions and no disclosure after it."""
    client.force_login(_user())
    body = client.get(reverse("crm:contact_new") + "?quick=1").content.decode()
    assert _DISCLOSURE not in body
    assert "<summary>More</summary>" not in body
    for absent in ("school", "linkedin", "region", "recruiting_contact",
                   "recruitment_related", "campaign_exempt", "angle", "opener"):
        assert f'name="{absent}"' not in body
    assert 'name="notes"' in body
