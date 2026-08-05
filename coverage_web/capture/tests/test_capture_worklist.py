"""The sync's worklist, and why the window is per contact.

A two-day search window is right for someone Coverage has been watching and
wrong for someone who just arrived carrying months of history. It reported
the second as the first: Cindy So was added on 1 August with a July thread in
which she had replied, agreed a time and confirmed it, and every sync after
that looked at the last two days only, found nothing, and left her at zero
touches — so Today told the owner to send her a first note.
"""

from __future__ import annotations

from io import StringIO

import pytest
from django.core.management import call_command
from django.utils import timezone

from capture.management.commands.capture_worklist import BACKFILL_DAYS
from crm.models import Contact, Touch

pytestmark = pytest.mark.django_db


@pytest.fixture
def user(django_user_model):
    return django_user_model.objects.create_user(
        email="sync@example.com", password="x", capture_slug="syncslug123"
    )


def _run(user, window=2):
    out = StringIO()
    call_command("capture_worklist", email=user.email, window=window, stdout=out)
    rows = [line.split("|") for line in out.getvalue().splitlines() if line.strip()]
    return {r[1]: int(r[4]) for r in rows}


def _touch(user, contact):
    Touch.all_objects.create(user=user, contact=contact, kind="outreach",
                             channel="email", ts=timezone.now())


def test_a_contact_with_no_history_gets_a_full_first_scan(user):
    Contact.all_objects.create(user=user, name="Cindy So", email="c@ms.com")
    assert _run(user)["Cindy So"] == BACKFILL_DAYS


def test_a_contact_coverage_already_watches_gets_the_normal_window(user):
    c = Contact.all_objects.create(user=user, name="Known Person", email="k@x.com")
    _touch(user, c)
    assert _run(user, window=2)["Known Person"] == 2


def test_the_first_touch_found_drops_them_to_the_normal_window(user):
    """Self-limiting: the deep scan happens once, then never again."""
    c = Contact.all_objects.create(user=user, name="Cindy So", email="c@ms.com")
    assert _run(user)["Cindy So"] == BACKFILL_DAYS
    _touch(user, c)
    assert _run(user)["Cindy So"] == 2


def test_a_missed_run_still_widens_the_normal_window(user):
    """The ledger-sized window is passed straight through, so a three-day gap
    is covered for everyone — the backfill rule adds to that, never replaces
    it."""
    c = Contact.all_objects.create(user=user, name="Known Person", email="k@x.com")
    _touch(user, c)
    assert _run(user, window=9)["Known Person"] == 9


def test_warm_relationships_are_not_re_checked(user):
    """Once someone is chatted or an advocate, an automated re-check earns
    nothing — the rule the skill file used to state and nothing enforced."""
    Contact.all_objects.create(user=user, name="Chatted Already", warmth="chatted")
    Contact.all_objects.create(user=user, name="An Advocate", warmth="advocate")
    Contact.all_objects.create(user=user, name="Still Cold", warmth="cold")
    assert set(_run(user)) == {"Still Cold"}


def test_archived_people_are_not_re_checked(user):
    Contact.all_objects.create(user=user, name="Gone", archived=True)
    assert _run(user) == {}


def test_only_your_own_contacts_are_listed(user, django_user_model):
    other = django_user_model.objects.create_user(
        email="other@example.com", password="x", capture_slug="otherslug99")
    Contact.all_objects.create(user=other, name="Theirs")
    Contact.all_objects.create(user=user, name="Mine")
    assert set(_run(user)) == {"Mine"}
