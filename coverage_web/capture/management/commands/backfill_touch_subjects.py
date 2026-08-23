"""backfill_touch_subjects — stamp Subject headers onto touches written before
`Touch.subject` existed, from a thread-id-to-subject mapping resolved out of
band (report-only by default).

    python manage.py backfill_touch_subjects --email you@example.com \
        --mapping /path/to/subjects.json
    python manage.py backfill_touch_subjects --email you@example.com \
        --mapping /path/to/subjects.json --commit

WHY THIS EXISTS
---------------
`capture.gmail._stamp_subject` writes `Touch.subject` only when a NEW touch is
logged, and the column landed on 2026-08-22. Every row older than that is
blank, and `crm.campaigns` groups outbound mail by normalized subject — so on
the founder's own account, a 201-thread mail merge with one subject line could
not form a campaign, because not one of its touches had the subject stored.
`capture/subject_backfill.py` carries the full argument, including why the
`[gmail:<thread_id>]` marker already in the notes is a real join key rather
than a guess.

THE MAPPING FILE FORMAT
-----------------------
One JSON object, Gmail thread id to subject line::

    {
      "19fbcd1fe5310001": "Fall 2026 ICC Alumni Digital Panel Outreach",
      "19f2e6ef0c479dc0": "Re: USC student interested in your desk",
      "19aaaaaaaaaaaaaa": null
    }

`null` or `""` means "this thread was looked up and no longer resolves". It is
counted and reported apart from a thread id that is simply missing from the
file, because a thread that is gone and a thread nobody checked are different
facts. Subjects are trimmed and truncated to 255 characters, exactly as the
live capture path truncates them, so a backfilled row and a live-stamped row of
the same message come out byte-identical.

Producing the file is deliberately NOT this command's job. It needs the
mailbox, and this command needs to be runnable, reviewable and testable with no
network at all. Extract the thread ids from the notes, resolve them against
Gmail, write the JSON, then run this.

REPORT-ONLY BY DEFAULT
----------------------
Same posture as `reclassify_inbound_touches` next door, for the same reason:
this writes to a column a detector groups on, so a mapping assembled against
the wrong mailbox would not corrupt one row visibly — it would quietly invent a
campaign out of unrelated people. The report prints the subjects it would write
and how many touches each would land on, which is the shape that makes a wrong
mapping obvious at a glance: a real merge is one subject across dozens of rows.
Nothing is written without `--commit`, and a non-blank subject is never
overwritten under any flag.

Live network: none. Live database: read-only unless `--commit`.
"""

from __future__ import annotations

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from capture import subject_backfill


