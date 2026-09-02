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
      "cycle": "SA 2028",               # optional; "sa2028" / "sa2028_ib"
                                        #   also read. When absent, the
                                        #   region+date rule in `infer_cycle`
                                        #   decides, and --cycle is only a
                                        #   fallback for what that rule cannot
                                        #   place. There is no bare default.
      "track": "ib",                    # optional desk: ib | st | pe | am |
                                        #   consulting | corp-strat
      "region": "hk",                   # optional ("" = unscoped)
      "precision": "day",               # optional: "" | day | month | estimated
      "confidence": "confirmed_official",   # rumor | reported |
                                            #   confirmed_official
      "source": "https://...",          # where the claim came from
      "note": "opens right after Labor Day"     # optional, free text
    }

An unknown firm slug is SKIPPED and named, never silently dropped and never
auto-created: a typo'd slug that invented a firm would pollute the shared
directory every other user reads.

EVERY VOCABULARY FIELD IS CHECKED BEFORE IT IS WRITTEN
------------------------------------------------------
`event_kind`, `cycle`, `track`, `precision` and `confidence` are all closed
vocabularies, and an entry that misses any of them is skipped with the firm
named — never written, never coerced. Three of those five checks are new, and
each replaces a silent failure this command actually had:

  - `cycle` was written verbatim, which is how the table came to hold four
    spellings of one cycle and why nothing could group a programme across
    firms. See `directory.timeline`.
  - `precision` was written verbatim into the field that decides whether a
    date renders as an exact day, a month, or an estimate. Anything the
    renderer does not recognise falls through to the EXACT-DAY branch, so a
    typo turns a guess into a specific date on a public page.
  - `confidence` fell back to 0.0 for an unrecognised band. On a new row that
    writes a real date that every `>= 0.8` reader downstream then discards; on
    an existing one, rule 1 below reads it as a downgrade and quietly keeps the
    old date. Both look exactly like a successful import.

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
from directory.timeline import CYCLE_TRACKS, is_valid_track, parse_cycle

# Same three-band vocabulary seed_directory uses; the float is what the
# cadence engine compares against (1.0 == confirmed_official).
CONFIDENCE_BAND = {"rumor": 0.3, "reported": 0.6, "confirmed_official": 1.0}

# The events the backward planner knows how to act on, plus insight_open,
# which the directory tracks for display. Anything else is a typo.
EVENT_KINDS = ("app_open", "app_close", "insight_open", "insight_deadline")

# What `_firm_date_row` in directory/views.py knows how to render a date AT.
# "" is a full day; "month" prints "Sep 2026"; "estimated" prints "~ Sep 2026"
# and can never be badged confirmed. A fourth value is not a fourth rendering —
# it falls through to the exact-day branch, which is how an estimate once
# printed as a specific day. This command wrote whatever the findings said.
PRECISIONS = ("", "day", "month", "estimated")


# ---------------------------------------------------------------------------
# WHICH CYCLE A DEADLINE BELONGS TO, WHEN THE FINDING DOES NOT SAY
# ---------------------------------------------------------------------------
# `--cycle` used to default to "SA 2028", and a default is not a fact. On the
# 2026-08-02 radar run six Hong Kong deadlines came in with no `cycle` of their
# own and were stamped `sa2028` by that default: Morgan Stanley 27 Sep 2026,
# J.P. Morgan 30 Sep, HSBC 30 Oct, UBS 3 Aug, BlackRock 31 Aug, Bain 31 Aug.
# Every one of them is the SA 2027 Hong Kong intake. The damage was not
# cosmetic: the firm page badged HSBC's Oct 2026 close "your cycle" for a
# student recruiting for SA 2028, `firm_lookup` handed the advisor
# `cycle: sa2028`, and `_drop_contradicted_openings` in directory/views.py
# suppressed the CORRECT SA 2028 Hong Kong estimate on four firm pages because
# a close "in the same cycle" appeared to contradict it.
#
# So the market and the month decide, and they can, because the two markets
# this product covers run on calendars that do not overlap:
#
#   HONG KONG. An application that CLOSES in Jul-Oct of year Y belongs to
#   SA Y+1. Grade A, `scratchpad/research-hongkong.md` §1: Morgan Stanley's HK
#   SA 2027 "recruitment began on July 7, 2026" with staged deadlines of
#   16 Aug and 27 Sep 2026; Bank of America and J.P. Morgan HK SA 2027 close
#   30 Sep 2026; Citi and HSBC HK SA 2027 close 30 Oct 2026. HK recruits on
#   the London pattern — apply the autumn before the summer — not the US one.
#
#   UNITED STATES. An application that CLOSES in Aug-Dec of year Y belongs to
#   SA Y+2. `scratchpad/research-us-ib-calendar.md` Rule 2 (n=21 firms across
#   two cycles, Grade C+ aggregate): BB/EB postings first appear a median 17-18
#   months before a June start, so the Aug-Dec window of Y is the early-ID and
#   diversity wave of the intake that runs in the summer of Y+2, not Y+1 —
#   the SA Y+1 US classes were already filled by the spring of Y.
#
# NOTHING ELSE IS INFERRED. Only `app_close`, and only for `hk` and `us`, and
# only inside those month bands. An opening is deliberately excluded: the
# research dates HK openings (Jul 2026 at MS, 1 Jul at Barclays) but a
# first-posting month is a distribution, not a rule, and Rule 3 of the US
# research says the spread is still moving. Outside the rule this function
# returns "" and the caller falls back to `--cycle` or refuses — it never
# guesses (P1: silence beats a confident guess).
_INFERRED_CLOSE_KINDS = ("app_close",)

