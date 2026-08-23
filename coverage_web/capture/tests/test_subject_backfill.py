"""The historical subject backfill — `capture.subject_backfill` and its command.

The shapes here are the ones the founder's live account actually holds: 100 of
his 292 touches carry a `[gmail:...]` marker and 192 do not, and every one of
the 292 has a blank subject because the column postdates all of them. The tests
that matter are therefore about what the command REFUSES to do — it must never
overwrite a header-sourced subject, never invent a thread id for a row that has
none, and never write anything at all without `--commit`.

`transaction=True` is not needed: nothing here writes through `crm.services`'s
own psycopg connection (compare `test_reclassify_inbound.py`).
"""

from __future__ import annotations

import json
from datetime import timedelta
from io import StringIO

import pytest
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.utils import timezone

from capture import subject_backfill
from crm.models import Contact, Touch

pytestmark = pytest.mark.django_db

User = get_user_model()

ICC = "Fall 2026 ICC Alumni Digital Panel Outreach"


@pytest.fixture
def student(db):
    return User.objects.create_user(email="backfill@example.com", password="x")


def _contact(student, name):
    return Contact.all_objects.create(
        user=student, name=name, email=f"{name.split()[0].lower()}@firm.example",
        source="manual",
    )


def _touch(student, contact, *, note="", subject="", kind="outreach", days_ago=1):
    return Touch.all_objects.create(
        user=student, contact=contact, kind=kind, note=note, subject=subject,
        source="capture", ts=timezone.now() - timedelta(days=days_ago),
    )


def _mapping_file(tmp_path, payload):
    path = tmp_path / "subjects.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return str(path)


# --------------------------------------------------------------------------
# The mapping file
# --------------------------------------------------------------------------

def test_load_mapping_keeps_nulls_as_empty(tmp_path):
    path = _mapping_file(tmp_path, {"t1": ICC, "t2": None, "t3": "  padded  "})
    assert subject_backfill.load_mapping(path) == {
        "t1": ICC, "t2": "", "t3": "padded",
    }


def test_load_mapping_rejects_a_list(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(["t1", "t2"]), encoding="utf-8")
    with pytest.raises(ValueError, match="JSON object"):
        subject_backfill.load_mapping(str(path))


def test_load_mapping_rejects_a_non_string_subject(tmp_path):
    path = _mapping_file(tmp_path, {"t1": 17})
    with pytest.raises(ValueError, match="string or null"):
        subject_backfill.load_mapping(str(path))


def test_thread_ids_in_reads_every_distinct_marker():
    assert subject_backfill.thread_ids_in(
        "[gmail:aaa] sent [gmail:bbb] and [gmail:aaa] again"
    ) == ["aaa", "bbb"]
    assert subject_backfill.thread_ids_in("hand-logged coffee chat") == []
    assert subject_backfill.thread_ids_in(None) == []


# --------------------------------------------------------------------------
# The report
# --------------------------------------------------------------------------

def test_stamps_a_blank_subject_on_a_marked_touch(student):
    contact = _contact(student, "Nick Tehle")
    touch = _touch(student, contact, note="[gmail:19fbcd1fe5310001] Sent the panel ask")

    report = subject_backfill.build_report(student, {"19fbcd1fe5310001": ICC})

    assert [s.touch.id for s in report.stamps] == [touch.id]
    assert report.stamps[0].subject == ICC
    assert report.subject_counts == [(ICC, 1)]

    assert subject_backfill.commit(student, report) == 1
    touch.refresh_from_db()
    assert touch.subject == ICC


def test_refuses_to_overwrite_a_subject_that_is_already_there(student):
    contact = _contact(student, "Ellen Chung")
    touch = _touch(
        student, contact, note="[gmail:19fbcd1fe5310002] Sent the panel ask",
        subject="Subject the live sync stamped from the header",
    )

    report = subject_backfill.build_report(student, {"19fbcd1fe5310002": ICC})

    assert report.stamps == []
    assert [s.touch.id for s in report.already_stamped] == [touch.id]
    assert "never overwritten" in report.already_stamped[0].reason

    assert subject_backfill.commit(student, report) == 0
    touch.refresh_from_db()
    assert touch.subject == "Subject the live sync stamped from the header"


def test_ignores_a_touch_with_no_marker_and_says_why(student):
    contact = _contact(student, "Shelby Dibs")
    touch = _touch(student, contact, note="coffee chat, hand logged", kind="chat")

    report = subject_backfill.build_report(student, {"19fbcd1fe5310003": ICC})

    assert report.stamps == []
    assert [s.touch.id for s in report.unmarked] == [touch.id]
    assert "no [gmail:...] marker" in report.unmarked[0].reason
    assert report.unused_thread_ids == ["19fbcd1fe5310003"]

    assert subject_backfill.commit(student, report) == 0
    touch.refresh_from_db()
    assert touch.subject == ""


def test_a_marker_absent_from_the_mapping_is_not_guessed_at(student):
    contact = _contact(student, "Kristin Welty")
    touch = _touch(student, contact, note="[gmail:19aaaaaaaaaaaaaa] outreach sent")

    report = subject_backfill.build_report(student, {"19fbcd1fe5310001": ICC})

    assert report.stamps == []
    assert [s.touch.id for s in report.unmapped] == [touch.id]
    assert subject_backfill.commit(student, report) == 0