class Command(BaseCommand):
    help = (
        "Stamp Subject headers onto historical touches from a JSON mapping of "
        "gmail thread id to subject. Report-only unless --commit."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--email", required=True,
            help="Whose CRM to stamp. Always scoped to one user.",
        )
        parser.add_argument(
            "--mapping", required=True,
            help="Path to the JSON object of {thread_id: subject}.",
        )
        parser.add_argument(
            "--commit", "--apply", action="store_true", default=False,
            dest="commit",
            help="Write the subjects. Default is report-only.",
        )
        parser.add_argument(
            "--limit-detail", type=int, default=20,
            help="How many individual rows to print per section (default 20). "
                 "The counts are always complete.",
        )

    def handle(self, *args, **opts):
        User = get_user_model()
        try:
            user = User.objects.get(email__iexact=opts["email"])
        except User.DoesNotExist as exc:
            raise CommandError(f"No user with email {opts['email']!r}") from exc

        try:
            mapping = subject_backfill.load_mapping(opts["mapping"])
        except (OSError, ValueError) as exc:
            raise CommandError(f"Could not read --mapping: {exc}") from exc

        commit = opts["commit"]
        limit = max(0, opts["limit_detail"])
        tag = "" if commit else "[report only] "

        report = subject_backfill.build_report(user, mapping)

        self.stdout.write(
            f"{tag}{user.email}: {report.touches_seen} touches examined, "
            f"{report.marked_touches} carry a [gmail:...] marker; "
            f"mapping holds {report.mapping_size} thread id(s)."
        )
        self.stdout.write("")

        if report.stamps:
            self.stdout.write(self.style.MIGRATE_HEADING(
                "SUBJECTS THAT WOULD BE STAMPED, commonest first:"
                if not commit else "SUBJECTS STAMPED, commonest first:"
            ))
            for subject, count in report.subject_counts:
                self.stdout.write(f"  {count:4d} touch(es)  {subject[:100]!r}")
            self.stdout.write("")
            self.stdout.write(self.style.MIGRATE_HEADING("ROW DETAIL:"))
            self._rows(
                [(s.touch, f"[gmail:{s.thread_id}] -> {s.subject[:80]!r}")
                 for s in report.stamps],
                limit,
            )
        else:
            self.stdout.write("Nothing to stamp.")

        self._section(
            "ALREADY STAMPED, left alone (a header beats a file):",
            report.already_stamped, limit,
        )
        self._section(
            "MARKER PRESENT, NOT IN THE MAPPING — nobody looked these up:",
            report.unmapped, limit,
        )
        self._section(
            "MARKER PRESENT, THREAD NO LONGER RESOLVES:",
            report.unresolvable, limit,
        )
        self._section(
            "CONFLICTING MARKERS — needs your eyes, not this command's:",
            report.ambiguous, limit,
        )
        self._section(
            "NO GMAIL MARKER — unstampable by any mapping, and this is the "
            "coverage ceiling:",
            report.unmarked, limit,
        )

        if report.unused_thread_ids:
            self.stdout.write("")
            self.stdout.write(
                f"{len(report.unused_thread_ids)} thread id(s) in the mapping "
                "match no touch on this account "
                f"(e.g. {', '.join(report.unused_thread_ids[:5])})."
            )

        written = subject_backfill.commit(user, report) if commit else 0

        self.stdout.write("")
        if commit:
            self.stdout.write(self.style.SUCCESS(
                f"{written} touch(es) stamped. "
                f"{len(report.already_stamped)} left alone as already stamped; "
                f"{len(report.unmapped) + len(report.unresolvable)} carry a "
                f"marker this mapping cannot resolve; {len(report.unmarked)} "
                "carry no marker at all."
            ))
            if written != len(report.stamps):
                self.stdout.write(self.style.WARNING(
                    f"{len(report.stamps) - written} row(s) planned but not "
                    "written — something stamped them between the report and "
                    "the write. Nothing was overwritten. Re-run to see the "
                    "current picture."
                ))
        else:
            self.stdout.write(self.style.SUCCESS(
                f"{tag}{len(report.stamps)} touch(es) would be stamped; "
                f"{len(report.already_stamped)} would be left alone as already "
                f"stamped; {len(report.unmapped) + len(report.unresolvable)} "
                f"carry a marker this mapping cannot resolve; "
                f"{len(report.unmarked)} carry no marker at all and cannot be "
                "stamped from any mapping."
            ))
            if report.stamps:
                self.stdout.write(
                    "Nothing was written. Re-run with --commit to apply."
                )

    def _section(self, heading, skips, limit) -> None:
        if not skips:
            return
        self.stdout.write("")
        self.stdout.write(self.style.MIGRATE_HEADING(
            f"{heading}  ({len(skips)})"
        ))
        self._rows([(s.touch, s.reason) for s in skips], limit)

    def _rows(self, rows, limit) -> None:
        for touch, detail in rows[:limit]:
            self.stdout.write(
                f"  touch #{touch.id} {touch.ts:%Y-%m-%d} {touch.kind}"
            )
            self.stdout.write(f"      {detail}")
        if len(rows) > limit:
            self.stdout.write(f"  ... and {len(rows) - limit} more")
