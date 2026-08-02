"""import_firm_dates — the write path for scanned recruiting dates.

    python manage.py import_firm_dates --findings dates.json
    python manage.py import_firm_dates --findings - --dry-run     # stdin

WHO CALLS THIS
--------------
The weekly recruiting-radar agent, after it scans the web. Before this, the
radar wrote its findings into `tracker.md` inside a separate project folder
that nothing in Coverage reads — so a scan could confirm a real deadline and
Coverage would never learn it. This command is that missing half.

It is also the one-off importer for the dates the radar had already collected.

FINDINGS SHAPE
--------------
A JSON array. Each entry:

    {
      "firm": "goldman-sachs",          # Coverage firm slug (required)
      "event_kind": "app_close",        # app_open | app_close |
                                        #   insight_open | insight_deadline
      "date": "2026-09-15",             # ISO; "" for a known-unknown
      "cycle": "SA 2028",               # optional, defaults to --cycle
      "region": "hk",                   # optional ("" = unscoped)
      "confidence": "confirmed_official",   # rumor | reported |
                                            #   confirmed_official
      "source": "https://...",          # where the claim came from
      "note": "opens right after Labor Day"     # optional, free text
    }

An unknown firm slug is SKIPPED and named, never silently dropped and never
auto-created: a typo'd slug that invented a firm would pollute the shared
directory every other user reads.

TWO RULES THAT MAKE THIS SAFE TO RUN ON A SCHEDULE
--------------------------------------------------
1. CONFIDENCE NEVER SILENTLY DOWNGRADES. A date already recorded as
   `confirmed_official` is not overwritten by a later `rumor` about the same
   event — the rumor is appended to `history` and the stored date stands.
   Without this, one bad week of scanning could quietly demote every date the
   system is most sure of, and the cadence engine's whole "confirmed only"
   posture (see coverage_domain.cadence) reads off exactly that field.
   `--force` overrides, for the case where a firm really did retract.

2. HISTORY IS APPEND-ONLY. Every observation is kept with its own confidence,
   source and timestamp, so a date that moved can be explained rather than
   just being different from what someone remembers.
"""

from __future__ import annotations

import json
import sys
from datetime import date, datetime

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from directory.models import Firm, FirmDate

# Same three-band vocabulary seed_directory uses; the float is what the
# cadence engine compares against (1.0 == confirmed_official).
CONFIDENCE_BAND = {"rumor": 0.3, "reported": 0.6, "confirmed_official": 1.0}

# The events the backward planner knows how to act on, plus insight_open,
# which the directory tracks for display. Anything else is a typo.
EVENT_KINDS = ("app_open", "app_close", "insight_open", "insight_deadline")


class BadDate(ValueError):
    """A `date` that was supplied but could not be read."""


def _parse_date(value) -> date | None:
    """ISO date, or None for a DELIBERATELY blank one.

    The distinction is the whole point. A blank is real information — "we
    know this event exists and not yet when" is a state the radar genuinely
    reports. An unparseable string is a broken finding, and the two must not
    collapse to the same answer.

    They used to. Both returned None, so a finding carrying `"Dec 1 2026"`
    read as "no date known" and OVERWROTE a stored `confirmed_official`
    deadline with NULL — reported cheerfully as `MOVE ... -> (no date)`.
    Reproduced 2026-08-02 against a real row. With a scheduled agent writing
    these weekly, one `12/01/2026` would have silently destroyed a date the
    cadence engine acts on. Now it raises, and the caller skips the finding
    with the firm named.
    """
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.strptime(text[:10], "%Y-%m-%d").date()
    except ValueError as exc:
        raise BadDate(text) from exc


