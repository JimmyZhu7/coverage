"""backfill_class_year_derived — recompute Opportunity.class_year_derived
from classify.derive_class_year and repair rows where the stored column has
drifted from what a live recompute answers today (report-only by default).

    python manage.py backfill_class_year_derived            # report only (default)
    python manage.py backfill_class_year_derived --commit     # write the repairs

WHY THIS EXISTS
---------------
`class_year_derived` is written once, at ingest (directory.ingest.
_apply_opportunity), and never touched again by anything else. A row written
before a `derive_class_year` rule change, or before its own title/cohort/
bucket were corrected by a later pass (reclassify, a title split, a bucket
fix), is stuck holding whatever the column read on the day it was last
saved. A read-only measurement against the live DB found 247 of 2,662 open
campus rows (9%) where a fresh `derive_class_year(bucket, title, cohort)`
call now derives a year the stored column still reads BLANK for — and only
that shape: zero rows where a stored value CHANGED to a different value,
zero where a stored value went to blank. `role_matches_level`
(directory/recommend.py) and `directory.views._eligibility` both read this
column to decide whether a role should surface to a student filtering by
class year, so those 247 rows are current UNDER-FILTERING, not
misclassification: real, derivable programme years silently invisible to a
level filter that should already be matching them. Nothing is being wrongly
blocked.

`derive_class_year` returns a `(year, justification)` TUPLE, not a bare
string. A first pass at measuring this comparing the tuple itself to the
stored string reported 100% of rows stale — every tuple fails `==` against
a plain string, whatever the year. This command (and its tests) always
compare `derive_class_year(...)[0]` to the stored value, never the tuple.

THREE CLASSES OF CHANGE, NOT ONE
---------------------------------
blank -> value    is what a derivation catching up to a title/cohort/bucket
                   fix looks like, and is the only class the live
                   measurement actually found. Safe to sweep in as one
                   block: it can only make a role MORE visible to a level
                   filter, never less — both readers above treat this
                   column as strictly non-blocking (see the field's own
                   comment in directory/models.py and the "never allowed to
                   tell a student they are INELIGIBLE" rule in classify.py's
                   derive_class_year commentary).
value -> blank    means a role that used to carry an inferred year no
                   longer does — most often because a stated `class_year`
                   was added since (this command mirrors ingest's own rule,
                   `"" if class_year else derive_class_year(...)[0]`, so a
                   stated year always wins to blank), or the title/bucket/
                   cohort changed in a way that no longer fits either
                   derivable shape. A student who matched on the old
                   inferred year would stop matching.
value -> changed  means the inferred year itself moved — a title or cohort
                   correction changed which class the role now derives.
                   A student who matched the OLD year is told nothing now,
                   and one who matches the NEW year starts seeing a role
                   they didn't before.

Only the first class is swept in as a block. The other two change what a
student reads as ELIGIBLE FOR (or likely-eligible for), so every row in
either is printed individually, in full, and never folded into the safe
count — even though the current measurement found zero of them. That
measurement can go stale the moment a title is fixed or a class_year lands
on a row that used to carry only a derived one, so the code path for both
unsafe classes is exercised on every run whether or not it finds anything,
not assumed empty because it was empty once.

SCOPE: every row, not just open campus rows
---------------------------------------------
The 2,662-row measurement was scoped to open rows in TARGET_BUCKETS
(insight/internship/entry_level) because that is what a live level filter
acts on today. This command is not scoped that way, on purpose:
`directory.views._my_applications_context` keeps a `status="closed"` row on
a student's own tracked list once the firm takes the live posting down
("POSTING CLOSED... rows are moved, never dropped") instead of removing it,
and `_eligibility`'s "Likely your year" verdict — the one place
`class_year_derived` actually renders to a student — reads that same row on
My Applications, not only the open feed. A closed row's stale derived year
is exactly as visible to that student as an open row's, so scoping this
repair to `status="open"` would leave My Applications history wrong on
purpose. Buckets outside TARGET_BUCKETS are not filtered out either:
`derive_class_year` already returns `("", "")` for every one of them
(insight is excluded explicitly; everything else falls through the same
unconditional return at the bottom of the function), so including them
costs nothing, and one unfiltered query is simpler to keep idempotent than
a filtered one that would need re-justifying every time the bucket
vocabulary changes.

Live network: none. Live database: read-only unless --commit.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand

from directory.classify import derive_class_year
from directory.models import Opportunity


def expected_class_year_derived(bucket: str, title: str, cohort: str,
                                 class_year: str) -> str:
    """The value ingest would write today for this row's current fields —
    exactly `directory.ingest._apply_opportunity`'s own rule, so a repaired
    row ends up reading exactly as it would if freshly ingested right now.
    A stated `class_year` always wins to blank, and `derive_class_year` is
    not even called in that case — matching ingest's
    `"" if class_year else derive_class_year(...)[0]`.

    Returns the bare year string, never the `(year, justification)` tuple
    `derive_class_year` actually returns — see the module docstring's note
    on the measurement bug this guards against.
    """
    if class_year:
        return ""
    return derive_class_year(bucket, title or "", cohort)[0]


class Command(BaseCommand):
    help = ("Recompute class_year_derived for every Opportunity and report "
            "(or, with --commit, write) rows where it has drifted from a "
            "live recompute. Report-only by default.")

    def add_arguments(self, parser):
        parser.add_argument(
            "--commit", "--apply", action="store_true", default=False,
            dest="commit",
            help="Write the repairs. Default is report-only.",
        )
        parser.add_argument(
            "--limit", type=int, default=0,
            help="Stop after N rows examined (0 = all). For spot-checking only.",
        )

    def handle(self, *args, **opts):
        commit = opts["commit"]
        tag = "" if commit else "[dry-run] "

        qs = (Opportunity.objects.select_related("firm")
              .order_by("firm__name", "id"))
        if opts["limit"]:
            qs = qs[: opts["limit"]]

        # Three separate buckets — see module docstring. Never merged: the
        # safe one is swept in as a block, the other two are always printed
        # row by row.
        blank_to_value: list[tuple[Opportunity, str]] = []
        value_to_blank: list[tuple[Opportunity, str]] = []
        value_to_changed: list[tuple[Opportunity, str, str]] = []
        examined = 0

        for o in qs.iterator(chunk_size=200):
            examined += 1
            current = o.class_year_derived or ""
            expected = expected_class_year_derived(
                o.bucket, o.title, o.cohort, o.class_year)
            if current == expected:
                continue
            if not current and expected:
                blank_to_value.append((o, expected))
            elif current and not expected:
                value_to_blank.append((o, current))
            else:
                value_to_changed.append((o, current, expected))

        total_changes = (len(blank_to_value) + len(value_to_blank)
                          + len(value_to_changed))
        if not total_changes:
            self.stdout.write(f"{tag}{examined} row(s) examined. Nothing stale.")
            return

        if blank_to_value:
            self.stdout.write(self.style.MIGRATE_HEADING(
                f"BLANK -> VALUE ({len(blank_to_value)} rows) — safe, swept "
                f"in as one block. A role becomes visible to a level filter "
                f"it should already match, never less visible:"
            ))
            for o, new in blank_to_value:
                self.stdout.write(
                    f"  #{o.id} {o.firm.name} — {o.title[:60]!r}: "
                    f"'' -> {new!r}")

        if value_to_blank:
            self.stdout.write("")
            self.stdout.write(self.style.WARNING(
                f"VALUE -> BLANK ({len(value_to_blank)} rows) — changes who "
                f"this role reads as likely-eligible for. Printed "
                f"individually, never swept in with the safe rows:"
            ))
            for o, old in value_to_blank:
                self.stdout.write(
                    f"  #{o.id} {o.firm.name} — {o.title[:60]!r}: "
                    f"{old!r} -> ''  [status={o.status!r} bucket={o.bucket!r} "
                    f"cohort={o.cohort!r} class_year={o.class_year!r}]")

        if value_to_changed:
            self.stdout.write("")
            self.stdout.write(self.style.WARNING(
                f"VALUE -> CHANGED ({len(value_to_changed)} rows) — the "
                f"inferred year itself moved. Printed individually, never "
                f"swept in with the safe rows:"
            ))
            for o, old, new in value_to_changed:
                self.stdout.write(
                    f"  #{o.id} {o.firm.name} — {o.title[:60]!r}: "
                    f"{old!r} -> {new!r}  [status={o.status!r} "
                    f"bucket={o.bucket!r} cohort={o.cohort!r} "
                    f"class_year={o.class_year!r}]")

        if commit:
            to_write: list[Opportunity] = []
            for o, new in blank_to_value:
                o.class_year_derived = new
                to_write.append(o)
            for o, _old in value_to_blank:
                o.class_year_derived = ""
                to_write.append(o)
            for o, _old, new in value_to_changed:
                o.class_year_derived = new
                to_write.append(o)
            # One batched write, not 2,662 individual .save() calls.
            Opportunity.objects.bulk_update(
                to_write, ["class_year_derived"], batch_size=500)

        self.stdout.write("")
        verb = "repaired" if commit else "would be repaired"
        self.stdout.write(self.style.SUCCESS(
            f"{tag}{examined} row(s) examined; {total_changes} {verb} "
            f"({len(blank_to_value)} blank->value, {len(value_to_blank)} "
            f"value->blank, {len(value_to_changed)} value->changed)."
        ))
        if not commit:
            self.stdout.write(
                "Nothing was written. Re-run with --commit to apply.")
