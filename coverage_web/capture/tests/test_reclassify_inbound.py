"""The historical reclassifier — `capture.reclassify` and its command.

Every scenario here is a shape that actually exists on the founder's live
account, named after the person it came from, because the four conditions
in `capture.reclassify` were each added to stop one of these from being
demoted wrongly:

  - Caroline Baenen  — a blast from a recruiter he never wrote to. Demote.
  - Kristin Welty    — a genuine reply, imported. Never demote.
  - Nick Tehle       — a genuine reply whose outreach touch carries no
                       thread marker. Never demote (this is why the
                       outbound check is per-contact, not per-thread).
  - Shelby Dibs      — a genuine reply from before Coverage existed, so
                       there is no outreach touch at all. Held back by the
                       source check.
  - Ellen Chung      — already hand-corrected. Held back by the override
                       check, and the override must survive.

`transaction=True`: `commit_plan` writes through `crm.services`, which
opens its own psycopg connection (see `test_gmail.py`'s module docstring).
"""

from __future__ import annotations

from datetime import timedelta
from io import StringIO

import pytest
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.utils import timezone

from capture import reclassify
from coverage_domain.pipeline import BULK_RECEIVED_KIND, MANUAL_OVERRIDE_KIND
from crm.models import Contact, Touch

pytestmark = pytest.mark.django_db(transaction=True)

User = get_user_model()

BLAST_NOTE = (
    "[gmail:19ff69462ba31125] As a participant in West Monroe's Sophomore "
    "Series, we'd love to invite you to an exclusive conversation with USC "
    "alumni at West Monroe before applications open."
)


@pytest.fixture
def student(db):
    return User.objects.create_user(email="reclass@example.com", password="x")


def _contact(student, name, *, warmth="cold", thread_state="no_reply", **over):
    return Contact.all_objects.create(
        user=student, name=name, email=f"{name.split()[0].lower()}@firm.example",
        source="manual", warmth=warmth, thread_state=thread_state, **over,
    )


def _touch(student, contact, kind, *, note="", source="capture", days_ago=1):
    return Touch.all_objects.create(
        user=student, contact=contact, kind=kind, channel="email",
        note=note, source=source,
        ts=timezone.now() - timedelta(days=days_ago),
    )


class TestEvidenceReading:
    def test_the_discovery_placeholder_counts_as_no_evidence(self):
        is_bulk, why = reclassify.looks_like_bulk_evidence(
            "Discovered by mailbox scan"
        )
        assert is_bulk is True
        assert "placeholder" in why

    def test_the_west_monroe_blast_reads_as_mass_mailing(self):
        is_bulk, why = reclassify.looks_like_bulk_evidence(BLAST_NOTE)
        assert is_bulk is True
        assert "mass-mailing" in why

    @pytest.mark.parametrize("note", [
        "[gmail:abc] Kristin Welty (Deloitte) replied same day, looped in "
        "campus recruiter Myra Fernandez.",
        "[gmail:abc] Nick replied saying he is happy to help with a panel or "
        "mentorship program, but only for events before 10pm ET.",
        "[gmail:abc] Shelby Dibs (KPMG) replied: thanks for reaching out, "
        "recruiting for Advisory roles starting in September.",
        "Replied 2026-07-21 thanking Jimmy for reaching out; wants to "
        "revisit panel opportunities closer to fall.",
    ])
    def test_real_reply_summaries_are_not_bulk(self, note):
        assert reclassify.looks_like_bulk_evidence(note)[0] is False


