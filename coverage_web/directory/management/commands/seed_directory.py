"""seed_directory — one-time (idempotent) importer of the founder's SHARED firm
facts into the `firms` and `firm_dates` tables.

Reads (READ-ONLY) the founder's personal-tool data:
  - `<campaign>/firms.yaml`            -> firms
  - `<campaign>/kb/timeline_us.yaml`   -> firm_dates (region us)
  - `<campaign>/kb/timeline_hk.yaml`   -> firm_dates (region hk)

Only firm-level FACTS cross over. Per build-plan §4 this is shared data (facts
about firms, not about one student), so there is no `user_id` and the
single-user `tier` curation is deliberately dropped — it is one student's
targeting, not a property of the firm. Re-running updates rows in place
(`update_or_create` on the natural keys), never duplicating.

Usage:
    python manage.py seed_directory
    python manage.py seed_directory --firms-file <path> --timeline-dir <dir>
    python manage.py seed_directory --dry-run
"""

from __future__ import annotations

import re
from datetime import date, datetime, timezone as py_timezone
from pathlib import Path

from django.core.management.base import BaseCommand
from django.db import transaction

from directory.models import Firm, FirmDate
from directory.seed_parsers import parse_firms_yaml, parse_timeline_yaml

# The seed data used to be read live out of the founder's pre-Coverage
# project folder. That system is retired and its folder deleted (final
# archive: ~/Desktop/recruitment-opportunities-final-archive-2026-08-02.zip),
# so the canonical copies now live in THIS repo — which also means a fresh
# clone can seed itself without any external directory existing.
_DEFAULT_SEEDS = Path(__file__).resolve().parents[4] / "data" / "seeds"

# The founder's timeline confidence is a string label (see confidence.py's
# rumor < reported < confirmed_official ladder). The shared `firm_dates.confidence`
# column is a float, so we map the label onto a band and preserve the ORIGINAL
# label verbatim in `history` for fidelity. (Reported as a mapping compromise.)
_CONFIDENCE_BAND = {
    "rumor": 0.3,
    "reported": 0.6,
    "confirmed_official": 1.0,
}


def _partial_date(value: str | None) -> date | None:
    """`YYYY-MM-DD` -> exact date; `YYYY-MM` -> first of month; `YYYY` -> Jan 1;
    else None. The entry's own `precision` field records whether the day/month is
    real, so normalizing a month to the 1st loses nothing a reader would trust."""
    s = (value or "").strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
        try:
            return date.fromisoformat(s)
        except ValueError:
            return None
    if re.fullmatch(r"\d{4}-\d{2}", s):
        y, m = s.split("-")
        return date(int(y), int(m), 1)
    if re.fullmatch(r"\d{4}", s):
        return date(int(s), 1, 1)
    return None


def _found_dt(value: str | None) -> datetime | None:
    d = _partial_date(value)
    if d is None:
        return None
    return datetime(d.year, d.month, d.day, tzinfo=py_timezone.utc)


def _sponsors_blob(firm: dict) -> dict:
    """Map the founder's firm-level `sponsors: true|unknown` flag onto the
    model's per-region JSON shape (its documented intent). A firm-level flag
    applied across the firm's own regions is faithful — the founder's flag is
    explicitly about each firm's home market. No regions -> {"default": flag}.
    Absent (the on-campus pseudo-firm) -> {}."""
    if "sponsors" not in firm:
        return {}
    val = firm["sponsors"]  # True / False / "unknown"
    regions = firm.get("regions") or []
    if not regions:
        return {"default": val}
    return {r: val for r in regions}


