"""Backfill blank Contact.region values from the row's own evidence.

Dry-run FIRST, by design: with no flags this prints what it would do and
writes nothing. `--apply` writes, and every write is recorded in an undo file
so the whole run can be reversed with `--revert`. It only ever fills blanks —
a region a human (or a previous run the human reviewed) set is never touched,
which is the same explicit-wins rule `Contact.save()` enforces.

Usage:
    manage.py backfill_contact_regions --user founder@example.com
    manage.py backfill_contact_regions --user founder@example.com --apply
    manage.py backfill_contact_regions --user founder@example.com \
        --revert region_backfill_undo_20260825T120000.json

Revert restores a row only while it still holds the exact value this run
wrote: a region the user corrected afterwards is their word now, and undoing
the backfill must not undo the correction.
"""

from __future__ import annotations

import json
from collections import Counter
from datetime import datetime, timezone as dt_timezone
from pathlib import Path

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from crm.models import Contact, Touch
from crm.region_inference import infer_for_contact


class Command(BaseCommand):
    help = (
        "Fill blank contact regions from touch subjects, email domains, role "
        "text, campaign sources and firm footprints. Dry run by default; "
        "--apply to write; --revert <undo-file> to reverse an applied run."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--user", required=True,
            help="Email of the account whose contacts to backfill.",
        )
        parser.add_argument(
            "--apply", action="store_true",
            help="Write the proposed regions. Without this, dry run only.",
        )
        parser.add_argument(
            "--revert", metavar="UNDO_FILE",
            help="Reverse a previous --apply using its undo file.",
        )
        parser.add_argument(
            "--undo-file", metavar="PATH",
            help="Where --apply writes its undo record "
                 "(default: region_backfill_undo_<timestamp>.json in cwd).",
        )

    def handle(self, *args, **options):
        User = get_user_model()
        try:
            user = User.objects.get(email=options["user"])
        except User.DoesNotExist:
            raise CommandError(f"no user with email {options['user']!r}")

        if options["revert"]:
            self._revert(user, options["revert"])
            return

        contacts = list(
            Contact.objects.for_user(user)
            .filter(archived=False, region="")
            .select_related("firm")
            .order_by("id")
        )
        # One query for every row's subjects, newest first so the reason a
        # dry run shows is the most recent statement, not the oldest.
        subjects_by_contact: dict[int, list[str]] = {}
        subject_rows = (
            Touch.objects.for_user(user)
            .filter(contact_id__in=[c.id for c in contacts])
            .exclude(subject="")
            .order_by("-ts")
            .values_list("contact_id", "subject")
        )
        for cid, subject in subject_rows:
            subjects_by_contact.setdefault(cid, []).append(subject)

        proposals = []  # (contact, region, reason)
        no_signal = []
        for c in contacts:
            hit = infer_for_contact(c, subjects_by_contact.get(c.id, ()))
            if hit:
                proposals.append((c, *hit))
            else:
                no_signal.append(c)

        by_region = Counter(region for _, region, _ in proposals)
        self.stdout.write(
            f"{len(contacts)} blank-region contacts for {user.email}: "
            f"{len(proposals)} with a signal "
            f"({dict(by_region)}), {len(no_signal)} stay unknown."
        )
        for c, region, reason in proposals:
            self.stdout.write(f"  {c.id:>6}  {c.name[:30]:<30} -> {region:<5}  {reason}")
        if no_signal:
            self.stdout.write("No signal, left blank (honest unknowns):")
            for c in no_signal:
                self.stdout.write(f"  {c.id:>6}  {c.name[:30]:<30} -> (blank)")

        if not options["apply"]:
            self.stdout.write(self.style.WARNING(
                "Dry run — nothing written. Re-run with --apply to write."
            ))
            return

        undo_path = Path(
            options["undo_file"]
            or f"region_backfill_undo_"
               f"{datetime.now(dt_timezone.utc):%Y%m%dT%H%M%S}.json"
        )
        undo = {"user": user.email, "written": {}}
        for c, region, reason in proposals:
            c.region = region
            c.save(update_fields=["region"])
            undo["written"][str(c.id)] = region
        undo_path.write_text(json.dumps(undo, indent=2))
        self.stdout.write(self.style.SUCCESS(
            f"Wrote {len(proposals)} regions. Undo file: {undo_path} "
            f"(revert with --revert {undo_path})"
        ))

    def _revert(self, user, undo_file: str):
        try:
            undo = json.loads(Path(undo_file).read_text())
        except OSError as exc:
            raise CommandError(f"cannot read undo file: {exc}")
        if undo.get("user") != user.email:
            raise CommandError(
                f"undo file is for {undo.get('user')!r}, not {user.email!r}"
            )
        written = undo.get("written", {})
        rows = Contact.objects.for_user(user).filter(
            id__in=[int(k) for k in written]
        )
        reverted = skipped = 0
        for c in rows:
            if c.region == written[str(c.id)]:
                c.region = ""
                # `update_fields` bypasses nothing here: save()'s firm
                # inference only refills a blank when the firm is
                # unambiguous, which is the pre-backfill state we are
                # restoring on purpose.
                c.save(update_fields=["region"])
                reverted += 1
            else:
                # The user changed it after the backfill — their word now.
                skipped += 1
        self.stdout.write(self.style.SUCCESS(
            f"Reverted {reverted} contacts to blank; "
            f"left {skipped} that were edited since."
        ))
