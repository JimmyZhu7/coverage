"""Tests for purge_test_contacts — report-only by default, and both fixture
guards hold even when a row is named explicitly via --ids.

These build the live shapes: the four archived "ZZZ ..." rows that smoke runs
left in the founder's CRM, plus a real archived contact and a live un-archived
one alongside them, because the whole risk of this command is that it reaches
past its intended targets."""

from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.utils import timezone

from crm.models import Contact, Touch

User = get_user_model()


def _user(email="purge@example.com"):
    return User.objects.create_user(email=email, password="x")


def _fixture_row(user, name="ZZZ Smoke Test Contact", **kw):
    kw.setdefault("archived", True)
    kw.setdefault("source", "automated smoke test")
    kw.setdefault("warmth", "advocate")
    return Contact.all_objects.create(user=user, name=name, **kw)


@pytest.mark.django_db
def test_dry_run_reports_the_cascade_but_deletes_nothing(capsys):
    user = _user()
    row = _fixture_row(user)
    Touch.all_objects.create(
        user=user, contact=row, kind="manual_override", ts=timezone.now(),
        note="manual override: warmth=advocate — cutover",
    )
    Touch.all_objects.create(
        user=user, contact=row, kind="chat", ts=timezone.now(), note="",
    )

    call_command("purge_test_contacts", ids=[row.id])
    out = capsys.readouterr().out

    assert "[dry-run]" in out
    assert "would be deleted" in out
    # The report has to name the touches, or the owner is approving a delete
    # whose real size they cannot see.
    assert "cascade: touch" in out
    assert "2 touch(es)" in out

    assert Contact.all_objects.filter(id=row.id).exists()
    assert Touch.all_objects.filter(contact=row).count() == 2


@pytest.mark.django_db
def test_apply_deletes_the_fixture_and_its_touches():
    user = _user()
    row = _fixture_row(user)
    Touch.all_objects.create(
        user=user, contact=row, kind="chat", ts=timezone.now(), note="",
    )

    call_command("purge_test_contacts", ids=[row.id], apply=True)

    assert not Contact.all_objects.filter(id=row.id).exists()
    assert not Touch.all_objects.filter(contact_id=row.id).exists()


@pytest.mark.django_db
def test_a_real_persons_id_is_refused_even_with_apply(capsys):
    """The failure this guard exists for: an id typed one digit wrong lands on
    somebody's actual contact. Archived is not enough on its own — 21 of the
    live archived rows are real people."""
    user = _user()
    real = Contact.all_objects.create(
        user=user, name="James Bai", archived=True, source="import",
    )
    touch = Touch.all_objects.create(
        user=user, contact=real, kind="reply_received", ts=timezone.now(), note="",
    )

    call_command("purge_test_contacts", ids=[real.id], apply=True)
    out = capsys.readouterr().out

    assert "REFUSED" in out
    assert Contact.all_objects.filter(id=real.id).exists()
    assert Touch.all_objects.filter(id=touch.id).exists()


@pytest.mark.django_db
def test_an_unarchived_zzz_row_is_refused(capsys):
    """A fixture that is live in the Network board is a different situation
    from one already tidied away: something un-archived it, or a smoke run is
    mid-flight. Report it, do not delete it."""
    user = _user()
    live = _fixture_row(user, name="ZZZ Smoke Test Add Contact", archived=False)

    call_command("purge_test_contacts", ids=[live.id], apply=True)
    out = capsys.readouterr().out

    assert "REFUSED" in out
    assert Contact.all_objects.filter(id=live.id).exists()


@pytest.mark.django_db
def test_neighbouring_rows_are_untouched():
    """Purging the fixtures must leave the archived list's real population and
    the live Network rows exactly where they were."""
    user = _user()
    doomed = _fixture_row(user)
    archived_person = Contact.all_objects.create(
        user=user, name="Ellen Chung", archived=True,
    )
    live_person = Contact.all_objects.create(user=user, name="Jeffrey Tong")

    call_command("purge_test_contacts", ids=[doomed.id], apply=True)

    assert set(Contact.all_objects.values_list("id", flat=True)) == {
        archived_person.id, live_person.id,
    }


@pytest.mark.django_db
def test_a_missing_id_is_reported_not_crashed(capsys):
    _user()
    call_command("purge_test_contacts", ids=[999999])
    out = capsys.readouterr().out
    assert "no such contact" in out
    assert "Nothing to purge." in out