class Command(BaseCommand):
    help = "Seed shared firms + firm_dates from the founder's firms.yaml and kb/timeline_*.yaml (idempotent)."

    def add_arguments(self, parser):
        parser.add_argument("--firms-file", default=str(_DEFAULT_SEEDS / "firms.yaml"))
        parser.add_argument("--timeline-dir", default=str(_DEFAULT_SEEDS))
        parser.add_argument("--dry-run", action="store_true",
                            help="Parse and report counts without writing to the DB.")

    def handle(self, *args, **opts):
        firms_path = Path(opts["firms_file"])
        timeline_dir = Path(opts["timeline_dir"])
        dry = opts["dry_run"]

        if not firms_path.exists():
            self.stderr.write(self.style.ERROR(f"firms file not found: {firms_path}"))
            return

        firm_rows = parse_firms_yaml(firms_path.read_text(encoding="utf-8"))
        timeline_files = [timeline_dir / "timeline_us.yaml", timeline_dir / "timeline_hk.yaml"]

        with transaction.atomic():
            f_created, f_updated = self._seed_firms(firm_rows, dry)
            d_created, d_updated, d_skipped = self._seed_firm_dates(timeline_files, dry)
            if dry:
                transaction.set_rollback(True)

        self.stdout.write(self.style.SUCCESS(
            f"firms: {f_created} created, {f_updated} updated ({len(firm_rows)} in source)"
        ))
        self.stdout.write(self.style.SUCCESS(
            f"firm_dates: {d_created} created, {d_updated} updated, {d_skipped} skipped (unknown firm/key)"
        ))
        if dry:
            self.stdout.write(self.style.WARNING("--dry-run: rolled back, nothing written."))

    # ------------------------------------------------------------------ firms

    def _seed_firms(self, firm_rows: list[dict], dry: bool) -> tuple[int, int]:
        created = updated = 0
        for row in firm_rows:
            slug = str(row.get("id", "")).strip()
            name = str(row.get("name", "")).strip()
            if not slug or not name:
                continue
            defaults = {
                "name": name,
                "domains": row.get("domains") or [],
                "regions": row.get("regions") or [],
                "tracks": row.get("tracks") or [],
                "sponsors": _sponsors_blob(row),
                "status": str(row.get("status", "active")),
            }
            if dry:
                exists = Firm.objects.filter(slug=slug).exists()
                created += 0 if exists else 1
                updated += 1 if exists else 0
                continue
            _, was_created = Firm.objects.update_or_create(slug=slug, defaults=defaults)
            created += 1 if was_created else 0
            updated += 0 if was_created else 1
        return created, updated

    # -------------------------------------------------------------- firm_dates

    def _seed_firm_dates(self, timeline_files: list[Path], dry: bool) -> tuple[int, int, int]:
        created = updated = skipped = 0
        firm_by_slug = {f.slug: f for f in Firm.objects.all()}

        for path in timeline_files:
            if not path.exists():
                self.stderr.write(self.style.WARNING(f"timeline file not found (skipped): {path}"))
                continue
            region, cycle_label, entries = parse_timeline_yaml(path.read_text(encoding="utf-8"))
            for entry in entries:
                key = str(entry.get("key", "")).strip()
                parts = key.split("/")
                if len(parts) != 3:
                    skipped += 1
                    continue
                slug, cycle, event_kind = parts
                firm = firm_by_slug.get(slug)
                if firm is None:
                    skipped += 1
                    continue

                conf_label = str(entry.get("confidence", "")).strip()
                history = [{
                    "date": entry.get("date", ""),
                    "precision": entry.get("precision", ""),
                    "confidence": conf_label,          # ORIGINAL string label, preserved
                    "source": entry.get("source", ""),
                    "found": entry.get("found", ""),
                    "note": entry.get("note", ""),
                    "cycle_label": cycle_label,        # file-level "SA 2028"
                    "seeded_from": path.name,
                }]
                defaults = {
                    "date": _partial_date(entry.get("date")),
                    "precision": str(entry.get("precision", "")),
                    "confidence": _CONFIDENCE_BAND.get(conf_label, 0.0),
                    "source_url": str(entry.get("source", "")),
                    "found_on": _found_dt(entry.get("found")),
                    "history": history,
                }
                if dry:
                    exists = FirmDate.objects.filter(
                        firm=firm, cycle=cycle, region=region, event_kind=event_kind
                    ).exists()
                    created += 0 if exists else 1
                    updated += 1 if exists else 0
                    continue
                _, was_created = FirmDate.objects.update_or_create(
                    firm=firm, cycle=cycle, region=region, event_kind=event_kind, defaults=defaults
                )
                created += 1 if was_created else 0
                updated += 0 if was_created else 1
        return created, updated, skipped
