"""capture_discover — create contacts for people a mailbox scan newly found.

    python manage.py capture_discover --email you@example.com --findings people.json
    python manage.py capture_discover --email you@example.com --findings f.json --dry-run

WHY THIS IS A SEPARATE COMMAND FROM `capture_gmail`
---------------------------------------------------
`capture_gmail` deliberately never creates a contact. That rule is right for
what it does: it matches findings against people you are ALREADY tracking, so
an unmatched finding means the search drifted, and inventing a row would hide
the drift.

Discovery is the opposite intent — "here are people I found who are not on
your board yet" — and it needs its own door rather than a flag that softens
the other command's rule. Two commands, two contracts, neither weakening the
other.

WHAT IT REFUSES TO DO
---------------------
- It will not create someone who already exists, matched on email then on
  normalized name.
- It will not resurrect an ARCHIVED contact. Archiving is a deliberate user
  action; a scan finding that person again is not consent to undo it. The
  match is REPORTED so the user can unarchive by hand.
- It will not fabricate warmth. A discovered person starts cold with no
  touches unless the finding carries real evidence of a reply or a chat, in
  which case the touch is logged through the normal ratchet so the cadence
  engine sees the same history it would from any other source.

FINDINGS SHAPE
--------------
A JSON array of `{"name", "email", "role_guess", "firm", "replied",
"chatted", "evidence"}`. `firm` is an optional Coverage slug; an unknown one
is kept as free text rather than dropped, because `Contact.firm_text` exists
precisely so capture never blocks on directory coverage.
"""

from __future__ import annotations

import json
import sys

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from capture.extractors import normalize_email, normalize_name
from crm import services as crm_services
from crm.models import Contact
from directory.models import Firm


class Command(BaseCommand):
    help = "Create contacts for newly discovered people from a mailbox scan."

    def add_arguments(self, parser):
        parser.add_argument("--email", required=True, help="Whose CRM to write to.")
        parser.add_argument("--findings", required=True, help="JSON array, or '-'.")
        parser.add_argument("--source", default="capture",
                            help="Provenance stamped on new contacts.")
        parser.add_argument("--region", default="",
                            help="Region for new contacts (hk/us/...). Blank = unknown, "
                                 "which the cadence engine treats as 'either'.")
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **opts):
        User = get_user_model()
        try:
            user = User.objects.get(email__iexact=opts["email"])
        except User.DoesNotExist as exc:
            raise CommandError(f"no user with email {opts['email']}") from exc

        raw = sys.stdin.read() if opts["findings"] == "-" else None
        if raw is None:
            try:
                raw = open(opts["findings"], encoding="utf-8").read()
            except OSError as exc:
                raise CommandError(f"cannot read findings: {exc}") from exc
        try:
            people = json.loads(raw)
        except ValueError as exc:
            raise CommandError(f"findings is not valid JSON: {exc}") from exc
        if not isinstance(people, list):
            raise CommandError("findings must be a JSON array")

        dry = opts["dry_run"]
        tag = "[dry-run] " if dry else ""
        firm_by_slug = {f.slug: f for f in Firm.objects.all()}

        created = existing = archived_hits = skipped = 0
        touched = 0

        for person in people:
            if not isinstance(person, dict):
                skipped += 1
                continue
            name = str(person.get("name", "")).strip()
            if not name:
                skipped += 1
                continue
            email = normalize_email(str(person.get("email", "")).strip())

            # Match against EVERY row including archived, so an archived
            # person is recognised rather than duplicated.
            everyone = Contact.objects.for_user(user)
            match = None
            if email:
                match = everyone.filter(email__iexact=email).first()
            if match is None:
                target = normalize_name(name)
                match = next(
                    (c for c in everyone if normalize_name(c.name) == target), None
                )

            if match is not None:
                if match.archived:
                    archived_hits += 1
                    self.stdout.write(
                        f"{tag}ARCHIVED {name} — already on the board but archived. "
                        "Reported, not resurrected: unarchive by hand if they should "
                        "come back."
                    )
                else:
                    existing += 1
                    self.stdout.write(f"{tag}HAVE     {name} — already tracked, skipped")
                continue

            firm = firm_by_slug.get(str(person.get("firm", "")).strip())
            created += 1
            self.stdout.write(
                f"{tag}NEW      {name}"
                + (f" <{email}>" if email else " (no address)")
                + (f" @ {firm.name}" if firm else "")
            )
            if dry:
                continue

            contact = Contact(
                user=user, name=name, email=email or "",
                firm=firm, firm_text="" if firm else str(person.get("firm", "")).strip(),
                role=str(person.get("role_guess", "")).strip()[:255],
                region=opts["region"], source=opts["source"],
                notes=f"Discovered by mailbox scan · {timezone.localdate():%b %d, %Y}"
                      + (f"\n{person.get('evidence')}" if person.get("evidence") else ""),
            )
            contact.save()

            # Real evidence -> a real touch, through the same ratchet every
            # other source uses. Never invented: a person found on a CC line
            # with no reply stays cold, which is the truth about them.
            kind = ("chat" if person.get("chatted")
                    else "reply_received" if person.get("replied") else None)
            if kind:
                crm_services.log_touch(
                    user.id, contact.id, kind, "email",
                    note="Discovered by mailbox scan", source="capture",
                )
                touched += 1

        self.stdout.write(self.style.SUCCESS(
            f"{tag}{created} created ({touched} with a touch logged), "
            f"{existing} already tracked, {archived_hits} archived-and-left-alone, "
            f"{skipped} unusable"))
