"""Bulk verbs over a multi-selection on the Network board
(crm.views.contacts_bulk).

The feature exists because "Follow Up" routinely holds 80+ people and the
only way to act on them was one at a time. The tests below are mostly about
the two things a bulk control must never get wrong: acting on somebody
else's rows, and doing something a user cannot undo.

`transaction=True`: the `park` verb goes through `crm.services`, which opens
its own psycopg connection outside Django's test transaction and therefore
cannot see uncommitted rows.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse
from django.utils import timezone

from crm.models import Contact, Touch
from directory.models import Firm

pytestmark = pytest.mark.django_db(transaction=True)

User = get_user_model()


def _user(email="bulk@example.com"):
    return User.objects.create_user(email=email, password="pw12345!")


def _contact(user, name, **kw):
    return Contact.all_objects.create(user=user, name=name, **kw)


def _post(client, ids, verb, scope=""):
    return client.post(
        reverse("crm:contacts_bulk"),
        {"ids": [str(i) for i in ids], "verb": verb, "scope": scope},
        follow=True,
    )


# ---------------------------------------------------------------------------
# 1. Each verb does its one thing.
# ---------------------------------------------------------------------------
def test_snooze_sets_the_clock_on_every_selected_contact(client):
    user = _user()
    a = _contact(user, "Ada")
    b = _contact(user, "Bo")
    untouched = _contact(user, "Cy")
    client.force_login(user)

    _post(client, [a.id, b.id], "snooze")

    a.refresh_from_db(); b.refresh_from_db(); untouched.refresh_from_db()
    assert a.snoozed_until and b.snoozed_until
    assert untouched.snoozed_until is None
    # ~3 days out, not "some time in the future". Measured in hours, not
    # `.days`: three days minus the few microseconds this test takes
    # truncates to `.days == 2`, which is a broken assertion, not a bug.
    hours = (a.snoozed_until - timezone.now()).total_seconds() / 3600
    assert 71 < hours <= 72


def test_park_moves_thread_state_and_leaves_an_audit_row(client):
    """Parking goes through the audited override, one `manual_override`
    touch per contact — the same contract `today_park_all` documents. A
    bulk UPDATE would move a dozen relationships with nothing on the record
    saying who did it."""
    user = _user()
    a = _contact(user, "Ada")
    b = _contact(user, "Bo")
    client.force_login(user)

    _post(client, [a.id, b.id], "park")

    a.refresh_from_db(); b.refresh_from_db()
    assert a.thread_state == "parked" and b.thread_state == "parked"
    for c in (a, b):
        assert Touch.all_objects.filter(
            user=user, contact=c, kind="manual_override"
        ).exists()


def test_archive_takes_them_off_the_board_without_losing_history(client):
    user = _user()
    firm = Firm.objects.create(slug="acme", name="Acme")
    a = _contact(user, "Ada", firm=firm)
    Touch.all_objects.create(user=user, contact=a, kind="outreach",
                             channel="email", ts=timezone.now())
    client.force_login(user)

    _post(client, [a.id], "archive")

    a.refresh_from_db()
    assert a.archived is True
    # The whole point of archive over delete: the row and its touches stay.
    assert Touch.all_objects.filter(user=user, contact=a).count() == 1
    assert Contact.all_objects.filter(pk=a.pk).exists()


# ---------------------------------------------------------------------------
# 2. Tenancy — the property that matters most on a control that takes ids
#    straight from the client.
# ---------------------------------------------------------------------------
def test_another_tenants_ids_are_silently_ignored(client):
    mine = _user("mine@example.com")
    theirs = _user("theirs@example.com")
    ours = _contact(mine, "Ours")
    not_ours = _contact(theirs, "Not Ours")
    client.force_login(mine)

    _post(client, [ours.id, not_ours.id], "archive")

    ours.refresh_from_db(); not_ours.refresh_from_db()
    assert ours.archived is True
    assert not_ours.archived is False


def test_posting_only_another_tenants_id_changes_nothing(client):
    mine = _user("mine@example.com")
    theirs = _user("theirs@example.com")
    not_ours = _contact(theirs, "Not Ours")
    client.force_login(mine)

    resp = _post(client, [not_ours.id], "archive")

    not_ours.refresh_from_db()
    assert not_ours.archived is False
    assert b"Nothing was selected" in resp.content


# ---------------------------------------------------------------------------
# 3. The refusals.
# ---------------------------------------------------------------------------
def test_an_unknown_verb_is_rejected_outright(client):
    user = _user()
    a = _contact(user, "Ada")
    client.force_login(user)

    resp = client.post(reverse("crm:contacts_bulk"),
                       {"ids": [str(a.id)], "verb": "delete"})

    assert resp.status_code == 400
    a.refresh_from_db()
    assert a.archived is False
    assert Contact.all_objects.filter(pk=a.pk).exists()


def test_there_is_no_bulk_delete_verb():
    """Pinned deliberately. The product has no hard-delete path for a
    contact anywhere, and a multi-select is the worst place to add the
    first one — the mis-click that snoozes three people would erase
    eighty-three and their correspondence with them."""
    from crm.views import _BULK_VERBS

    assert "delete" not in _BULK_VERBS
    assert set(_BULK_VERBS) == {"snooze", "park", "archive"}


def test_a_get_is_not_allowed(client):
    user = _user()
    client.force_login(user)
    assert client.get(reverse("crm:contacts_bulk")).status_code == 405


def test_an_empty_selection_says_so_instead_of_erroring(client):
    user = _user()
    client.force_login(user)
    resp = _post(client, [], "archive")
    assert resp.status_code == 200
    assert b"Nothing was selected" in resp.content


def test_a_garbage_id_does_not_500(client):
    user = _user()
    a = _contact(user, "Ada")
    client.force_login(user)

    resp = client.post(
        reverse("crm:contacts_bulk"),
        {"ids": ["not-a-number", str(a.id)], "verb": "snooze"}, follow=True,
    )

    assert resp.status_code == 200
    a.refresh_from_db()
    assert a.snoozed_until is not None


# ---------------------------------------------------------------------------
# 4. It comes back to the board you were on.
# ---------------------------------------------------------------------------
def test_the_region_tab_survives_a_bulk_action(client):
    user = _user()
    a = _contact(user, "Ada")
    client.force_login(user)

    resp = _post(client, [a.id], "snooze", scope="hk")

    assert resp.redirect_chain
    assert resp.redirect_chain[-1][0].endswith("?scope=hk")


# ---------------------------------------------------------------------------
# 5. The board renders the controls.
# ---------------------------------------------------------------------------
def test_the_board_renders_checkboxes_and_the_bulk_bar(client):
    user = _user()
    firm = Firm.objects.create(slug="acme", name="Acme")
    c = _contact(user, "Ada", firm=firm)
    # Give the cadence engine a reason to queue her: contacted, no reply,
    # well past the follow-up window.
    Touch.all_objects.create(user=user, contact=c, kind="outreach",
                             channel="email",
                             ts=timezone.now() - timedelta(days=30))
    client.force_login(user)

    body = client.get(reverse("crm:contact_list")).content.decode()

    assert 'name="ids"' in body
    assert 'class="net-mini-check"' in body
    assert '<label class="net-mini"' in body  # the whole card toggles the box
    assert 'net-mini-open' in body            # and a real link still opens it
    assert 'data-bulk-bar' in body
    # Every offered verb, and no delete button anywhere on the page.
    assert 'value="snooze"' in body
    assert 'value="park"' in body
    assert 'value="archive"' in body
    assert 'value="delete"' not in body


# ---------------------------------------------------------------------------
# 6. The confirmation is actually seen.
#
# Found while testing the above: `base.html` never rendered the messages
# framework. Only three standalone pages (home, import, settings) carried
# their own copy of the loop, so every view that set a message and then
# REDIRECTED was writing into a void — `contact_archive` has always ended
# with "Archived {name}. They're in Archived Contacts if you want them
# back." and redirected to a board that dropped it on the floor. For a
# bulk action the confirmation is the whole safety story, so it is pinned.
# ---------------------------------------------------------------------------
def test_the_bulk_confirmation_reaches_the_page(client):
    user = _user()
    a = _contact(user, "Ada")
    b = _contact(user, "Bo")
    client.force_login(user)

    resp = _post(client, [a.id, b.id], "archive")
    body = resp.content.decode()

    assert "2 contacts archived" in body
    assert "Ada" in body and "Bo" in body
    # And the sentence that makes it safe to have clicked.
    assert "Archived Contacts if you want them back" in body


def test_a_single_contact_message_is_not_pluralised(client):
    user = _user()
    a = _contact(user, "Ada")
    client.force_login(user)

    body = _post(client, [a.id], "snooze").content.decode()

    assert "1 contact snoozed for 3 days" in body
    assert "1 contacts" not in body


def test_a_large_selection_is_counted_not_listed(client):
    """Naming eighty-three people is not a confirmation, it's a wall."""
    user = _user()
    ids = [_contact(user, f"Person {i:02d}").id for i in range(10)]
    client.force_login(user)

    body = _post(client, ids, "snooze").content.decode()

    assert "10 contacts snoozed" in body
    assert "and 7 more" in body


def test_the_single_archive_view_is_heard_too(client):
    """The pre-existing message this fix un-silenced."""
    user = _user()
    a = _contact(user, "Ada")
    client.force_login(user)

    resp = client.post(
        reverse("crm:contact_archive", args=[a.id]), follow=True)

    assert "Archived Ada" in resp.content.decode()
