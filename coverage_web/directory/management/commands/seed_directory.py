"""seed_directory — idempotent importer of the SHARED firm facts into the
`firms` and `firm_dates` tables. The step that makes a fresh deploy non-empty.

Reads (READ-ONLY) three YAML files that ship inside this package:
  - `directory/seeds/firms.yaml`        -> firms
  - `directory/seeds/timeline_us.yaml`  -> firm_dates (region us)
  - `directory/seeds/timeline_hk.yaml`  -> firm_dates (region hk)

Usage:
    python manage.py seed_directory
    python manage.py seed_directory --firms-file <path> --timeline-dir <dir>
    python manage.py seed_directory --dry-run

WHY THE SEEDS LIVE IN THE PACKAGE AND NOT IN `data/`
----------------------------------------------------
They used to live in `data/seeds/`, and this docstring used to claim that a
fresh clone could therefore seed itself. That claim was false: `.gitignore`
excludes the whole `data/` directory under its "this repo is PUBLIC" block, so
none of the three files was ever committed. On Render — or any fresh clone —
`firms.yaml` simply did not exist, this command printed "firms file not found"
and returned, and the app came up with zero firms and zero firm dates.
`seed_mail_domains` (2026-08-25) hit the identical problem scoped to one
column, and fixed it the same way: move the data into a tracked module under
`directory/`. This is that fix applied to the seed corpus itself.

The tracked copy is SCRUBBED, which is why `data/` did not simply get a
`!data/seeds/` negation. The founder's `firms.yaml` carries a hand-curated
`tier: 1/2/3` ranking of employers plus paragraphs reasoning about which banks
are prestigious — that is precisely the "founder's own target list" the ignore
rule names. Nothing is lost by dropping it: `tier` was ALREADY discarded on
import (per build-plan §4 these rows are facts about firms, not about one
student), and the only tier in the product is `UserFirm.tier`, which each
student sets for themselves. The tracked file also regroups its rows by
industry segment so the ORDER carries no ranking either. Every remaining
column — name, tracks, regions, status, domains, sponsors — is a public fact
about the firm. The two timeline files needed no scrubbing at all.

`directory/seeds/*.yaml` is now the canonical copy. If `data/seeds/` still
exists on this machine it is a pre-2026-08-25 archive, no longer read; the
command warns when its firm list has drifted from the tracked one, because a
silent divergence between the two is exactly what produced the mail-domain
bug.

Re-running updates rows in place (`update_or_create` on the natural keys),
never duplicating. Note that it REPLACES `domains` from the YAML, which is why
`seed_mail_domains` and `seed_logo_domains` exist as separate append-only
commands and why deploy order matters — see `docs/deploy.md` §2.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timezone as py_timezone
from pathlib import Path

from django.core.management.base import BaseCommand
from django.db import transaction

from directory.models import Firm, FirmDate
from directory.timeline import parse_cycle
from directory.seed_parsers import (
    parse_firms_yaml,
    parse_timeline_phases,
    parse_timeline_yaml,
)

# The seed data used to be read live out of the founder's pre-Coverage project
# folder, then out of `data/seeds/`. That folder is retired (final archive:
# ~/Desktop/recruitment-opportunities-final-archive-2026-08-02.zip) and `data/`
# is gitignored, so the canonical copies now sit INSIDE this Django app, where
# git tracks them and a fresh clone genuinely can seed itself. See the module
# docstring for what was stripped on the way in.
_DEFAULT_SEEDS = Path(__file__).resolve().parents[2] / "seeds"

# Pre-2026-08-25 location. Not read any more; only checked for drift, and only
# when it happens to exist (it never does on a deploy — `data/` is ignored).
_LEGACY_SEEDS = Path(__file__).resolve().parents[4] / "data" / "seeds"

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
    help = "Seed shared firms + firm_dates from directory/seeds/*.yaml (idempotent)."

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
            # Tracked and shipped inside the package, so this should be
            # unreachable on a normal checkout. It stays a hard error rather
            # than a warning: seeding nothing leaves the whole app empty, and
            # that used to happen SILENTLY enough to survive to production.
            self.stderr.write(self.style.ERROR(
                f"firms file not found: {firms_path}\n"
                "Expected it to ship with the package at directory/seeds/. "
                "Pass --firms-file to point somewhere else."
            ))
            return

        firm_rows = parse_firms_yaml(firms_path.read_text(encoding="utf-8"))
        timeline_files = [timeline_dir / "timeline_us.yaml", timeline_dir / "timeline_hk.yaml"]
        self._warn_if_legacy_drifted(firms_path, firm_rows)

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

    def handle_firm_dates_only(self, timeline_files: list[Path]):
        """Seed the firm dates and nothing else.

        `relabel_firm_dates --apply` needs exactly the firm-date half of this
        command: it is re-applying re-dated timeline seeds against a database
        the founder is reviewing row by row, and a firm re-seed alongside it
        would widen a reviewed change into an unreviewed one. Exposed as a
        method rather than copied, because `_seed_firm_dates` is where the
        never-downgrade and append-only-history rules live and a second writer
        with its own copy of those rules is the defect that method was fixed
        for.
        """
        with transaction.atomic():
            created, updated, skipped = self._seed_firm_dates(timeline_files, False)
        self.stdout.write(self.style.SUCCESS(
            f"firm_dates: {created} created, {updated} updated, "
            f"{skipped} skipped (unknown firm/key)"))
        return created, updated, skipped

    # ------------------------------------------------------------------ drift

    def _warn_if_legacy_drifted(self, firms_path: Path, firm_rows: list[dict]) -> None:
        """Report a `data/seeds/firms.yaml` whose firm list no longer matches the
        tracked one.

        The archive is never read, so a divergence changes nothing this run —
        the point is to make it VISIBLE. Editing the private copy and assuming
        the deploy picked it up is exactly the mistake behind the mail-domain
        bug (`_mail_domains.py`): a hand fix landed in the founder's database
        and in the gitignored YAML, and nowhere git could carry it.

        Compares slugs only. `tier` and the row ORDER are expected to differ —
        stripping them is the whole reason the tracked copy exists.
        """
        legacy = _LEGACY_SEEDS / "firms.yaml"
        # Only meaningful when this run is actually reading the tracked copy.
        if firms_path.resolve() != (_DEFAULT_SEEDS / "firms.yaml").resolve():
            return
        if not legacy.exists():
            return
        try:
            old = {str(r.get("id", "")).strip()
                   for r in parse_firms_yaml(legacy.read_text(encoding="utf-8"))}
        except OSError:
            return
        new = {str(r.get("id", "")).strip() for r in firm_rows}
        only_legacy, only_tracked = sorted(old - new), sorted(new - old)
        if not only_legacy and not only_tracked:
            return
        self.stderr.write(self.style.WARNING(
            f"{legacy} has drifted from the tracked seeds and is NOT read.\n"
            + (f"  only in the archive: {', '.join(only_legacy)}\n" if only_legacy else "")
            + (f"  only in {_DEFAULT_SEEDS.name}/: {', '.join(only_tracked)}\n" if only_tracked else "")
            + f"  Edit {firms_path} — that is the copy git ships."
        ))

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
            text = path.read_text(encoding="utf-8")
            region, cycle_label, entries = parse_timeline_yaml(text)
            # The `phases:` block used to be dropped without a word. It is the
            # only place either seed file states a WINDOW — `timeline_hk.yaml`
            # says the HK applications window runs Jul-Oct 2027, and the seven
            # HK point estimates in the same file are single days inside it —
            # and `FirmDate` has one `DateField`, so there is nowhere to put a
            # start and an end. Reporting what was read is the honest middle:
            # nothing is invented, and the gap is visible to whoever runs the
            # seed instead of living in a parser comment.
            for phase in parse_timeline_phases(text):
                span = f"{phase.get('start', '?')} to {phase.get('end', '?')}"
                self.stdout.write(
                    f"phase (not stored — FirmDate holds one date, not a window): "
                    f"{region}/{phase.get('id', '?')} {span}")
            for entry in entries:
                key = str(entry.get("key", "")).strip()
                parts = key.split("/")
                if len(parts) != 3:
                    skipped += 1
                    continue
                slug, raw_cycle, event_kind = parts
                firm = firm_by_slug.get(slug)
                if firm is None:
                    skipped += 1
                    continue

                # The `key:` middle is where `sa2028_ib` / `sa2028_hk` came
                # from — this file is the writer that produced the slug
                # spelling, while import_firm_dates produced the human one.
                # Both now go through the same parser, so the yaml keeps its
                # existing keys (nothing in seeds/ has to be rewritten) and the
                # column holds one vocabulary. An unreadable key is skipped and
                # named rather than written: with the cycle CHECK constraint in
                # place a bad key would otherwise abort the whole seed run with
                # an IntegrityError halfway through.
                parsed = parse_cycle(raw_cycle)
                if parsed is None:
                    # NOT skipped. `ms/insight/insight_open` is the one live
                    # key like this, and it is a real, dated, sourced event —
                    # dropping it would take a genuine deadline off the Morgan
                    # Stanley page to tidy up a key. Nor is it guessed into the
                    # file's own `cycle: SA 2028`: the entry's note describes
                    # the "2027 Internship Recruitment" series, so the file
                    # header, the key and the note name three different things
                    # and none of them is evidence for the others.
                    #
                    # It is written with no cycle, which is a state this column
                    # has ("" = not stated), and listed by `manage.py
                    # review_firm_date_cycles` until a human files it. Migration
                    # 0014 did exactly this to the same row, so a re-seed and
                    # the migrated database agree rather than fighting.
                    self.stderr.write(self.style.WARNING(
                        f"{slug}/{event_kind}: key names no readable cycle "
                        f"({raw_cycle!r}) — stored with no cycle on file. "
                        f"See `manage.py review_firm_date_cycles`."))
                    cycle, track = "", ""
                else:
                    cycle, track = parsed

                conf_label = str(entry.get("confidence", "")).strip()
                observation = {
                    "date": entry.get("date", ""),
                    "precision": entry.get("precision", ""),
                    "confidence": conf_label,          # ORIGINAL string label, preserved
                    "source": entry.get("source", ""),
                    "found": entry.get("found", ""),
                    "note": entry.get("note", ""),
                    "cycle_label": cycle_label,        # file-level "SA 2028"
                    "seeded_from": path.name,
                }
                conf = _CONFIDENCE_BAND.get(conf_label, 0.0)

                existing = FirmDate.objects.filter(
                    firm=firm, cycle=cycle, track=track, region=region,
                    event_kind=event_kind,
                ).first()
                if dry:
                    created += 0 if existing else 1
                    updated += 1 if existing else 0
                    continue

                if existing is None:
                    FirmDate.objects.create(
                        firm=firm, cycle=cycle, track=track, region=region,
                        event_kind=event_kind,
                        date=_partial_date(entry.get("date")),
                        precision=str(entry.get("precision", "")),
                        confidence=conf,
                        source_url=str(entry.get("source", "")),
                        found_on=_found_dt(entry.get("found")),
                        history=[observation],
                    )
                    created += 1
                    continue

                # THE SAME TWO RULES `import_firm_dates` MAKES, MADE HERE TOO.
                # This used to be a plain `update_or_create(defaults=...)`,
                # which on a re-run rewrote `date`, `precision` and
                # `confidence` from the seed file and REPLACED `history` with
                # a single-entry list. Every seed in this repo is
                # `confidence: reported` (0.6), so a row the weekly radar had
                # since upgraded to `confirmed_official` off the firm's own
                # posting was silently demoted to a 2026-07-03 guess the next
                # time anyone ran `seed_directory` — the exact downgrade
                # `import_firm_dates` rule 1 exists to make impossible, in the
                # one writer that did not enforce it. And the history that
                # would have explained the move was overwritten in the same
                # save, so nothing was left to notice it by.
                #
                # No live row is currently both seed-keyed and radar-upgraded,
                # so nothing has been lost yet. That is luck, not a design:
                # `seed_directory` is what a fresh deploy runs, and it keys on
                # the same five columns the radar writes.
                #
                # A re-seed is not a new observation of the world — it is the
                # same 2026-07-03 note being read again — so an unchanged
                # re-run appends NOTHING. History grows only when the file
                # itself says something different from what is stored.
                same_claim = (
                    existing.date == _partial_date(entry.get("date"))
                    and existing.confidence == conf
                    and existing.precision == str(entry.get("precision", ""))
                )
                if same_claim:
                    updated += 1
                    continue

                existing.history = list(existing.history or [])
                if conf < existing.confidence:
                    self.stderr.write(self.style.WARNING(
                        f"{slug}/{event_kind}: seed says {conf_label} ({conf}), "
                        f"stored is {existing.confidence} — stored date kept, "
                        f"seed recorded in history."))
                    existing.history.append(
                        {**observation, "outcome": "not_applied_lower_confidence"})
                    existing.save(update_fields=["history"])
                    updated += 1
                    continue

                existing.date = _partial_date(entry.get("date"))
                existing.precision = str(entry.get("precision", ""))
                existing.confidence = conf
                existing.source_url = str(entry.get("source", ""))
                existing.found_on = _found_dt(entry.get("found"))
                existing.history.append(observation)
                existing.save()
                updated += 1
        return created, updated, skipped
