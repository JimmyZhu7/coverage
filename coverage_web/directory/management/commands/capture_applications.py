"""capture_applications — mark roles applied from their confirmation emails.

    python manage.py capture_applications --email you@example.com --findings f.json --dry-run
    python manage.py capture_applications --email you@example.com --findings f.json

WHY THIS EXISTS
---------------
My Applications was a pipeline nobody used. The founder's own account tracked
zero roles after months, because the page competed with the ATS's own tracking
and lost: nobody who has just finished a twenty-minute Workday form comes back
here to click Save and later drag a card to "Applied".

But the evidence already lands in the mailbox — every ATS sends "Thank you for
applying" — and Coverage already reads that mailbox twice a day. So the board
fills itself, and the student's job shrinks to correcting it.

FINDINGS SHAPE
--------------
A JSON array of `{"firm", "title", "applied_at", "evidence", "thread_id"}`.
`firm` is a Coverage slug or a firm name; `title` is the role as the
confirmation states it (often partial, sometimes absent); `applied_at` is an
ISO date/datetime; `evidence` is the sentence that proved it.

WHAT IT REFUSES TO DO
---------------------
- Guess between two believable roles. `directory.applications.match_application`
  advances a row only when exactly one candidate is credible; everything else
  is REPORTED for the human. An application wrongly marked submitted is worse
  than one not marked at all — it tells a student a form is done that isn't,
  and the deadline passes while the board says they are covered.
- Move a row backwards. A role already at interview or offer knows more than a
  confirmation email does, and `closed` is a decision the user made.
- Touch a row twice. Re-running over the same findings is a no-op, which
  matters because the sync re-reads its window every run for days.
"""

from __future__ import annotations

import json
import sys

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime

from analytics.models import UserOpportunity
from directory.applications import match_application, may_advance
from directory.models import Firm, Opportunity


def _resolve_firm(raw: str):
    """A Coverage slug, or a firm name, or nothing."""
    name = (raw or "").strip()
    if not name:
        return None
    firm = Firm.objects.filter(slug=name.lower()).first()
    if firm is None:
        firm = Firm.objects.filter(name__iexact=name).first()
    return firm


def _applied_at(raw):
    """An aware datetime from an ISO date or datetime, or None."""
    text = (raw or "").strip()
    if not text:
        return None
    when = parse_datetime(text)
    if when is None:
        day = parse_date(text)
        if day is None:
            return None
        when = timezone.datetime.combine(day, timezone.datetime.min.time())
    if timezone.is_naive(when):
        when = timezone.make_aware(when, timezone.get_current_timezone())
    return when


class Command(BaseCommand):
    help = "Mark tracked roles applied from application-confirmation emails."

    def add_arguments(self, parser):
        parser.add_argument("--email", required=True)
        parser.add_argument("--findings", required=True, help="JSON array, or '-'.")
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
            rows = json.loads(raw)
        except ValueError as exc:
            raise CommandError(f"findings is not valid JSON: {exc}") from exc
        if not isinstance(rows, list):
            raise CommandError("findings must be a JSON array")

        dry = opts["dry_run"]
        tag = "[dry-run] " if dry else ""

        tracked_ids = set(
            UserOpportunity.objects.for_user(user).values_list("opportunity_id", flat=True)
        )

        applied = already = ambiguous = unknown_firm = 0

        for row in rows:
            if not isinstance(row, dict):
                continue
            firm = _resolve_firm(str(row.get("firm", "")))
            if firm is None:
                unknown_firm += 1
                self.stdout.write(
                    f"{tag}?    {row.get('firm', '(no firm)')!r}: no such firm in the "
                    "directory — nothing marked")
                continue

            title = str(row.get("title", "") or "")
            candidates = list(
                Opportunity.objects.filter(firm=firm, status="open").order_by("title")
            )
            result = match_application(candidates, title, tracked_ids=tracked_ids)

            if not result.matched:
                ambiguous += 1
                self.stdout.write(
                    f"{tag}?    {firm.name} — {title or '(no title given)'}: "
                    f"{result.reason}. Left for you to set by hand.")
                continue

            opp = result.opportunity
            uo = UserOpportunity.objects.for_user(user).filter(opportunity=opp).first()
            if uo is not None and not may_advance(uo.applied_status):
                already += 1
                self.stdout.write(
                    f"{tag}=    {firm.name} — {opp.title}: already at "
                    f"{uo.applied_status or 'saved'}, left alone")
                continue

            applied += 1
            self.stdout.write(f"{tag}APPLIED {firm.name} — {opp.title} ({result.reason})")
            if dry:
                continue

            uo, _ = UserOpportunity.all_objects.get_or_create(
                user=user, opportunity=opp
            )
            uo.applied_status = "submitted"
            uo.dismissed = False
            if uo.applied_at is None:
                uo.applied_at = _applied_at(row.get("applied_at")) or timezone.now()
            uo.save(update_fields=["applied_status", "applied_at", "dismissed"])
            tracked_ids.add(opp.id)

        self.stdout.write(self.style.SUCCESS(
            f"{tag}{applied} marked applied, {already} already further along, "
            f"{ambiguous} left for you, {unknown_firm} unknown firms"))
