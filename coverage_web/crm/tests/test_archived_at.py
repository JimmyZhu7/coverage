"""When a contact left the board, and when one was parked (WS-CRM-16).

`audit-crm-lifecycle.md` §6 and E6. Archive flipped a boolean and wrote
nothing else: no touch, no timestamp, so the archived ledger could list 41
people and not say when any of them left, and ordered them by name — the one
order that carries no information about the decision a reader came to
reverse. Park had a timestamp, but only inside the audit note's prose, which
is optional, human-authored and editable.

Both facts now come off a ROW: `Contact.archived_at` for the first, the
override touch's own `ts` for the second.
"""

from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse
from django.utils import timezone

from coverage_domain.pipeline import MANUAL_OVERRIDE_KIND
from crm import services
from crm.models import Contact, Touch
from crm.views import PARK_OVERRIDE_FIELD, park_ts

User = get_user_model()


def _user(email="student@example.com"):
    return User.objects.create_user(email=email, password="pw12345!")


# ---------------------------------------------------------------------------
# archived_at
# ---------------------------------------------------------------------------
@pytest.mark.django_db
def test_archive_stamps_the_time_and_unarchive_clears_it(client):
    user = _user()
    c = Contact.all_objects.create(user=user, name="Ada")
    client.force_login(user)

    before = timezone.now()
    client.post(reverse("crm:contact_archive", args=[c.pk]))
    c.refresh_from_db()
    assert c.archived is True
    assert c.archived_at is not None and c.archived_at >= before

    client.post(reverse("crm:contact_unarchive", args=[c.pk]))
    c.refresh_from_db()
    assert c.archived is False
    # Cleared, not kept: the column answers "when did this row last leave
    # the board", and a contact who is back has no answer to give.
    assert c.archived_at is None


@pytest.mark.django_db
def test_the_bulk_door_stamps_it_too(client):
    """The door 83 people can leave through at once is the last one that
    should leave the ledger unable to say when."""
    user = _user()
    rows = [Contact.all_objects.create(user=user, name=f"C{i}") for i in range(3)]
    client.force_login(user)
    client.post(reverse("crm:contacts_bulk"),
                {"verb": "archive", "ids": [c.id for c in rows]})
    for c in rows:
        c.refresh_from_db()
        assert c.archived is True and c.archived_at is not None


@pytest.mark.django_db
def test_the_archived_list_sorts_newest_first_with_nulls_last(client):
    """A recovery surface is read after a mistake, and the mistake is nearly
    always the most recent thing that happened. Rows archived before the
    column existed have no date and sort last rather than being handed an
    invented one."""
    user = _user()
    now = timezone.now()
    old = Contact.all_objects.create(
        user=user, name="Older", archived=True,
        archived_at=now - timezone.timedelta(days=5),
    )
    new = Contact.all_objects.create(
        user=user, name="Newer", archived=True, archived_at=now,
    )
    legacy = Contact.all_objects.create(
        user=user, name="Aaa Legacy", archived=True, archived_at=None,
    )

    client.force_login(user)
    resp = client.get(reverse("crm:contact_archived"))
    assert [c.pk for c in resp.context["contacts"]] == [new.pk, old.pk, legacy.pk]


@pytest.mark.django_db
def test_the_archived_row_says_which_date_it_is_showing(client):
    user = _user()
    Contact.all_objects.create(user=user, name="Ada", archived=True,
                               archived_at=timezone.now())
    Contact.all_objects.create(user=user, name="Bea", archived=True)
    client.force_login(user)
    body = client.get(reverse("crm:contact_archived")).content.decode()
    assert "Archived " in body
    # The legacy row falls back to the last touch, labelled, never to a
    # made-up archive date.
    assert "No touches" in body


# ---------------------------------------------------------------------------
# The park timestamp
# ---------------------------------------------------------------------------
@pytest.mark.django_db(transaction=True)
def test_park_ts_reads_the_row_not_the_note():
    """The whole point: a park whose note carries no date at all still has
    one, because the timestamp was never in the prose to begin with."""
    user = _user()
    c = Contact.all_objects.create(user=user, name="Ada", warmth="cold")
    services.set_contact_state(user.id, c.id, thread_state="parked",
                               note="no reply")
    c.refresh_from_db()
    touches = list(Touch.objects.for_user(user).filter(contact=c).order_by("-ts"))
    row = next(t for t in touches
               if t.kind == MANUAL_OVERRIDE_KIND
               and PARK_OVERRIDE_FIELD in (t.note or ""))
    assert park_ts(c, touches) == row.ts


@pytest.mark.django_db
def test_park_ts_is_none_for_an_unparked_contact_and_for_no_audit_row():
    """P1 and P3. A contact who is not parked has no park date, and a
    contact parked before the audit trail existed keeps the "no record on
    file" answer the cohort page already gives rather than inventing one."""
    user = _user()
    live = Contact.all_objects.create(user=user, name="Live")
    assert park_ts(live, []) is None

    imported = Contact.all_objects.create(user=user, name="Imported",
                                          thread_state="parked")
    assert park_ts(imported, []) is None


@pytest.mark.django_db(transaction=True)
def test_the_contact_page_shows_the_park_date(client):
    user = _user()
    c = Contact.all_objects.create(user=user, name="Ada", warmth="cold")
    services.set_contact_state(user.id, c.id, thread_state="parked",
                               note="no reply")
    client.force_login(user)
    body = client.get(reverse("crm:contact_detail", args=[c.pk])).content.decode()
    assert "Parked " in body
