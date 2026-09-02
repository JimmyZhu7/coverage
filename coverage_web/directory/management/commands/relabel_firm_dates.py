"""relabel_firm_dates — repair the three things wrong with the stored dates.

    python manage.py relabel_firm_dates            # DRY RUN, the default
    python manage.py relabel_firm_dates --apply

DRY RUN IS THE DEFAULT AND THAT IS DELIBERATE. `FirmDate` is SHARED directory
data: every user of Coverage reads the same rows, so a bad write here is not
one person's mistake, it is everybody's. Nothing is written without `--apply`,
and the three sections below print exactly what `--apply` would do.

WHY A COMMAND AND NOT A MIGRATION. Two of the three repairs change `cycle`,
which is part of `FirmDate`'s unique key, so they are moves rather than edits
and can collide. A migration would run itself on every deploy and on every
fresh database, where none of these rows exist; this runs once, against the
one database that has them, after a human has read the tables.

WHAT IT REPAIRS
---------------
1. MISLABELLED CYCLES. `import_firm_dates` used to default `--cycle "SA 2028"`,
   and on the 2026-08-02 radar run that default was stamped on six Hong Kong
   deadlines that belong to the SA 2027 intake: Morgan Stanley 27 Sep 2026,
   J.P. Morgan 30 Sep, HSBC 30 Oct, UBS 3 Aug, BlackRock 31 Aug, Bain 31 Aug.
   Grade A, `scratchpad/research-hongkong.md` §1 — the research quotes the same
   postings the radar read. The importer now infers the cycle from region and
   date instead (`import_firm_dates.infer_cycle`) and refuses a default that
   contradicts it; this section fixes the rows already written under the old
   behaviour, using that same function so the repair and the guard cannot
   disagree.

   What it was costing: HSBC's 30 Oct 2026 close badged "your cycle" on the
   firm page for a student recruiting for SA 2028; `firm_lookup` handing the
   advisor `cycle: sa2028`; and `_drop_contradicted_openings` suppressing the
   correct SA 2028 Hong Kong opening estimate on four firm pages because a
   deadline "in the same cycle" appeared to contradict it.

2. UNVERIFIABLE CONFIRMED DATES. A row at `confidence=1.0` with no source, no
   market and no cycle on file is asserting the strongest thing this table can
   say on the weakest possible basis. Two exist (gs id 48, 22 Sep 2026; jpm id
   47, 30 Aug 2026), both written 2026-08-28 with a single history entry.
   Neither is corroborated by either research pass — the Hong Kong research
   found no Goldman HK deadline at all, and the US Goldman IB SA 2027 close
   was February 2026, so a 22 Sep 2026 Goldman close matches nothing.
   `confirmed_official` is the bar the calendar, the .ics feed and the Today
   rail all act on, and the gs row was the second item on the founder's own
   rail with two phone alarms behind it.

   NOT DELETED (P4: mark, never drop). Somebody saw something. The row keeps
   its date and drops to `rumor`, which is what an uncorroborated claim is,
   and the reason goes into `history` where the next reader can weigh it.

3. SEEDED ESTIMATES. `directory/seeds/timeline_{hk,us}.yaml` were re-dated
   2026-09-01 from the two research passes: Hong Kong gained the `app_close`
   rows it had none of and its openings moved from ~Sep 2027 to Jul-Aug 2027;
   eleven US bulge-bracket openings moved from Feb-Apr 2027 to Nov 2026-Jan
   2027 per that research's Rule 2. This section shows the delta between those
   files and the database and, with `--apply`, hands the writing to
   `seed_directory`'s own `_seed_firm_dates` — the same code path a fresh
   deploy uses, including its no-downgrade and append-only history rules — so
   this command never becomes a second writer with its own opinions.

   Every one of those rows is `precision: estimated`, which
   `crm.utils.confirmed_firm_dates` excludes, so none of them can reach the
   calendar, the subscribed feed or the deadlines rail. They are forecasts,
   they say so, and they stay on the firm page where a forecast belongs.
"""

from __future__ import annotations

from pathlib import Path

from django.core.management.base import BaseCommand
from django.utils import timezone

from directory.management.commands.import_firm_dates import (
    CONFIDENCE_BAND,
    infer_cycle,
)
from directory.management.commands.seed_directory import (
    _DEFAULT_SEEDS,
    Command as SeedCommand,
    _partial_date,
)
from directory.models import FirmDate
from directory.seed_parsers import parse_timeline_yaml
from directory.timeline import parse_cycle

#: Where an uncorroborated `confidence=1.0` row lands. Not 0.0: the claim was
#: made and somebody recorded it, which is what `rumor` means in this
#: vocabulary. 0.0 would read as "no confidence recorded", which is a
#: different and less honest statement.
_UNVERIFIABLE_BAND = "rumor"

