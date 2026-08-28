"""The bulk way back (Phase 1 bench design, 2026-08-27): the Network
board's parked view, grouped by the audit note that parked each cohort, and
the audited `unpark` bulk verb that restores `thread_state` from `warmth`.

`transaction=True`: `services.set_contact_state` opens its own psycopg
connection outside Django's test transaction — same posture as
`test_today.py` / `test_bench.py`.
"""

from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse

from crm import services
from crm.models import Contact, Touch

pytestmark = pytest.mark.django_db(transaction=True)


def _user(email="parked@example.com", **kw):
    return get_user_model().objects.create_user(
        email=email, password="pw12345!", **kw
    )


def _contact(*, user, **kw):
    return Contact.all_objects.create(user=user, **kw)


def _park(user, contact, *, note):
    services.set_contact_state(
        user.id, contact.id, thread_state="parked", note=note,
    )


def test_one_bulk_park_forms_one_cohort(client):
    user = _user()
    made = [_contact(user=user, name=f"Bulk {i}", warmth="cold") for i in range(3)]
    for c in made:
        _park(user, c, note="Parked from the Today queue (bulk)")

    client.force_login(user)
    resp = client.get(reverse("crm:contact_parked"))
    assert resp.status_code == 200
    body = resp.content.decode()
    assert "3 contacts" in body
    for c in made:
        assert c.name in body


def test_two_different_park_notes_form_two_cohorts(client):
    user = _user()
    a = _contact(user=user, name="From Today", warmth="cold")
    b = _contact(user=user, name="From Network", warmth="cold")
    _park(user, a, note="Parked from the Today queue (bulk)")
    _park(user, b, note="Parked from the Network board (bulk)")

    client.force_login(user)
    body = client.get(reverse("crm:contact_parked")).content.decode()
    assert "Parked from the Today queue (bulk)" in body
    assert "Parked from the Network board (bulk)" in body


def test_no_parked_contacts_is_a_clean_empty_state(client):
    user = _user()
    _contact(user=user, name="Active Annie", warmth="cold", thread_state="no_reply")
    client.force_login(user)
    body = client.get(reverse("crm:contact_parked")).content.decode()
    assert "Nobody is parked" in body


# ---------------------------------------------------------------------------
# The audited unpark bulk verb.
# ---------------------------------------------------------------------------
def test_unpark_restores_thread_state_from_warmth(client):
    user = _user()
    cold = _contact(user=user, name="Cold Cara", warmth="cold")
    chatted = _contact(user=user, name="Chatted Chad", warmth="chatted")
    advocate = _contact(user=user, name="Advocate Ana", warmth="advocate")
    replied = _contact(user=user, name="Replied Rex", warmth="replied")
    for c in (cold, chatted, advocate, replied):
        _park(user, c, note="Parked from the Network board (bulk)")

    client.force_login(user)
    resp = client.post(
        reverse("crm:contact_parked_unpark"),
        {"ids": [cold.id, chatted.id, advocate.id, replied.id]},
    )
    assert resp.status_code == 302

    cold.refresh_from_db(); chatted.refresh_from_db()
    advocate.refresh_from_db(); replied.refresh_from_db()
    assert cold.thread_state == "no_reply"
    assert chatted.thread_state == "chat_done"
    assert advocate.thread_state == "advocate"
    assert replied.thread_state == "replied"


def test_unpark_is_audited_with_one_touch_per_contact(client):
    user = _user()
    made = [_contact(user=user, name=f"Aud {i}", warmth="cold") for i in range(4)]
    for c in made:
        _park(user, c, note="Parked from the Today queue (bulk)")

    client.force_login(user)
    client.post(reverse("crm:contact_parked_unpark"), {"ids": [c.id for c in made]})

    for c in made:
        overrides = Touch.all_objects.filter(
            user=user, contact=c, kind="manual_override",
        ).order_by("-ts")
        assert overrides.count() == 2  # the park, then the unpark
        assert "Unparked" in overrides.first().note


def test_unpark_ignores_ids_not_currently_parked(client):
    """`contacts_bulk`'s own posture: trust the posted ids, but resolve every
    one through `.for_user`/the current state, so a stale checkbox (already
    unparked, or never parked) is a no-op rather than a corrupting write."""
    user = _user()
    active = _contact(user=user, name="Never Parked", warmth="cold",
                       thread_state="no_reply")
    client.force_login(user)
    resp = client.post(reverse("crm:contact_parked_unpark"), {"ids": [active.id]})
    assert resp.status_code == 302
    active.refresh_from_db()
    assert active.thread_state == "no_reply"
    assert not Touch.all_objects.filter(
        user=user, contact=active, kind="manual_override"
    ).exists()


def test_unpark_is_tenant_scoped(client):
    a = _user("aa@example.com")
    b = _user("bb@example.com")
    theirs = _contact(user=b, name="Not Yours", warmth="chatted")
    _park(b, theirs, note="Parked from the Today queue (bulk)")

    client.force_login(a)
    resp = client.post(reverse("crm:contact_parked_unpark"), {"ids": [theirs.id]})
    assert resp.status_code == 302
    theirs.refresh_from_db()
    assert theirs.thread_state == "parked", "another tenant's contact must be untouched"


def test_parked_cohorts_view_is_tenant_scoped(client):
    a = _user("cc@example.com")
    b = _user("dd@example.com")
    theirs = _contact(user=b, name="Belongs To B", warmth="cold")
    _park(b, theirs, note="Parked from the Today queue (bulk)")

    client.force_login(a)
    body = client.get(reverse("crm:contact_parked")).content.decode()
    assert "Belongs To B" not in body
