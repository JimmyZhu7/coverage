"""reclassify_inbound_touches — re-judge `reply_received` touches already in
the database, now that "inbound" and "replied to me" are two different
things (report-only by default).

    python manage.py reclassify_inbound_touches --email you@example.com
    python manage.py reclassify_inbound_touches --email you@example.com --commit

WHY THIS EXISTS
---------------
Until `capture.inbound` landed, every inbound, non-bouncing Gmail message
became a `reply_received` touch, which ratcheted warmth to `replied` and
thread_state to `replied`, which the Today queue then read as a warm
relationship worth asking for a coffee chat. A mass programme invitation
from a recruiter the owner had never written to is not that. The classifier
fixes every message from here on; this command is for the rows already
written.

REPORT-ONLY BY DEFAULT, and the reason is not caution for its own sake
----------------------------------------------------------------------
This command lowers people's warmth. Getting that wrong in the other
direction — demoting someone who really did write back — silences a real
relationship, and unlike the noise it is fixing, a silence is invisible.
So nothing is written without `--commit`, the report prints every
individual touch it would change AND every touch it deliberately held back,
and `capture.reclassify`'s four conditions are all conjunctive. Matching
this repo's read-only-DB-by-default rule (see
`crm/management/commands/scrub_manual_override_notes.py`, which spells the
same flag `--apply`; both spellings are accepted here so neither habit
fails silently).

Live network: none. Live database: read-only unless `--commit`.
"""

from __future__ import annotations

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from capture import reclassify


class Command(BaseCommand):
    help = (
        "Re-judge existing reply_received touches that were really bulk or "
        "automated mail. Report-only unless --commit."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--email", required=True,
            help="Whose CRM to examine. Always scoped to one user.",
        )
        parser.add_argument(
            "--commit", "--apply", action="store_true", default=False,
            dest="commit",
            help="Write the reclassification. Default is report-only.",
        )

    def handle(self, *args, **opts):
        User = get_user_model()
        try:
            user = User.objects.get(email__iexact=opts["email"])
        except User.DoesNotExist as exc:
            raise CommandError(f"No user with email {opts['email']!r}") from exc

        commit = opts["commit"]
        tag = "" if commit else "[report only] "

        report = reclassify.build_report(user)

        self.stdout.write(
            f"{tag}{user.email}: {report.contacts_seen} contacts, "
            f"{report.reply_touches_seen} reply_received touches examined."
        )
        self.stdout.write("")

        if report.plans:
            self.stdout.write(self.style.MIGRATE_HEADING(
                "WOULD RECLASSIFY as bulk/automated (not a reply):"
                if not commit else
                "RECLASSIFIED as bulk/automated (not a reply):"
            ))
            for plan in report.plans:
                contact = plan.contact
                self.stdout.write(
                    f"\n  #{contact.id} {contact.name} <{contact.email or '—'}>"
                    f"  [{contact.firm or contact.firm_text or 'no firm'}]"
                )
                for verdict in plan.demotions:
                    touch = verdict.touch
                    self.stdout.write(
                        f"      touch #{touch.id} {touch.ts:%Y-%m-%d} "
                        f"reply_received -> bulk_received"
                    )
                    self.stdout.write(f"        why:  {verdict.reason}")
                    self.stdout.write(f"        note: {(touch.note or '')[:200]!r}")
                if plan.state_changes:
                    self.stdout.write(
                        "        contact: "
                        f"warmth {contact.warmth} -> "
                        f"{plan.new_warmth or contact.warmth}, "
                        f"thread_state {contact.thread_state} -> "
                        f"{plan.new_thread_state or contact.thread_state}"
                    )
                else:
                    self.stdout.write(
                        "        contact: warmth and status unchanged "
                        "(other evidence still supports them)"
                    )
        else:
            self.stdout.write("Nothing to reclassify.")

        if report.flagged:
            self.stdout.write("")
            self.stdout.write(self.style.MIGRATE_HEADING(
                "LOOKS BULK, LEFT ALONE — worth your eyes, not this command's:"
            ))
            for verdict in report.flagged:
                touch = verdict.touch
                self.stdout.write(
                    f"  #{verdict.contact.id} {verdict.contact.name} "
                    f"touch #{touch.id} {touch.ts:%Y-%m-%d}"
                )
                self.stdout.write(f"      {verdict.reason}")
                self.stdout.write(f"      note: {(touch.note or '')[:200]!r}")

        if commit:
            for plan in report.plans:
                reclassify.commit_plan(user, plan)

        self.stdout.write("")
        verb = "reclassified" if commit else "would be reclassified"
        self.stdout.write(self.style.SUCCESS(
            f"{tag}{report.demoted_touches} touches {verb} across "
            f"{len(report.plans)} contacts; "
            f"{report.contacts_with_state_change} contacts' warmth/status "
            f"{'recalculated' if commit else 'would be recalculated'}; "
            f"{len(report.flagged)} flagged for review and left alone."
        ))
        if not commit and report.plans:
            self.stdout.write(
                "Nothing was written. Re-run with --commit to apply."
            )