class Command(BaseCommand):
    help = "Upsert firm recruiting dates from a scan's JSON findings."

    def add_arguments(self, parser):
        parser.add_argument("--findings", required=True,
                            help="Path to a JSON array, or '-' for stdin.")
        parser.add_argument("--cycle", default="SA 2028",
                            help="Cycle label for entries that omit one.")
        parser.add_argument("--dry-run", action="store_true",
                            help="Report every decision, write nothing.")
        parser.add_argument("--force", action="store_true",
                            help="Allow a lower-confidence finding to overwrite "
                                 "a higher-confidence stored date.")

    def handle(self, *args, **opts):
        raw = sys.stdin.read() if opts["findings"] == "-" else None
        if raw is None:
            try:
                raw = open(opts["findings"], encoding="utf-8").read()
            except OSError as exc:
                raise CommandError(f"cannot read findings: {exc}") from exc
        try:
            findings = json.loads(raw)
        except ValueError as exc:
            raise CommandError(f"findings is not valid JSON: {exc}") from exc
        if not isinstance(findings, list):
            raise CommandError("findings must be a JSON array")

        dry = opts["dry_run"]
        tag = "[dry-run] " if dry else ""
        firm_by_slug = {f.slug: f for f in Firm.objects.all()}
        now = timezone.now()

        created = updated = kept = skipped = 0
        unknown_slugs: set[str] = set()

        for entry in findings:
            if not isinstance(entry, dict):
                skipped += 1
                continue
            slug = str(entry.get("firm", "")).strip()
            event_kind = str(entry.get("event_kind", "")).strip()
            firm = firm_by_slug.get(slug)

            if firm is None:
                unknown_slugs.add(slug or "(blank)")
                skipped += 1
                continue
            if event_kind not in EVENT_KINDS:
                self.stderr.write(self.style.WARNING(
                    f"skip {slug}: unknown event_kind {event_kind!r}"))
                skipped += 1
                continue

            cycle = str(entry.get("cycle") or opts["cycle"]).strip()
            region = str(entry.get("region", "")).strip().lower()
            conf_label = str(entry.get("confidence", "")).strip()
            conf = CONFIDENCE_BAND.get(conf_label, 0.0)
            try:
                new_date = _parse_date(entry.get("date"))
            except BadDate as exc:
                self.stderr.write(self.style.WARNING(
                    f"skip {slug}/{event_kind}: unreadable date {exc!s:.40} "
                    f"(expected YYYY-MM-DD, or \"\" for a known-unknown). "
                    f"Any stored date is left alone."))
                skipped += 1
                continue

            observation = {
                "date": str(entry.get("date", "")),
                "confidence": conf_label,
                "source": str(entry.get("source", "")),
                "note": str(entry.get("note", "")),
                "seen": now.isoformat(),
            }

            existing = FirmDate.objects.filter(
                firm=firm, cycle=cycle, region=region, event_kind=event_kind
            ).first()

            if existing is None:
                created += 1
                self.stdout.write(f"{tag}NEW  {slug}/{event_kind} "
                                  f"{new_date or '(no date)'} [{conf_label}]")
                if not dry:
                    FirmDate.objects.create(
                        firm=firm, cycle=cycle, region=region, event_kind=event_kind,
                        date=new_date, precision=str(entry.get("precision", "")),
                        confidence=conf, source_url=str(entry.get("source", "")),
                        found_on=now, history=[observation],
                    )
                continue

            # Rule 1: a weaker claim never overwrites a stronger stored one.
            downgrade = conf < existing.confidence
            if downgrade and not opts["force"]:
                kept += 1
                self.stdout.write(
                    f"{tag}KEEP {slug}/{event_kind} stored {existing.date} "
                    f"({existing.confidence}) > incoming {new_date} ({conf}); "
                    f"recorded in history")
                if not dry:
                    existing.history = list(existing.history or []) + [
                        {**observation, "outcome": "not_applied_lower_confidence"}
                    ]
                    existing.save(update_fields=["history"])
                continue

            changed = existing.date != new_date or existing.confidence != conf
            updated += 1 if changed else 0
            kept += 0 if changed else 1
            verb = "MOVE" if changed else "SAME"
            self.stdout.write(
                f"{tag}{verb} {slug}/{event_kind} {existing.date} -> "
                f"{new_date or '(no date)'} [{conf_label}]")
            if not dry:
                existing.date = new_date
                existing.confidence = conf
                existing.precision = str(entry.get("precision", "")) or existing.precision
                existing.source_url = str(entry.get("source", "")) or existing.source_url
                existing.found_on = now
                existing.history = list(existing.history or []) + [observation]
                existing.save()

        if unknown_slugs:
            self.stderr.write(self.style.WARNING(
                "unknown firm slugs (skipped, nothing created): "
                + ", ".join(sorted(unknown_slugs))))

        self.stdout.write(self.style.SUCCESS(
            f"{tag}{created} new, {updated} moved, {kept} unchanged, {skipped} skipped"))
