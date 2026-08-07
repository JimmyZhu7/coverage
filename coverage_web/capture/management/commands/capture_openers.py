"""capture_openers — save drafted openers, fill-only, with a hard voice gate.

    python manage.py capture_openers --email you@example.com --findings f.json
    python manage.py capture_openers --email ... --findings f.json --dry-run

The write half of the opener-drafting scheduled task. The drafting agents
produce a JSON ARRAY of {"id": <contact id>, "opener": <text>}; this command
is the only thing that touches the database, and it enforces the contract the
skill promises:

- FILL-ONLY. A contact whose opener is no longer empty is skipped, reported
  as such, and never overwritten — the field belongs to the user the moment
  they touch it, and a re-run must not clobber an edit they made in the
  contact page's editor.
- DRAFTS, NEVER MAIL. Nothing here sends anything. The text lands in
  `Contact.opener`, which surfaces as the "draft ready" chip and prefills
  Compose — where a human reads it before any send.
- BOUNDED. An opener longer than the model's brief (~600 chars) is refused,
  not truncated: a truncated draft ends mid-sentence in the Compose window,
  and a refusal tells the skill its agent ignored the brief.

Tenant-scoped by id: an id that isn't this user's contact is reported as
unknown, never written.
"""

from __future__ import annotations

import json
from pathlib import Path

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from analytics.events import record_event
from crm.models import Contact

MAX_OPENER_CHARS = 600


class Command(BaseCommand):
    help = "Save drafted openers onto contacts, fill-only."

    def add_arguments(self, parser):
        parser.add_argument("--email", required=True)
        parser.add_argument("--findings", required=True)
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **opts):
        User = get_user_model()
        try:
            user = User.objects.get(email=opts["email"])
        except User.DoesNotExist:
            raise CommandError(f"no user {opts['email']!r}")

        try:
            rows = json.loads(Path(opts["findings"]).read_text())
        except (OSError, ValueError) as exc:
            raise CommandError(f"cannot read findings: {exc}")
        if not isinstance(rows, list):
            raise CommandError("findings must be a JSON array of {id, opener}")

        saved = skipped_filled = refused_long = unknown = 0
        for row in rows:
            cid = row.get("id")
            text = " ".join(str(row.get("opener") or "").split()).strip()
            contact = Contact.objects.for_user(user).filter(
                id=cid, archived=False).first()
            if contact is None:
                unknown += 1
                self.stderr.write(f"unknown contact id {cid!r} — not written")
                continue
            if not text:
                continue
            if len(text) > MAX_OPENER_CHARS:
                refused_long += 1
                self.stderr.write(
                    f"refused {contact.name}: {len(text)} chars is not the brief")
                continue
            if (contact.opener or "").strip():
                skipped_filled += 1
                continue
            saved += 1
            if not opts["dry_run"]:
                contact.opener = text
                contact.save(update_fields=["opener"])
        if saved and not opts["dry_run"]:
            record_event("openers_drafted", user=user, count=saved)

        tag = "[dry-run] " if opts["dry_run"] else ""
        self.stdout.write(self.style.SUCCESS(
            f"{tag}{saved} drafted · {skipped_filled} already had one (kept) · "
            f"{refused_long} refused for length · {unknown} unknown ids"))