#: market -> (months the rule covers, how many years ahead the intake runs).
_CLOSE_CYCLE_RULE = {
    "hk": (range(7, 11), 1),    # Jul-Oct of Y -> SA Y+1
    "us": (range(8, 13), 2),    # Aug-Dec of Y -> SA Y+2
}


def infer_cycle(region: str, event_kind: str, on: date | None) -> str:
    """The cycle slug a closing date in a known market must belong to, or "".

    "" is not a cycle and not a failure: it means the rule above has nothing
    to say about this row, and the caller must get its cycle from the finding
    or from an explicit `--cycle`.
    """
    if on is None or event_kind not in _INFERRED_CLOSE_KINDS:
        return ""
    rule = _CLOSE_CYCLE_RULE.get(str(region or "").strip().lower())
    if rule is None:
        return ""
    months, ahead = rule
    if on.month not in months:
        return ""
    return f"sa{on.year + ahead}"


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
        # NO DEFAULT. A default here is a claim the operator never made, and
        # this one made six wrong claims (see `infer_cycle` above). Now it is
        # a FALLBACK for the rows the region+date rule cannot speak to, and an
        # entry that gets neither is skipped with the firm named rather than
        # written under a guess.
        parser.add_argument("--cycle", default=None,
                            help="Cycle label for entries that omit one AND that "
                                 "the region+date rule cannot place. Never "
                                 "overrides that rule.")
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

            # The market and the date are read BEFORE the cycle, because on a
            # finding that does not state a cycle they are what decides it.
            # See `infer_cycle`.
            region = str(entry.get("region", "")).strip().lower()
            try:
                new_date = _parse_date(entry.get("date"))
            except BadDate as exc:
                self.stderr.write(self.style.WARNING(
                    f"skip {slug}/{event_kind}: unreadable date {exc!s:.40} "
                    f"(expected YYYY-MM-DD, or \"\" for a known-unknown). "
                    f"Any stored date is left alone."))
                skipped += 1
                continue

            # `cycle` used to be written through verbatim — the ONLY key on
            # the row that was never checked, while `event_kind` above was
            # matched against a closed tuple and `confidence` against a closed
            # dict. That is how the table came to hold four spellings of "SA
            # 2028": this command's own `--cycle` default writes the human
            # one, `seed_directory` writes the slug, and neither could see the
            # other. `parse_cycle` reads every spelling and returns the one
            # canonical pair; None means the value does not name a cycle, and
            # is skipped with the firm named rather than stored — the same
            # posture `_parse_date` takes toward an unreadable date.
            #
            # THREE SOURCES, IN THIS ORDER. A cycle stated on the finding wins
            # (P2: stated beats derived); failing that, the region+date rule;
            # failing that, `--cycle`. What is NOT allowed is the combination
            # that produced the six mislabelled Hong Kong rows: a finding with
            # no cycle of its own, a market and a month that place it exactly,
            # and a `--cycle` on the command line that says otherwise. That is
            # not a fallback, it is a contradiction, and it is refused.
            stated_cycle = str(entry.get("cycle") or "").strip()
            fallback_cycle = str(opts["cycle"] or "").strip()
            inferred = infer_cycle(region, event_kind, new_date)
            if stated_cycle:
                raw_cycle = stated_cycle
            elif inferred:
                raw_cycle = inferred
                fallback_parsed = parse_cycle(fallback_cycle) if fallback_cycle else None
                if fallback_parsed is not None and fallback_parsed[0] != inferred:
                    self.stderr.write(self.style.WARNING(
                        f"skip {slug}/{event_kind}: --cycle {fallback_cycle!r} "
                        f"contradicts the {region} calendar, which places a close "
                        f"on {new_date} in {inferred}. State the cycle on the "
                        f"finding if the rule is wrong. "
                        f"Any stored date is left alone."))
                    skipped += 1
                    continue
            elif fallback_cycle:
                raw_cycle = fallback_cycle
            else:
                self.stderr.write(self.style.WARNING(
                    f"skip {slug}/{event_kind}: no cycle on the finding, none "
                    f"the region+date rule can infer, and no --cycle given. "
                    f"A cycle is a key on this row, not a decoration. "
                    f"Any stored date is left alone."))
                skipped += 1
                continue

            parsed = parse_cycle(raw_cycle)
            if parsed is None:
                self.stderr.write(self.style.WARNING(
                    f"skip {slug}/{event_kind}: unreadable cycle {raw_cycle!r} "
                    f"(expected a season+year like \"SA 2028\" or \"sa2028\", "
                    f"optionally suffixed with a desk: sa2028_ib). "
                    f"Any stored date is left alone."))
                skipped += 1
                continue
            cycle, cycle_track = parsed
            # A finding that names its own cycle is evidence and is written as
            # stated, but a disagreement with the calendar rule is said out
            # loud rather than swallowed — it is either a firm doing something
            # new or a finding that needs a second look.
            if stated_cycle and inferred and cycle != inferred:
                self.stdout.write(self.style.WARNING(
                    f"{tag}NOTE {slug}/{event_kind}: finding says {cycle}, the "
                    f"{region} calendar says {inferred} for a close on "
                    f"{new_date}. Writing what the finding says."))
            # An explicit `track` on the finding beats one inferred from a
            # cycle suffix, and disagreeing with the suffix is an error rather
            # than a preference — a finding that says both things must not
            # have one of them silently chosen for it.
            track = str(entry.get("track", "")).strip().lower() or cycle_track
            if not is_valid_track(track):
                self.stderr.write(self.style.WARNING(
                    f"skip {slug}/{event_kind}: unknown track {track!r} "
                    f"(expected one of {', '.join(CYCLE_TRACKS)}, or omit it). "
                    f"Any stored date is left alone."))
                skipped += 1
                continue
            if cycle_track and track != cycle_track:
                self.stderr.write(self.style.WARNING(
                    f"skip {slug}/{event_kind}: track {track!r} contradicts the "
                    f"desk in cycle {raw_cycle!r}. Any stored date is left alone."))
                skipped += 1
                continue

            precision = str(entry.get("precision", "")).strip().lower()
            if precision not in PRECISIONS:
                self.stderr.write(self.style.WARNING(
                    f"skip {slug}/{event_kind}: unknown precision {precision!r} "
                    f"(expected one of {', '.join(repr(p) for p in PRECISIONS)}). "
                    f"An unrecognised precision renders as an EXACT day. "
                    f"Any stored date is left alone."))
                skipped += 1
                continue

            conf_label = str(entry.get("confidence", "")).strip()
            if conf_label not in CONFIDENCE_BAND:
                # Silently scoring 0.0 for a typo'd band is a downgrade wearing
                # the clothes of a value: rule 1 below would then treat the
                # finding as weaker than anything stored and quietly keep the
                # old date, or — on a NEW row — write a real date at zero
                # confidence, which every `>= 0.8` reader downstream discards.
                self.stderr.write(self.style.WARNING(
                    f"skip {slug}/{event_kind}: unknown confidence "
                    f"{conf_label!r} (expected one of "
                    f"{', '.join(CONFIDENCE_BAND)}). Any stored date is left alone."))
                skipped += 1
                continue
            conf = CONFIDENCE_BAND[conf_label]

            observation = {
                "date": str(entry.get("date", "")),
                "confidence": conf_label,
                "source": str(entry.get("source", "")),
                "note": str(entry.get("note", "")),
                "seen": now.isoformat(),
            }

            existing = FirmDate.objects.filter(
                firm=firm, cycle=cycle, track=track, region=region,
                event_kind=event_kind,
            ).first()

            if existing is None:
                created += 1
                self.stdout.write(f"{tag}NEW  {slug}/{event_kind} "
                                  f"{new_date or '(no date)'} [{conf_label}]")
                if not dry:
                    FirmDate.objects.create(
                        firm=firm, cycle=cycle, track=track, region=region,
                        event_kind=event_kind,
                        date=new_date, precision=precision,
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
                existing.precision = precision or existing.precision
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
