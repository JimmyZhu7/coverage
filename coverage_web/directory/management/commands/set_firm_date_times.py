"""set_firm_date_times — attach the closing HOUR to the rows that have one.

    python manage.py set_firm_date_times            # DRY RUN, the default
    python manage.py set_firm_date_times --apply

DRY RUN IS THE DEFAULT, for the reason `relabel_firm_dates` states first:
`FirmDate` is SHARED directory data. Every user of Coverage reads the same
rows, so a bad write here is not one person's mistake, it is everybody's.

WHAT THIS IS FOR. `FirmDate.date` is a bare day with no hour and no zone,
and every renderer therefore treats a deadline as lasting until midnight in
the READER's own zone. Real closes are instants: Citi's Hong Kong SA 2027
deadline is "Friday, October 30, 2026 at 23:59 HKT", which for a Los Angeles
student is 08:59 that morning. The row stayed on the deadlines rail, alarm
and all, for the rest of that Californian day.

WHY A COMMAND AND NOT A MIGRATION, and not a scraper. A migration runs on
every deploy and on every fresh database, where none of these rows exist. A
scraper would have to READ an hour out of prose, and prose deadlines are
already 96% of what this product regrets: `extract_deadlines_ai` was declined
on exactly that ground. This reads a hand-checked findings file where every
entry carries the sentence it came from, and it is expected to run about once
a cycle.

THE FOUR REFUSALS. Each one exists because writing anyway would produce a
date that looks more certain than the evidence behind it.

1. NOT `confirmed_official`. `crm.utils.confirmed_firm_dates` is the bar the
   calendar, the .ics feed and the Today rail all act on: confidence 1.0 AND
   a precision that locates a real day. A time on a row below that bar dresses
   a rumour in an hour. Morgan Stanley's 27 Sep 2026 Hong Kong close is the
   live example — the 23:55 HKT is Grade A, the row is `reported`, so the
   hour waits for the row to be confirmed rather than confirming it.
2. A `month` precision, or no date. An hour on "fall 2026" would combine into
   an instant on the first of the month that nobody stated. The database
   refuses this too (`firm_dates_close_time_needs_a_day`); the command
   refuses it first, with a sentence instead of an IntegrityError.
3. THE DAY MOVED. A time is a fact about one close. If the stored row's date
   no longer matches the entry's, the entry is describing a different
   deadline — the six Hong Kong cycle relabels of 2026-09-02 are what that
   looks like in practice — and attaching the hour anyway would put last
   cycle's time on this cycle's date.
4. NO ROW AT ALL. An entry that matches nothing is reported and skipped; this
   command does not create `FirmDate` rows. `import_firm_dates` owns that,
   and a file about times should not be able to mint a deadline.

HISTORY IS APPENDED, NEVER REPLACED, the same rule every other writer on this
table follows: the entry records the hour, the zone, the source and the
quote, so the next reader can weigh the claim instead of taking the column's
word for it.
"""

from __future__ import annotations

from datetime import datetime, time as _time
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.core.management.base import BaseCommand
from django.utils import timezone

from crm.utils import CONFIRMED_CONFIDENCE
from directory.models import FirmDate
from directory.seed_parsers import parse_firm_date_times

#: Precisions that locate a real DAY, and so can carry an hour. Narrower than
#: `crm.utils.CONFIRMED_PRECISIONS` on purpose: that tuple includes "month",
#: which is a legitimate confirmed precision and an illegitimate place to
#: hang a time. Mirrored by the `firm_dates_close_time_needs_a_day` check
#: constraint — the constraint is the guard, this is the message.
DAY_PRECISIONS = ("", "day")

_DEFAULT_FINDINGS = Path(__file__).resolve().parents[2] / "seeds" / "firm_date_times.yaml"


