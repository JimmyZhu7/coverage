"""repair_blanked_regions — put back the regions a silent re-scrape erased.

    python manage.py repair_blanked_regions            # DRY RUN, the default
    python manage.py repair_blanked_regions --apply

DRY RUN IS THE DEFAULT AND THAT IS DELIBERATE, for the same reason it is in
`relabel_firm_dates`: `Opportunity` is SHARED directory data. Every student
reads the same rows, so a bad write here is not one person's mistake.

WHAT BROKE
----------
`ingest._apply_opportunity` assigned `existing.location` and `existing.region`
unconditionally on every re-scrape of a known posting, and derived both from
the connector's reading of the provider's location field. Workday states a
posting's place in `locationsText`, and a tenant is free not to send the key:
Raymond James's early-careers site sends `title`, `externalPath`, `postedOn`
and `bulletFields`, and nothing else. So the derivation produced "" and the ""
was written over a correct value — 225 of 225 open Raymond James rows lost the
`us` they had held minutes earlier, and 252 TD and 37 Barclays rows carry the
same blank from earlier passes over the same shape. `OpportunityChange` did
not record either field, so the wipe left no trail: the rows just stopped
answering the feed's Region filter and no report anywhere said why.

Ingest no longer does that (silence keeps the stored value, and both fields
are now written to the change log). This command is for the rows already hurt.

WHAT IT WILL AND WILL NOT DO
----------------------------
It recovers a region only from EVIDENCE, and it names the evidence for every
row it proposes to touch. The ladder, strongest first:

  change_log       an earlier `OpportunityChange` on this row states the
                   region it held, or the location text it held. Our own
                   record of the row's past, which is why it goes first.
  own_location     the row's own stored `location` still names a place, and
                   `classify.normalize_region` reads a market from it. Only
                   the region was lost; the text that derives it survived.
  payload          the provider's own payload, still stored verbatim in
                   `raw`: the location structures `classify.region_from_fields`
                   reads, or the city Workday puts at the head of its
                   `externalPath` ("/job/Saint-Petersburg-Florida---United-
                   States/..."). This is what rescues the Raymond James rows,
                   whose `location` text was blanked by the same bug.
  sibling_location another OPEN row of the SAME FIRM carries the identical
                   location text and a region — and every such sibling agrees
                   on one. The firm has already told us where that office is.
  firm_market      the firm recruits in exactly one market (`Firm.regions`
                   holds one code) and not one of its open rows contradicts
                   it. The weakest rung, and reported separately.

A row whose region cannot be recovered from any of those is LEFT BLANK and
counted on its own line. Most of them deserve to be: "Regina, Saskatchewan"
and "2 Locations" are what those postings actually say, and blank is the
honest reading of a place outside the four tracked markets. Filling those in
would be inventing a market for a role, which is the same class of mistake as
the wipe this command exists to undo — see P4, mark, never drop, and never
fabricate. Nothing here guesses.

Every applied repair writes an `OpportunityChange` naming the evidence, so the
repair is as auditable as the damage was not.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from urllib.parse import unquote

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from directory.classify import normalize_region, region_from_fields
from directory.models import Firm, Opportunity, OpportunityChange

#: "/job/Copenhagen/Key-Account-Manager..." -> "Copenhagen". The same read
#: `reclassify` makes of the field, and for the same reason: it is the
#: provider's own routing, not our inference. "/job/2-Locations/..." exists
#: too, which is why the captured segment still goes through
#: `normalize_region` rather than being trusted as a place.
_EXTERNAL_PATH_CITY = re.compile(r"^/job/([^/]+)/")

#: Ladder order, strongest evidence first. Also the report's row order.
_SOURCES = ("change_log", "own_location", "payload", "sibling_location",
            "firm_market")


def _from_change_log(opp: Opportunity) -> str:
    """The region this row is recorded as having held, or the region derived
    from a location text it is recorded as having held.

    Rows are read newest-first and the first stated value wins: the last
    thing the log saw is the last thing that was true. A blank on either side
    of a change is the wipe itself and states nothing.
    """
    rows = (OpportunityChange.objects
            .filter(opportunity_id=opp.pk, field__in=("region", "location"))
            .order_by("-observed_at", "-id"))
    for row in rows:
        for value in (row.new_value, row.old_value):
            value = (value or "").strip()
            if not value:
                continue
            code = value if row.field == "region" else normalize_region(value)
            if code:
                return code
    return ""


def _from_payload(opp: Opportunity) -> str:
    """The market the provider's own stored payload states."""
    code = region_from_fields(opp.raw)
    if code:
        return code
    m = _EXTERNAL_PATH_CITY.match((opp.raw or {}).get("externalPath") or "")
    if not m:
        return ""
    # A Workday slug's hyphen joins the words of one name ("United-States")
    # AND separates a city from its state ("OLATHE-KS", where the
    # state-suffix rule needs a comma). Both readings are tried, space first
    # — exactly as `reclassify` does it, so the two commands can never
    # disagree about what a path says.
    seg = unquote(m.group(1))
    return (normalize_region(seg.replace("-", " "))
            or normalize_region(seg.replace("-", ", ")))


def _from_siblings(opp: Opportunity) -> str:
    """The one region every other OPEN row of this firm at this exact location
    text carries. Disagreement means the text is ambiguous (Workday's "2
    Locations" is the same string in three markets) and answers nothing."""
    text = (opp.location or "").strip()
    if not text:
        return ""
    found = set(Opportunity.objects
                .filter(firm_id=opp.firm_id, status="open", location=opp.location)
                .exclude(pk=opp.pk).exclude(region="")
                .values_list("region", flat=True))
    return found.pop() if len(found) == 1 else ""


