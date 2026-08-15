"""Tests for scrub_manual_override_notes — report-only by default, only
rewrites the touches named in DEFAULT_REPLACEMENTS / --ids."""

from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.utils import timezone

from crm.models import Contact, Touch

User = get_user_model()


def _user(email="scrub@example.com"):
    return User.objects.create_user(email=email, password="x")


@pytest.mark.django_db
def test_dry_run_reports_but_does_not_write(capsys):
    user = _user()
    contact = Contact.all_objects.create(user=user, name="James Bai")
    touch = Touch.all_objects.create(
        user=user, contact=contact, kind="manual_override", ts=timezone.now(),
        note=("manual override: thread_state=chat_done — Correction: a "
              "capture_gmail test batch mistakenly logged a duplicate "
              "chat_scheduled..."),
    )

    call_command("scrub_manual_override_notes", ids=[touch.id])
    out = capsys.readouterr().out
    assert "[dry-run]" in out
    assert "would be rewritten" in out

    touch.refresh_from_db()
    assert "capture_gmail" in touch.note  # untouched without --apply


@pytest.mark.django_db
def test_an_id_outside_default_replacements_is_reported_and_left_alone():
    """--ids can target any manual_override touch, but a row with no entry
    in DEFAULT_REPLACEMENTS has nothing to rewrite it TO — it is reported
    as skipped, never guessed at."""
    user = _user()
    contact = Contact.all_objects.create(user=user, name="James Bai")
    touch = Touch.all_objects.create(
        user=user, contact=contact, kind="manual_override", ts=timezone.now(),
        note="manual override: warmth=advocate",
    )

    call_command("scrub_manual_override_notes", ids=[touch.id], apply=True)

    touch.refresh_from_db()
    assert touch.note == "manual override: warmth=advocate"


@pytest.mark.django_db
def test_apply_with_matching_default_id_rewrites_the_note(monkeypatch):
    """A touch whose id happens to be one of DEFAULT_REPLACEMENTS' keys gets
    its note rewritten under --apply; capture_gmail/campaign.db language is
    gone from the stored row afterward."""
    from crm.management.commands import scrub_manual_override_notes as cmd_mod

    user = _user()
    contact = Contact.all_objects.create(user=user, name="James Bai")
    touch = Touch.all_objects.create(
        user=user, contact=contact, kind="manual_override", ts=timezone.now(),
        note=("manual override: thread_state=chat_done — Correction: a "
              "capture_gmail test batch mistakenly logged a duplicate "
              "chat_scheduled..."),
    )
    monkeypatch.setattr(
        cmd_mod, "DEFAULT_REPLACEMENTS",
        {touch.id: ("manual override: thread_state=chat_done — Correction: "
                    "an email sync logged a duplicate notification in error.")},
    )

    call_command("scrub_manual_override_notes", apply=True)

    touch.refresh_from_db()
    assert "capture_gmail" not in touch.note
    assert touch.note == (
        "manual override: thread_state=chat_done — Correction: an email "
        "sync logged a duplicate notification in error.")


@pytest.mark.django_db
def test_touch_358s_replacement_is_a_real_rewrite_not_a_no_op():
    """PINS the fix: the first DEFAULT_REPLACEMENTS entry ever written for
    touch #358 restated the exact ops-voice text already stored in the live
    DB verbatim — a live dry-run printed "already matches; skipped" and
    --apply would have changed nothing. The replacement must now actually
    differ from the pre-fix live text, and must drop the internal
    vocabulary ("email sync", "re-threaded") that survived
    crm.views._display_note's prefix-strip."""
    from crm.management.commands.scrub_manual_override_notes import (
        DEFAULT_REPLACEMENTS,
    )

    live_text_before_this_fix = (
        "manual override: thread_state=chat_done — Correction: an email "
        "sync mistakenly logged a duplicate scheduled-chat notification "
        "from a re-threaded email conversation about this same, "
        "already-completed call. Restoring the correct status."
    )
    replacement = DEFAULT_REPLACEMENTS[358]

    assert replacement != live_text_before_this_fix
    assert "email sync" not in replacement
    assert "re-threaded" not in replacement
    assert "capture_gmail" not in replacement


@pytest.mark.django_db
def test_an_id_with_no_replacement_text_is_left_alone():
    user = _user()
    contact = Contact.all_objects.create(user=user, name="Someone Else")
    touch = Touch.all_objects.create(
        user=user, contact=contact, kind="manual_override", ts=timezone.now(),
        note="manual override: warmth=hot",
    )

    call_command("scrub_manual_override_notes", ids=[touch.id], apply=True)

    touch.refresh_from_db()
    assert touch.note == "manual override: warmth=hot"