class Command(BaseCommand):
    help = ("Attach the published closing time and zone to confirmed firm "
            "dates that state one. Dry run by default.")

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply", action="store_true",
            help="Actually write. Without it this command only reports.")
        parser.add_argument(
            "--findings", default=str(_DEFAULT_FINDINGS),
            help="The YAML file of published closing times.")

    def handle(self, *args, **opts):
        apply = opts["apply"]
        tag = "" if apply else "[dry-run] "
        path = Path(opts["findings"])
        entries = parse_firm_date_times(path.read_text())

        self.stdout.write(self.style.MIGRATE_HEADING(
            f"Closing times from {path.name} ({len(entries)} entr"
            f"{'y' if len(entries) == 1 else 'ies'})"))

        written = 0
        skipped: list[str] = []
        now = timezone.now()
        for entry in entries:
            row, reason = self._match(entry)
            label = (f"{entry.get('firm')}/{entry.get('cycle') or '-'}/"
                     f"{entry.get('region') or '-'}/{entry.get('event_kind')}")
            if row is None:
                skipped.append(f"{label}: {reason}")
                continue

            close_time = self._parse_time(entry.get("time"))
            zone_name = str(entry.get("tz") or "")
            if close_time is None:
                skipped.append(f"{label}: unreadable time {entry.get('time')!r}")
                continue
            try:
                ZoneInfo(zone_name)
            except (ZoneInfoNotFoundError, ValueError):
                skipped.append(f"{label}: {zone_name!r} is not an IANA zone key")
                continue

            if row.close_time == close_time and row.close_tz == zone_name:
                skipped.append(f"{label}: already set to {close_time} {zone_name}")
                continue

            self.stdout.write(
                f"  {row.id:>4}  {row.firm.slug:<12} {row.event_kind:<12} "
                f"{row.date}  -> {close_time:%H:%M} {zone_name}")
            written += 1
            if not apply:
                continue
            row.close_time = close_time
            row.close_tz = zone_name
            row.history = list(row.history or []) + [{
                "seen": now.isoformat(),
                "outcome": "close_time_set",
                "close_time": f"{close_time:%H:%M}",
                "close_tz": zone_name,
                "source": str(entry.get("source") or ""),
                "note": str(entry.get("quote") or "").strip(),
            }]
            row.save(update_fields=["close_time", "close_tz", "history"])

        if skipped:
            self.stdout.write("")
            self.stdout.write(self.style.MIGRATE_HEADING("Not written, and why"))
            for line in skipped:
                self.stdout.write(f"  {line}")

        self.stdout.write("")
        verb = "set" if apply else "would set"
        self.stdout.write(self.style.SUCCESS(
            f"{tag}{verb} a closing time on {written} row(s); "
            f"{len(skipped)} entr{'y' if len(skipped) == 1 else 'ies'} skipped."))
        if not apply:
            self.stdout.write(
                "Nothing was written. Re-run with --apply once the rows above "
                "have been read.")

    # ------------------------------------------------------------------
    def _match(self, entry) -> tuple[FirmDate | None, str]:
        """The one stored row this entry describes, or None and the reason.

        Matched on the full scope — firm, cycle, track, region, event_kind —
        because that IS `FirmDate`'s unique key, and matching on anything
        less would let one entry silently land on a sibling row of the same
        firm in a different market.
        """
        rows = list(FirmDate.objects.select_related("firm").filter(
            firm__slug=str(entry.get("firm") or ""),
            cycle=str(entry.get("cycle") or ""),
            track=str(entry.get("track") or ""),
            region=str(entry.get("region") or ""),
            event_kind=str(entry.get("event_kind") or ""),
        ))
        if not rows:
            return None, "no stored row with that scope"
        row = rows[0]
        stated = self._parse_date(entry.get("date"))
        if stated is None:
            return None, f"unreadable date {entry.get('date')!r}"
        if row.date != stated:
            return None, (f"the stored row closes {row.date}, the finding "
                          f"describes {stated} — a different deadline")
        if row.confidence != CONFIRMED_CONFIDENCE:
            return None, (f"not confirmed_official (confidence "
                          f"{row.confidence}); an hour would dress a rumour")
        if (row.precision or "") not in DAY_PRECISIONS:
            return None, (f"precision {row.precision!r} locates no single day, "
                          f"so it can carry no hour")
        return row, ""

    @staticmethod
    def _parse_date(raw):
        """`2026-09-27` off the findings file, as a `date`.

        The parser hands back strings; a bad one becomes a skipped entry with
        a reason, never an exception. A date is required — an entry without
        one cannot make the "is this still the same deadline" check that
        stops last cycle's hour landing on this cycle's row.
        """
        if isinstance(raw, datetime):
            return raw.date()
        try:
            return datetime.strptime(str(raw or "").strip(), "%Y-%m-%d").date()
        except ValueError:
            return None

    @staticmethod
    def _parse_time(raw) -> _time | None:
        if isinstance(raw, _time):
            return raw
        if isinstance(raw, datetime):
            return raw.time()
        text = str(raw or "").strip()
        for fmt in ("%H:%M", "%H:%M:%S"):
            try:
                return datetime.strptime(text, fmt).time()
            except ValueError:
                continue
        return None