def _from_firm_market(opp: Opportunity, firm_regions: dict[int, list[str]],
                      firm_row_regions: dict[int, set[str]]) -> str:
    """The firm's single market, when it has exactly one and nothing it has
    posted disagrees. A firm that recruits in one place puts its postings
    there; a firm with two is telling us nothing about this row."""
    regions = [r for r in (firm_regions.get(opp.firm_id) or []) if r]
    if len(regions) != 1:
        return ""
    stated = firm_row_regions.get(opp.firm_id) or set()
    if stated - {regions[0]}:
        return ""
    return regions[0]


class Command(BaseCommand):
    help = ("Restore the region on open rows a silent re-scrape blanked, "
            "from recorded evidence only. Dry run by default.")

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply", action="store_true", default=False,
            help="Actually write. Without it this command only reports.")
        parser.add_argument(
            "--firm", default="",
            help="Restrict to one firm slug (e.g. raymondjames).")
        parser.add_argument(
            "--limit", type=int, default=0,
            help="Stop after N blank rows (0 = all).")
        parser.add_argument(
            "--samples", type=int, default=3,
            help="Example rows to print per firm and evidence source.")

    def handle(self, *args, **opts):
        apply = opts["apply"]
        tag = "" if apply else "[dry-run] "

        qs = (Opportunity.objects.select_related("firm")
              .filter(status="open", region="").order_by("firm__slug", "id"))
        if opts["firm"]:
            qs = qs.filter(firm__slug=opts["firm"])
        if opts["limit"]:
            qs = qs[:opts["limit"]]

        firm_regions = {f.pk: list(f.regions or []) for f in Firm.objects.all()}
        # Every region this firm's OPEN rows actually carry — the contradiction
        # test for `firm_market`, gathered once rather than per row.
        firm_row_regions: dict[int, set[str]] = defaultdict(set)
        for firm_id, region in (Opportunity.objects.filter(status="open")
                                .exclude(region="")
                                .values_list("firm_id", "region").distinct()):
            firm_row_regions[firm_id].add(region)

        by_source: Counter = Counter()
        by_firm: dict[str, Counter] = defaultdict(Counter)
        samples: dict[tuple[str, str], list[str]] = defaultdict(list)
        repairs: list[tuple[Opportunity, str, str]] = []

        for opp in qs.iterator():
            region, source = self._recover(opp, firm_regions, firm_row_regions)
            slug = opp.firm.slug
            key = source if region else "unrecoverable"
            by_source[key] += 1
            by_firm[slug][key] += 1
            if len(samples[(slug, key)]) < opts["samples"]:
                samples[(slug, key)].append(
                    f"      #{opp.pk} {region or '-':<6} "
                    f"{(opp.location or '(no location)')[:34]:<34} "
                    f"{opp.url[:70]}")
            if region:
                repairs.append((opp, region, source))

        self._report(by_source, by_firm, samples)

        if apply and repairs:
            now = timezone.now()
            with transaction.atomic():
                changes = []
                for opp, region, source in repairs:
                    opp.region = region
                    opp.save(update_fields=["region"])
                    changes.append(OpportunityChange.entry(
                        opp.pk, "region", "", region,
                        stage=OpportunityChange.STAGE_REPAIR, at=now,
                        note=f"restored from {source} after a re-scrape whose "
                             f"payload stated no location blanked it",
                    ))
                OpportunityChange.objects.bulk_create(changes, batch_size=1000)

        verb = "repaired" if apply else "would repair"
        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS(
            f"{tag}{verb} {len(repairs)} row(s) from evidence; "
            f"{by_source['unrecoverable']} left blank for want of any."))
        if not apply and repairs:
            self.stdout.write(
                "Nothing was written. Re-run with --apply once the table "
                "above has been read.")

    # ------------------------------------------------------------------
    def _recover(self, opp, firm_regions, firm_row_regions) -> tuple[str, str]:
        """The region this row can be shown to have, and the rung that showed
        it. `("", "")` when nothing does — which is an answer, not a failure."""
        for source, finder in (
            ("change_log", lambda o: _from_change_log(o)),
            ("own_location", lambda o: normalize_region(o.location)),
            ("payload", lambda o: _from_payload(o)),
            ("sibling_location", lambda o: _from_siblings(o)),
            ("firm_market", lambda o: _from_firm_market(
                o, firm_regions, firm_row_regions)),
        ):
            code = (finder(opp) or "").strip()
            # A region code, never a sentence: the change log stores free text
            # and a malformed row must not become a column value.
            if code and len(code) <= 16 and " " not in code:
                return code, source
        return "", ""

    def _report(self, by_source, by_firm, samples) -> None:
        self.stdout.write("")
        self.stdout.write(self.style.MIGRATE_HEADING(
            "Open rows with a blank region, by the evidence that can restore it"))
        self.stdout.write(f"  {'firm':<16} " +
                          " ".join(f"{s[:14]:>15}" for s in _SOURCES) +
                          f"{'unrecoverable':>15}")
        for slug in sorted(by_firm, key=lambda s: -sum(by_firm[s].values())):
            counts = by_firm[slug]
            self.stdout.write(
                f"  {slug[:16]:<16} " +
                " ".join(f"{counts[s] or '':>15}" for s in _SOURCES) +
                f"{counts['unrecoverable'] or '':>15}")
        self.stdout.write("  " + "-" * 90)
        self.stdout.write(
            f"  {'TOTAL':<16} " +
            " ".join(f"{by_source[s] or '':>15}" for s in _SOURCES) +
            f"{by_source['unrecoverable'] or '':>15}")

        self.stdout.write("")
        self.stdout.write(self.style.MIGRATE_HEADING("Examples"))
        for (slug, key) in sorted(samples):
            self.stdout.write(f"    {slug} / {key}")
            for line in samples[(slug, key)]:
                self.stdout.write(line)