class TestBuildReport:
    def test_a_blast_from_someone_never_written_to_is_demoted(self, student):
        caroline = _contact(
            student, "Caroline Baenen", warmth="replied", thread_state="replied"
        )
        _touch(student, caroline, "reply_received", note=BLAST_NOTE)

        report = reclassify.build_report(student)
        assert report.demoted_touches == 1
        plan = report.plans[0]
        assert plan.contact.id == caroline.id
        assert plan.new_warmth == "cold"
        assert plan.new_thread_state == "no_reply"

    def test_a_genuine_reply_with_outreach_is_left_alone(self, student):
        """Nick Tehle: outreach exists on the contact but carries no thread
        marker, so a per-thread outbound check would have demoted him."""
        nick = _contact(
            student, "Nick Tehle", warmth="replied", thread_state="replied"
        )
        _touch(student, nick, "outreach", note="Discovered by mailbox scan",
               days_ago=3)
        _touch(student, nick, "reply_received",
               note="[gmail:19f2e6ef0c479dc0] Nick replied saying he is happy "
                    "to help with a panel.", days_ago=2)

        report = reclassify.build_report(student)
        assert report.demoted_touches == 0

    def test_an_imported_reply_is_left_alone_even_with_no_outreach(self, student):
        """Shelby Dibs: real reply, no outreach touch (it predates
        Coverage), placeholder-free note. The source check is what saves
        her from the never-written-to rule."""
        shelby = _contact(
            student, "Shelby Dibs", warmth="replied", thread_state="replied"
        )
        _touch(student, shelby, "reply_received",
               note="Discovered by mailbox scan", source="import")

        report = reclassify.build_report(student)
        assert report.demoted_touches == 0
        assert len(report.flagged) == 1
        assert "'import'" in report.flagged[0].reason

    def test_a_manual_override_survives_reclassification(self, student):
        """The hard constraint: a human correction outranks any automated
        judgement. Ellen Chung's shape — the founder hand-fixing a
        mislabelled scan result — must come out of a run untouched."""
        ellen = _contact(
            student, "Ellen Chung", warmth="replied", thread_state="replied"
        )
        bad = _touch(student, ellen, "reply_received",
                     note="Discovered by mailbox scan", days_ago=5)
        override = _touch(
            student, ellen, MANUAL_OVERRIDE_KIND, source="manual", days_ago=1,
            note="manual override: warmth=replied, thread_state=replied — "
                 "Correction: discovery scan mislabeled an email reply.",
        )

        report = reclassify.build_report(student)
        assert report.demoted_touches == 0
        assert len(report.flagged) == 1
        assert "human judgement wins" in report.flagged[0].reason

        # And it stays untouched through a real commit run.
        for plan in report.plans:
            reclassify.commit_plan(student, plan)
        bad.refresh_from_db()
        override.refresh_from_db()
        ellen.refresh_from_db()
        assert bad.kind == "reply_received"
        assert override.kind == MANUAL_OVERRIDE_KIND
        assert ellen.warmth == "replied"
        assert ellen.thread_state == "replied"

    def test_warmth_is_recomputed_from_what_remains_not_reset(self, student):
        """A contact with a real chat AND a blast keeps the chat's warmth.
        Demoting one touch must not flatten the whole record."""
        mixed = _contact(
            student, "Jane Banker", warmth="chatted", thread_state="chat_done"
        )
        _touch(student, mixed, "chat", note="Coffee chat at their office",
               days_ago=10)
        _touch(student, mixed, "reply_received", note=BLAST_NOTE, days_ago=2)

        report = reclassify.build_report(student)
        assert report.demoted_touches == 1
        plan = report.plans[0]
        assert plan.state_changes is False

    def test_tenancy_another_users_touches_are_invisible(self, student, db):
        other = User.objects.create_user(email="other@example.com", password="x")
        theirs = Contact.all_objects.create(
            user=other, name="Caroline Baenen", email="c@w.example",
            source="manual", warmth="replied", thread_state="replied",
        )
        Touch.all_objects.create(
            user=other, contact=theirs, kind="reply_received", channel="email",
            note=BLAST_NOTE, source="capture", ts=timezone.now(),
        )
        report = reclassify.build_report(student)
        assert report.contacts_seen == 0
        assert report.demoted_touches == 0


class TestCommit:
    def test_commit_demotes_the_touch_and_keeps_the_evidence(self, student):
        caroline = _contact(
            student, "Caroline Baenen", warmth="replied", thread_state="replied"
        )
        touch = _touch(student, caroline, "reply_received", note=BLAST_NOTE)

        report = reclassify.build_report(student)
        for plan in report.plans:
            reclassify.commit_plan(student, plan)

        touch.refresh_from_db()
        caroline.refresh_from_db()
        assert touch.kind == BULK_RECEIVED_KIND
        # Nothing discarded: the original evidence is still there, with the
        # reason appended.
        assert "Sophomore Series" in touch.note
        assert "reclassified" in touch.note
        assert caroline.warmth == "cold"
        assert caroline.thread_state == "no_reply"
        # The state change left its own audit row.
        assert Touch.objects.for_user(student).filter(
            contact=caroline, kind=MANUAL_OVERRIDE_KIND
        ).exists()

    def test_a_second_run_changes_nothing(self, student):
        caroline = _contact(
            student, "Caroline Baenen", warmth="replied", thread_state="replied"
        )
        _touch(student, caroline, "reply_received", note=BLAST_NOTE)
        for plan in reclassify.build_report(student).plans:
            reclassify.commit_plan(student, plan)

        assert reclassify.build_report(student).demoted_touches == 0


class TestCommand:
    def _run(self, student, *args):
        out = StringIO()
        call_command(
            "reclassify_inbound_touches", "--email", student.email,
            *args, stdout=out,
        )
        return out.getvalue()

    def test_report_only_is_the_default_and_writes_nothing(self, student):
        caroline = _contact(
            student, "Caroline Baenen", warmth="replied", thread_state="replied"
        )
        touch = _touch(student, caroline, "reply_received", note=BLAST_NOTE)

        output = self._run(student)

        assert "report only" in output
        assert "Caroline Baenen" in output
        assert "reply_received -> bulk_received" in output
        assert "Re-run with --commit to apply" in output

        touch.refresh_from_db()
        caroline.refresh_from_db()
        assert touch.kind == "reply_received"
        assert caroline.warmth == "replied"

    def test_commit_writes(self, student):
        caroline = _contact(
            student, "Caroline Baenen", warmth="replied", thread_state="replied"
        )
        touch = _touch(student, caroline, "reply_received", note=BLAST_NOTE)

        self._run(student, "--commit")

        touch.refresh_from_db()
        caroline.refresh_from_db()
        assert touch.kind == BULK_RECEIVED_KIND
        assert caroline.warmth == "cold"

    def test_unknown_email_is_an_error_not_a_silent_no_op(self, student):
        from django.core.management.base import CommandError

        with pytest.raises(CommandError):
            call_command(
                "reclassify_inbound_touches", "--email", "nobody@example.com",
                stdout=StringIO(),
            )