_TIMELINE_FILES = ("timeline_hk.yaml", "timeline_us.yaml")


class Command(BaseCommand):
    help = ("Repair mislabelled cycles, downgrade unverifiable confirmed "
            "dates, and re-date the seeded estimates. Dry run by default.")

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply", action="store_true",
            help="Actually write. Without it this command only reports.")
        parser.add_argument(
            "--seeds", default=str(_DEFAULT_SEEDS),
            help="Directory holding the timeline_*.yaml seeds.")

    def handle(self, *args, **opts):
        apply = opts["apply"]
        self.tag = "" if apply else "[dry-run] "
        seeds = Path(opts["seeds"])

        # ORDER MATTERS, AND SO DOES TELLING THE TRUTH ABOUT IT IN A DRY RUN.
        # Six of the Hong Kong closes currently sit at `(firm, sa2028, "", hk,
        # app_close)` — the exact key the re-dated HK seeds want. Section 1
        # moves them to `sa2027` first, so by the time section 3 writes, the
        # key is free and the seed CREATES a SA 2028 estimate beside the real
        # SA 2027 deadline. Run the other way round, the seed would overwrite
        # Morgan Stanley's genuine 27 Sep 2026 close with a 2027 guess.
        #
        # In a dry run nothing has moved yet, so section 3 would report those
        # six as overwrites of real dates — the very thing the ordering
        # prevents. It is told which rows section 1 is about to vacate, so the
        # table the founder reads is the table `--apply` produces.
        moved, vacating = self._relabel_cycles(apply)
        downgraded = self._downgrade_unverifiable(apply)
        seeded = self._reseed_estimates(seeds, apply, vacating)

        verb = "wrote" if apply else "would write"
        self.stdout.write(self.style.SUCCESS(
            f"{self.tag}{verb}: {moved} cycle relabel(s), "
            f"{downgraded} confidence downgrade(s), "
            f"{seeded} seeded estimate(s) changed."))
        if not apply:
            self.stdout.write(
                "Nothing was written. Re-run with --apply once the tables "
                "above have been read.")

    # ------------------------------------------------------------------ 1
    def _relabel_cycles(self, apply: bool) -> tuple[int, set[int]]:
        """Rows whose stored cycle contradicts the region+date rule.

        Returns the number moved and the pks of the rows that are leaving
        their current key, which section 3 needs so its dry run describes the
        database as it will be, not as it was.
        """
        self.stdout.write("")
        self.stdout.write(self.style.MIGRATE_HEADING(
            "1. Cycle relabels (region + date disagree with the stored cycle)"))
        self.stdout.write(
            f"{'id':>4}  {'firm':<12} {'region':<7} {'event':<12} "
            f"{'date':<12} {'stored':<8} -> {'correct':<8}")

        rows = []
        for fd in FirmDate.objects.select_related("firm").order_by("id"):
            correct = infer_cycle(fd.region, fd.event_kind, fd.date)
            if correct and correct != fd.cycle:
                rows.append((fd, correct))

        if not rows:
            self.stdout.write("  (none)")
            return 0, set()

        moved = 0
        vacating: set[int] = set()
        now = timezone.now()
        for fd, correct in rows:
            self.stdout.write(
                f"{fd.id:>4}  {fd.firm.slug:<12} {fd.region or '-':<7} "
                f"{fd.event_kind:<12} {str(fd.date):<12} "
                f"{fd.cycle or '(none)':<8} -> {correct:<8}")
            # `cycle` is part of the unique key, so this is a MOVE and it can
            # land on an occupied key. Merging two rows is a judgement about
            # which date is right, which is not a decision a relabelling
            # command gets to make.
            clash = (FirmDate.objects
                     .filter(firm=fd.firm, cycle=correct, track=fd.track,
                             region=fd.region, event_kind=fd.event_kind)
                     .exclude(pk=fd.pk).first())
            if clash is not None:
                self.stderr.write(self.style.WARNING(
                    f"      SKIPPED: id {clash.id} already holds "
                    f"{fd.firm.slug}/{correct}/{fd.region}/{fd.event_kind}. "
                    f"Two rows for one scope is a data question, not a "
                    f"relabelling one. Left alone."))
                continue
            moved += 1
            vacating.add(fd.pk)
            if apply:
                fd.history = list(fd.history or []) + [{
                    "date": str(fd.date or ""),
                    "confidence": _band_label(fd.confidence),
                    "source": fd.source_url,
                    "note": (f"cycle relabelled {fd.cycle or '(none)'} -> "
                             f"{correct}: a {fd.region} close on {fd.date} is "
                             f"that intake by the region+date rule in "
                             f"import_firm_dates.infer_cycle "
                             f"(research-hongkong.md §1, Grade A; "
                             f"research-us-ib-calendar.md Rule 2). The stored "
                             f"cycle came from the old --cycle default."),
                    "seen": now.isoformat(),
                    "outcome": "cycle_relabelled",
                }]
                fd.cycle = correct
                fd.save(update_fields=["cycle", "history"])
        return moved, vacating

    # ------------------------------------------------------------------ 2
    def _downgrade_unverifiable(self, apply: bool) -> int:
        """`confidence=1.0` rows with no source, no market and no cycle."""
        self.stdout.write("")
        self.stdout.write(self.style.MIGRATE_HEADING(
            "2. Unverifiable confirmed dates (no source, no market, no cycle)"))
        self.stdout.write(
            f"{'id':>4}  {'firm':<12} {'event':<12} {'date':<12} "
            f"{'conf':<6} -> {'conf':<6}  verdict")

        rows = list(FirmDate.objects.select_related("firm")
                    .filter(confidence=CONFIDENCE_BAND["confirmed_official"],
                            source_url="", region="", cycle="")
                    .order_by("id"))
        if not rows:
            self.stdout.write("  (none)")
            return 0

        target = CONFIDENCE_BAND[_UNVERIFIABLE_BAND]
        now = timezone.now()
        for fd in rows:
            self.stdout.write(
                f"{fd.id:>4}  {fd.firm.slug:<12} {fd.event_kind:<12} "
                f"{str(fd.date):<12} {fd.confidence:<6} -> {target:<6}  "
                f"unverifiable, confidence should be estimated")
            if apply:
                fd.history = list(fd.history or []) + [{
                    "date": str(fd.date or ""),
                    "confidence": _UNVERIFIABLE_BAND,
                    "source": "",
                    "note": ("downgraded from confirmed_official: the row "
                             "carries no source_url, no region and no cycle, "
                             "and neither research pass of 2026-09-01 "
                             "corroborates it. The date is kept; the claim "
                             "that a firm published it is not."),
                    "seen": now.isoformat(),
                    "outcome": "downgraded_unverifiable",
                }]
                fd.confidence = target
                fd.save(update_fields=["confidence", "history"])
        return len(rows)

    # ------------------------------------------------------------------ 3
    def _reseed_estimates(self, seeds: Path, apply: bool,
                          vacating: set[int]) -> int:
        """The delta between the re-dated seed files and the database."""
        self.stdout.write("")
        self.stdout.write(self.style.MIGRATE_HEADING(
            "3. Seeded estimates (timeline_*.yaml, re-dated from the research)"))
        self.stdout.write(
            f"{'firm':<12} {'region':<7} {'event':<12} "
            f"{'stored':<12} -> {'seed':<12}  confidence")

        files = [seeds / name for name in _TIMELINE_FILES]
        changed = 0
        for path in files:
            if not path.exists():
                self.stderr.write(self.style.WARNING(
                    f"timeline file not found (skipped): {path}"))
                continue
            region, _cycle_label, entries = parse_timeline_yaml(
                path.read_text(encoding="utf-8"))
            for entry in entries:
                parts = str(entry.get("key", "")).strip().split("/")
                if len(parts) != 3:
                    continue
                slug, raw_cycle, event_kind = parts
                parsed = parse_cycle(raw_cycle)
                cycle, track = parsed if parsed else ("", "")
                stored = (FirmDate.objects
                          .filter(firm__slug=slug, cycle=cycle, track=track,
                                  region=region, event_kind=event_kind)
                          .exclude(pk__in=vacating)
                          .first())
                want_date = _partial_date(entry.get("date"))
                conf_label = str(entry.get("confidence", "")).strip()
                if stored is None:
                    changed += 1
                    self.stdout.write(
                        f"{slug:<12} {region:<7} {event_kind:<12} "
                        f"{'(new)':<12} -> {str(want_date):<12}  {conf_label}")
                    continue
                if (stored.date == want_date
                        and stored.confidence == CONFIDENCE_BAND.get(conf_label, 0.0)):
                    continue
                changed += 1
                self.stdout.write(
                    f"{slug:<12} {region:<7} {event_kind:<12} "
                    f"{str(stored.date):<12} -> {str(want_date):<12}  "
                    f"{_band_label(stored.confidence)} -> {conf_label}")

        if not changed:
            self.stdout.write("  (none)")
        elif apply:
            # ONE WRITER. `seed_directory._seed_firm_dates` already refuses to
            # downgrade a confirmed row and already appends rather than
            # replaces history; re-implementing either here is how two writers
            # of one table start disagreeing.
            SeedCommand().handle_firm_dates_only(files)
        return changed


def _band_label(value: float) -> str:
    """A stored float back to the vocabulary it came from, or its number."""
    for label, level in CONFIDENCE_BAND.items():
        if round(float(value or 0.0), 1) == level:
            return label
    return str(value)