def test_a_thread_the_mailbox_no_longer_resolves_is_reported_apart(student):
    contact = _contact(student, "Ayda Yang")
    touch = _touch(student, contact, note="[gmail:19bbbbbbbbbbbbbb] outreach sent")

    report = subject_backfill.build_report(student, {"19bbbbbbbbbbbbbb": None})

    assert report.stamps == [] and report.unmapped == []
    assert [s.touch.id for s in report.unresolvable] == [touch.id]
    assert "unresolvable" in report.unresolvable[0].reason


def test_conflicting_markers_are_skipped_rather_than_resolved(student):
    contact = _contact(student, "Brooke Baker")
    touch = _touch(student, contact, note="[gmail:19cccccccccccccc] merged [gmail:19dddddddddddddd] note")

    report = subject_backfill.build_report(
        student, {"19cccccccccccccc": ICC, "19dddddddddddddd": "Something else entirely"}
    )

    assert report.stamps == []
    assert [s.touch.id for s in report.ambiguous] == [touch.id]
    assert subject_backfill.commit(student, report) == 0


def test_the_report_never_leaves_this_users_tenant(student):
    other = User.objects.create_user(email="other@example.com", password="x")
    theirs = Contact.all_objects.create(
        user=other, name="Someone Else", email="s@firm.example", source="manual",
    )
    their_touch = Touch.all_objects.create(
        user=other, contact=theirs, kind="outreach",
        note="[gmail:19fbcd1fe5310001] Sent the panel ask", source="capture",
        ts=timezone.now(),
    )

    report = subject_backfill.build_report(student, {"19fbcd1fe5310001": ICC})

    assert report.touches_seen == 0 and report.stamps == []
    subject_backfill.commit(student, report)
    their_touch.refresh_from_db()
    assert their_touch.subject == ""


def test_a_merge_shows_up_as_one_subject_across_many_rows(student):
    """The sanity check the operator actually reads before committing."""
    for i in range(12):
        contact = _contact(student, f"Alum{i} Panelist")
        _touch(
            student, contact,
            note=f"[gmail:19fbcd1fe531{i:04d}] Jimmy asked them to speak on the panel",
        )
    mapping = {f"19fbcd1fe531{i:04d}": ICC for i in range(12)}

    report = subject_backfill.build_report(student, mapping)

    assert report.subject_counts == [(ICC, 12)]


# --------------------------------------------------------------------------
# The command
# --------------------------------------------------------------------------

def _run(*args, **kwargs):
    out = StringIO()
    call_command("backfill_touch_subjects", *args, stdout=out, **kwargs)
    return out.getvalue()


def test_command_report_mode_writes_nothing(student, tmp_path):
    contact = _contact(student, "Nick Tehle")
    touch = _touch(student, contact, note="[gmail:19fbcd1fe5310001] Sent the panel ask")
    path = _mapping_file(tmp_path, {"19fbcd1fe5310001": ICC})

    out = _run("--email", student.email, "--mapping", path)

    assert "[report only]" in out
    assert "1 touch(es) would be stamped" in out
    assert "Nothing was written" in out
    touch.refresh_from_db()
    assert touch.subject == ""


def test_command_commit_writes(student, tmp_path):
    contact = _contact(student, "Nick Tehle")
    touch = _touch(student, contact, note="[gmail:19fbcd1fe5310001] Sent the panel ask")
    path = _mapping_file(tmp_path, {"19fbcd1fe5310001": ICC})

    out = _run("--email", student.email, "--mapping", path, "--commit")

    assert "1 touch(es) stamped" in out
    assert "[report only]" not in out
    touch.refresh_from_db()
    assert touch.subject == ICC


def test_command_commit_is_idempotent(student, tmp_path):
    contact = _contact(student, "Nick Tehle")
    touch = _touch(student, contact, note="[gmail:19fbcd1fe5310001] Sent the panel ask")
    path = _mapping_file(tmp_path, {"19fbcd1fe5310001": ICC})

    _run("--email", student.email, "--mapping", path, "--commit")
    out = _run("--email", student.email, "--mapping", path, "--commit")

    assert "0 touch(es) stamped" in out
    assert "1 left alone as already stamped" in out
    touch.refresh_from_db()
    assert touch.subject == ICC


def test_command_reports_the_unstampable_ceiling(student, tmp_path):
    contact = _contact(student, "Shelby Dibs")
    _touch(student, contact, note="hand logged, no thread", kind="chat")
    path = _mapping_file(tmp_path, {"19fbcd1fe5310001": ICC})

    out = _run("--email", student.email, "--mapping", path)

    assert "NO GMAIL MARKER" in out
    assert "1 carry no marker at all" in out


def test_command_rejects_an_unknown_user(tmp_path):
    path = _mapping_file(tmp_path, {"19fbcd1fe5310001": ICC})
    with pytest.raises(CommandError, match="No user with email"):
        _run("--email", "nobody@example.com", "--mapping", path)


def test_command_rejects_an_unreadable_mapping(student):
    with pytest.raises(CommandError, match="Could not read --mapping"):
        _run("--email", student.email, "--mapping", "/nope/missing.json")
