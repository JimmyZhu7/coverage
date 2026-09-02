"""fix_school_firms — alumni filed at their school instead of their employer.

    python manage.py fix_school_firms --email you@example.com          # dry run
    python manage.py fix_school_firms --email you@example.com --apply

WHAT IT FINDS. A contact with no directory `Firm`, a free-text firm that names
a SCHOOL (the student's own institution, or anything carrying a generic school
word), and an email address whose domain `FirmDomains.match` resolves to a
directory firm. That combination has exactly one reading: an alum, working at
a firm the directory knows, filed under where they studied.

WHY IT MATTERS. Measured 2026-09-01 on the founder's account: 19 contacts sit
at free-text firm "usc" with `firm_id` NULL, and 7 of them write from
bain.com, bcg.com, deloitte.com or pwc.com. Because `Contact.firm` is the only
employer field there is, those 7 are absent from Firm Coverage, carry no tier,
get no firm dates and no Firm Fit — and the affinity the record was trying to
express (alum at a target firm) is the single highest-value fact a networking
CRM can hold. The rule is in `capture.discovery.school_firm_fields`; this
command is that rule applied to rows written before it existed.

DRY RUN BY DEFAULT. The dry run prints every row it would change and the exact
before/after for each field, and writes nothing. `--apply` saves through
`Contact.save()` (not `.update()`) precisely so `resolve_region` runs and a
now-known single-market firm fills in the region the school never could.

WHAT IT WILL NOT DO: overwrite a firm that already resolved, touch a contact
whose free-text firm is a real employer outside the directory, or guess from
an address that resolves to nothing. All three degrade to "leave the row
alone", which is what they mean.
"""

from __future__ import annotations

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from capture.discovery import FirmDomains, school_firm_fields
from crm.models import Contact
from directory.models import Firm


class Command(BaseCommand):
    help = (
        "Re-file contacts whose free-text firm names a school while their "
        "email domain names a directory firm."
    )

    def add_arguments(self, parser):
        parser.add_argument("--email", help="One account. Omit to check all.")
        parser.add_argument(
            "--apply", action="store_true",
            help="Save the changes. Without this the command only reports.",
        )

    def handle(self, *args, **opts):
        User = get_user_model()
        if opts["email"]:
            try:
                users = [User.objects.get(email__iexact=opts["email"])]
            except User.DoesNotExist as exc:
                raise CommandError(f"no user with email {opts['email']}") from exc
        else:
            users = list(User.objects.filter(deleted_at__isnull=True).order_by("email"))

        # One map for the whole run — the same lazy load the capture batch
        # uses, not one query per contact.
        firm_domains = FirmDomains()
        firm_names = dict(Firm.objects.values_list("id", "name"))
        total = 0

        for user in users:
            rows = []
            for contact in (
                Contact.objects.for_user(user)
                .filter(firm__isnull=True)
                .exclude(email="")
                .order_by("id")
            ):
                fields = school_firm_fields(
                    contact, user=user, firm_domains=firm_domains
                )
                if fields:
                    rows.append((contact, fields))
            if not rows:
                continue

            self.stdout.write(self.style.MIGRATE_HEADING(
                f"{user.email}: {len(rows)} contact(s) whose address names a "
                "directory firm"
            ))
            self.stdout.write(
                f"  {'id':>6}  {'contact':<22} {'domain':<18} "
                f"{'firm_text':<12} -> {'firm':<22} school"
            )
            for contact, fields in rows:
                domain = contact.email.rsplit("@", 1)[-1].lower()
                self.stdout.write(
                    f"  {contact.id:>6}  {contact.name[:22]:<22} {domain[:18]:<18} "
                    f"{(contact.firm_text or '')[:12]:<12} -> "
                    f"{firm_names.get(fields['firm_id'], '?')[:22]:<22} "
                    f"{fields.get('school', contact.school or '—')}"
                )
            total += len(rows)

            if not opts["apply"]:
                continue
            for contact, fields in rows:
                for field, value in fields.items():
                    setattr(contact, field, value)
                # save(), not update(): the region has to re-resolve off the
                # firm that just became knowable.
                contact.save()
            self.stdout.write(self.style.SUCCESS(
                f"  {len(rows)} contact(s) re-filed under their employer."
            ))

        if not total:
            self.stdout.write(self.style.SUCCESS(
                "No contact is filed at a school while their address names a "
                "directory firm."
            ))
        elif not opts["apply"]:
            self.stdout.write(f"{total} contact(s) would change. Re-run with --apply.")
