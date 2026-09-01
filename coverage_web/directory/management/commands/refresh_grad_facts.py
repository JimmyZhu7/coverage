"""refresh_grad_facts — re-run the graduation-window extractor over the
descriptions already cached in raw.detail_text and repair rows whose stored
raw.facts.grad has drifted from what the extractor answers today
(report-only by default).

    python manage.py refresh_grad_facts            # report only (default)
    python manage.py refresh_grad_facts --commit   # write the refreshed facts

WHY THIS EXISTS
---------------
`raw["facts"]` is written by `extract_facts` (the management command), which
runs every extractor in directory/facts.py over every OPEN campus row's
cached description and rewrites the whole dict. A rule change in one
extractor therefore reaches a row only when that command next runs, and
never reaches a row that has since closed — while `directory.views.
_eligibility` keeps reading a closed row's grad fact on My Applications for
as long as the student tracks it.

The graduation-window extractor changed in three ways at once (see
`facts.extract_grad_years`): it learned that "graduating in 2028 or later"
has no ceiling (`open_high`), that a body's "Class of 2028" is the same
statement as a title's, and that two windows in one sentence are one
union. Every one of those changes what a student reads as ELIGIBLE FOR, and
a read-only measurement found 13 open rows blocking a 2029 student on a
sentence that includes them and 17 more with a "Class of" the verdict could
not see. This command is the scoped repair for that one fact: it re-derives
`grad` alone, leaves every other extractor's output exactly as stored, and
touches nothing on a row `extract_facts` has never run over (no `facts`
key) — a row like that has no grad fact to be wrong about, and inventing a
partial facts dict for it would make `extract_facts`' own "has this row
been read yet" question unanswerable.

FIVE CLASSES OF CHANGE, PRINTED SEPARATELY
------------------------------------------
NEW        no grad fact -> a fact. The body "Class of" rows. A verdict
           appears where there was silence; it can now block a student the
           sentence excludes, so every row is printed.
OPENED     a closed window -> an open-ended one (`open_high`). The rows
           that were wrongly blocking later classes. Only ever widens who
           reads as eligible.
BOUNDS     a closed window whose floor, ceiling or label moved (a second
           window unioned in, a "May/June 2029" the old pattern could not
           cross). Changes who reads as eligible in either direction, so
           every row is printed with both readings.
RETRACTED  a fact -> none. The pattern no longer matches this description.
           A verdict disappears; printed individually.
SHAPE      same floor, ceiling, label and openness — only the years list's
           order or de-duplication, or the evidence phrase, changed. No
           student's verdict moves. Counted, not printed.

Every class is written on --commit; the split exists so the report can be
read before the write, not so some rows can be skipped.

Live network: none. Live database: read-only unless --commit.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand

from directory.facts import _clean, extract_grad_years
from directory.models import Opportunity


def refreshed_grad_fact(raw: dict | None) -> dict | None:
    """What `extract_facts` would store as `facts["grad"]` for this row's
    cached description today — the same `_clean` pass it applies before
    every extractor, then `extract_grad_years` alone. None when the row has
    no description, or the description states no window."""
    text = (raw or {}).get("detail_text") or ""
    if not text.strip():
        return None
    return extract_grad_years(_clean(text))


def _bounds(fact: dict | None) -> tuple | None:
    """(floor, ceiling, open_high, label) — the four things a reader acts on.
    Two facts with equal bounds give every student the same verdict."""
    if not fact:
        return None
    years = [int(y) for y in fact.get("years") or () if str(y).isdigit()]
    if not years:
        return None
    return (min(years), max(years), bool(fact.get("open_high")), fact.get("value"))


def classify_change(old: dict | None, new: dict | None) -> str | None:
    """Which of the module docstring's five classes this row falls in, or
    None when nothing changed."""
    if old == new:
        return None
    ob, nb = _bounds(old), _bounds(new)
    if ob is None and nb is not None:
        return "NEW"
    if ob is not None and nb is None:
        return "RETRACTED"
    if ob == nb:
        return "SHAPE"
    if nb[2] and not ob[2]:
        return "OPENED"
    return "BOUNDS"


def _describe(fact: dict | None) -> str:
    if not fact:
        return "none"
    b = _bounds(fact)
    if b is None:
        return f"{fact.get('value')!r} (no readable years)"
    lo, hi, open_high, label = b
    span = f"{lo}+" if open_high else (str(lo) if lo == hi else f"{lo}–{hi}")
    return f"{label!r} [{span}]"


class Command(BaseCommand):
    help = ("Re-run the graduation-window extractor over cached descriptions "
            "and report (or, with --commit, write) rows whose raw.facts.grad "
            "has drifted from a live re-extraction. Report-only by default.")

    def add_arguments(self, parser):
        parser.add_argument(
            "--commit", "--apply", action="store_true", default=False,
            dest="commit",
            help="Write the refreshed facts. Default is report-only.",
        )
        parser.add_argument(
            "--limit", type=int, default=0,
            help="Stop after N rows examined (0 = all). For spot-checking only.",
        )

    def handle(self, *args, **opts):
        commit = opts["commit"]
        tag = "" if commit else "[dry-run] "

        # Every row extract_facts has ever read (it leaves a `facts` key even
        # when it found nothing), open or closed — see the module docstring
        # for why closed rows are in and never-read rows are out.
        qs = (Opportunity.objects.filter(raw__has_key="facts")
              .select_related("firm").order_by("firm__name", "id"))
        if opts["limit"]:
            qs = qs[: opts["limit"]]

        changes: dict[str, list] = {
            "NEW": [], "OPENED": [], "BOUNDS": [], "RETRACTED": [], "SHAPE": []}
        examined = 0
        to_write: list[Opportunity] = []

        for o in qs.iterator(chunk_size=200):
            examined += 1
            raw = o.raw or {}
            old = (raw.get("facts") or {}).get("grad")
            new = refreshed_grad_fact(raw)
            kind = classify_change(old, new)
            if kind is None:
                continue
            changes[kind].append((o, old, new))
            if commit:
                facts = dict(raw.get("facts") or {})
                if new:
                    facts["grad"] = new
                else:
                    facts.pop("grad", None)
                o.raw = {**raw, "facts": facts}
                to_write.append(o)

        total = sum(len(v) for v in changes.values())
        if not total:
            self.stdout.write(f"{tag}{examined} row(s) examined. Nothing stale.")
            return

        headings = {
            "NEW": ("a verdict appears where there was silence", self.style.WARNING),
            "OPENED": ("a closed window was really open-ended; only widens "
                       "who reads as eligible", self.style.MIGRATE_HEADING),
            "BOUNDS": ("the window's floor, ceiling or label moved; can "
                       "change a verdict either way", self.style.WARNING),
            "RETRACTED": ("the pattern no longer matches; a verdict "
                          "disappears", self.style.WARNING),
        }
        for kind, (why, style) in headings.items():
            rows = changes[kind]
            if not rows:
                continue
            self.stdout.write(style(f"{kind} ({len(rows)} rows) — {why}:"))
            for o, old, new in rows:
                self.stdout.write(
                    f"  #{o.id} {o.firm.name} — {o.title[:60]!r} "
                    f"[status={o.status!r}]: {_describe(old)} -> {_describe(new)}")
                phrase = (new or old or {}).get("phrase", "")
                if phrase:
                    self.stdout.write(f"      \"{phrase[:150]}\"")
            self.stdout.write("")
        if changes["SHAPE"]:
            self.stdout.write(
                f"SHAPE ({len(changes['SHAPE'])} rows) — same bounds, label and "
                f"openness; only the years list's order/de-duplication or the "
                f"evidence phrase changed. No verdict moves.")
            self.stdout.write("")

        if commit:
            # One batched write, not one .save() per row.
            Opportunity.objects.bulk_update(to_write, ["raw"], batch_size=500)

        verb = "refreshed" if commit else "would be refreshed"
        self.stdout.write(self.style.SUCCESS(
            f"{tag}{examined} row(s) examined; {total} {verb} "
            f"({len(changes['NEW'])} new, {len(changes['OPENED'])} opened, "
            f"{len(changes['BOUNDS'])} bounds, {len(changes['RETRACTED'])} "
            f"retracted, {len(changes['SHAPE'])} shape-only)."
        ))
        if not commit:
            self.stdout.write(
                "Nothing was written. Re-run with --commit to apply.")
