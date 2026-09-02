"""The two repair commands from the 2026-09-01 CRM lifecycle audit.

Both default to a dry run and both write only under `--apply`, which is the
property most worth pinning: they exist to be pointed at a real student's
account, and the first thing anybody does with them is look.

`transaction=True`: `replay_states --apply` goes through `crm.services`,
which opens its own psycopg connection outside Django's test transaction.
"""

from __future__ import annotations

from datetime import timedelta
from io import StringIO

import pytest
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.utils import timezone

from crm.models import Contact, Touch
from directory.models import Firm

pytestmark = pytest.mark.django_db(transaction=True)


def _user(email="cmd@example.com", **kw):
    return get_user_model().objects.create_user(email=email, password="pw12345!", **kw)


def _touch(user, contact, kind, *, days_ago=0, note=None, source="manual"):
    return Touch.all_objects.create(
        user=user, contact=contact, kind=kind,
        channel=None if kind == "manual_override" else "email",
        note=note, source=source,
        ts=timezone.now() - timedelta(days=days_ago),
    )


def _run(command, **opts):
    out = StringIO()
    call_command(command, stdout=out, **opts)
    return out.getvalue()


# ---------------------------------------------------------------------------
# replay_states
# ---------------------------------------------------------------------------
def _drifted(user):
    """A contact in exactly the shape the write-order ratchet produced:
    parked in August, then a July reply written afterwards moved the state
    while the ledger, read in date order, still ends at the park."""
    contact = Contact.all_objects.create(
        user=user, name="Nicole Park", warmth="chatted", thread_state="replied",
    )
    _touch(user, contact, "outreach", days_ago=60)
    _touch(user, contact, "chat", days_ago=58, source="capture")
    _touch(
        user, contact, "manual_override", days_ago=20,
        note="manual override: thread_state=parked — Parked from the Network board (bulk)",
    )
    # Dated BEFORE the park, written after it.
    _touch(user, contact, "reply_received", days_ago=55, source="capture")
    return contact


def test_the_dry_run_reports_the_drift_and_writes_nothing():
    user = _user()
    contact = _drifted(user)
    before = Touch.all_objects.filter(user=user).count()

    out = _run("replay_states", email=user.email)

    assert "Nicole Park" in out
    assert "chatted/replied" in out and "chatted/parked" in out
    assert "would be repaired by --apply" in out
    contact.refresh_from_db()
    assert contact.thread_state == "replied"
    assert Touch.all_objects.filter(user=user).count() == before


def test_apply_writes_the_replayed_state_through_the_audited_path():
    user = _user()
    contact = _drifted(user)

    _run("replay_states", email=user.email, apply=True)

    contact.refresh_from_db()
    assert contact.thread_state == "parked"
    audit = Touch.all_objects.filter(
        user=user, contact=contact, kind="manual_override", source="replay"
    )
    # One row per changed contact, saying why — never a silent .save().
    assert audit.count() == 1
    assert "replayed from your own touch history" in audit.get().note


def test_a_contact_that_agrees_with_its_ledger_is_left_alone():
    user = _user()
    contact = Contact.all_objects.create(
        user=user, name="Agrees", warmth="replied", thread_state="replied",
    )
    _touch(user, contact, "outreach", days_ago=10)
    _touch(user, contact, "reply_received", days_ago=5)

    out = _run("replay_states", email=user.email, apply=True)

    assert "agree with their own ledger" in out
    assert not Touch.all_objects.filter(
        user=user, contact=contact, kind="manual_override"
    ).exists()


def test_the_replay_is_idempotent():
    """After a repair the ledger's newest event IS the replay's own override,
    so a second run has nothing to say. A command that keeps finding the same
    drift it just fixed is a command nobody can trust to be run twice."""
    user = _user()
    _drifted(user)
    _run("replay_states", email=user.email, apply=True)
    out = _run("replay_states", email=user.email)
    assert "All 1 contact(s) agree with their own ledger." in out


def test_replay_is_scoped_to_the_named_account():
    mine = _user("mine@example.com")
    theirs = _user("theirs@example.com")
    not_mine = _drifted(theirs)

    _run("replay_states", email=mine.email, apply=True)

    not_mine.refresh_from_db()
    assert not_mine.thread_state == "replied"


# ---------------------------------------------------------------------------
# fix_school_firms
# ---------------------------------------------------------------------------
@pytest.fixture
def alum_setup():
    user = _user("usc@example.com", school="University of Southern California",
                 school_emails=["someone@usc.edu"])
    bain = Firm.objects.create(slug="bain", name="Bain & Company",
                               domains=["bain.com"])
    alum = Contact.all_objects.create(
        user=user, name="Nicole Park", email="nicole@bain.com",
        firm_text="usc", source="capture",
    )
    stays = Contact.all_objects.create(
        user=user, name="No Match", email="someone@nowhere.example",
        firm_text="usc", source="capture",
    )
    return user, bain, alum, stays


def test_the_dry_run_names_the_rows_and_writes_nothing(alum_setup):
    user, bain, alum, _ = alum_setup

    out = _run("fix_school_firms", email=user.email)

    assert "Nicole Park" in out
    assert "Bain & Company" in out
    assert "would change" in out
    alum.refresh_from_db()
    assert alum.firm_id is None
    assert alum.firm_text == "usc"


def test_apply_refiles_the_alum_under_their_employer(alum_setup):
    user, bain, alum, stays = alum_setup

    _run("fix_school_firms", email=user.email, apply=True)

    alum.refresh_from_db()
    assert alum.firm_id == bain.id
    assert alum.firm_text == ""
    assert alum.school_affiliation is True
    assert alum.school == "usc"
    # The row whose address resolves to nothing is untouched — "leave it
    # alone" is what no evidence means.
    stays.refresh_from_db()
    assert stays.firm_id is None
    assert stays.firm_text == "usc"


def test_apply_is_idempotent(alum_setup):
    user, _, _, _ = alum_setup
    _run("fix_school_firms", email=user.email, apply=True)
    out = _run("fix_school_firms", email=user.email)
    assert "No contact is filed at a school" in out
