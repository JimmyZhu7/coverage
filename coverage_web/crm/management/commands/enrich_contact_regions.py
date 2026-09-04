"""Place blank-region contacts from their own public record, via the web.

The sibling of `backfill_contact_regions`, and the same contract: dry run
FIRST by design, `--apply` writes, every write lands in an undo file so the
whole run reverses with `--revert`, and it only ever fills blanks — a region
a person set is never touched. What differs is the evidence: where the
backfill reads what the row already holds, this asks `crm.region_enrich` to
search the public web for the person and answers only from a page that names
them at their firm. See that module for why this does not breach
`Contact.resolve_region`'s ban on probabilistic placement.

It writes with `.update()`, never `save()`, for the reason `_revert` in the
backfill gives: `save()` runs `resolve_region`, and a firm with one deadline
market would re-place a row this command had just cleared. `region_source`
is stamped "web" so what a model placed is a query, not a guess, and can be
reverted as a set.

Usage:
    manage.py enrich_contact_regions --user founder@example.com               # dry run, all blanks
    manage.py enrich_contact_regions --user founder@example.com --limit 5     # try a handful first
    manage.py enrich_contact_regions --user founder@example.com --ids 482,806
    manage.py enrich_contact_regions --user founder@example.com --apply
    manage.py enrich_contact_regions --user founder@example.com \\
        --revert region_enrich_undo_20260904T020000.json

Each contact is one API call with web search — measured at ~15 cents at
Opus 5, ~$14 for 94. The dry run spends it too: that is the point, to see
what the search finds before believing it. So it does not get spent twice,
a dry run writes everything it found to `--plan-out` (default
region_enrich_plan_<timestamp>.json), and `--apply-plan FILE` writes those
placements WITHOUT searching again. Review the plan, then apply it.

    manage.py enrich_contact_regions --user X                         # searches, saves a plan
    manage.py enrich_contact_regions --user X --apply-plan region_enrich_plan_....json
"""
from __future__ import annotations

import json
import time
from collections import Counter
from datetime import datetime, timezone as dt_timezone
from pathlib import Path

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from core.templatetags.textstyle import smart_person_name
from crm.models import Contact
from crm.region_enrich import Placement, enrich


class Command(BaseCommand):
    help = (
        "Fill blank contact regions from the person's own public record "
        "(profile, firm bio, FINRA), one web-search call per contact. Dry "
        "run by default; --apply to write; --revert <undo-file> to reverse."
    )

    def add_arguments(self, parser):
        parser.add_argument("--user", required=True,
                            help="Email of the account whose contacts to place.")
        parser.add_argument("--apply", action="store_true",
                            help="Write the placements. Without this, dry run only.")
        parser.add_argument("--allow-other", action="store_true",
                            help="Also write 'other' (outside both markets). Off by "
                                 "default: 'other' is normally a human's call.")
        parser.add_argument("--ids", metavar="ID,ID,...",
                            help="Only these contact ids (must be blank-region).")
        parser.add_argument("--limit", type=int, default=0,
                            help="Stop after this many contacts (0 = all).")
        parser.add_argument("--sleep", type=float, default=0.5,
                            help="Seconds between calls (rate courtesy).")
        parser.add_argument("--plan-out", metavar="PATH",
                            help="Where a dry run saves what it found (default: "
                                 "region_enrich_plan_<timestamp>.json in cwd).")
        parser.add_argument("--apply-plan", metavar="PLAN_FILE",
                            help="Write the placements a previous dry run saved, "
                                 "without searching again.")
        parser.add_argument("--revert", metavar="UNDO_FILE",
                            help="Reverse a previous --apply using its undo file.")
        parser.add_argument("--undo-file", metavar="PATH",
                            help="Where --apply writes its undo record "
                                 "(default: region_enrich_undo_<timestamp>.json in cwd).")

    def handle(self, *args, **options):
        User = get_user_model()
        try:
            user = User.objects.get(email=options["user"])
        except User.DoesNotExist:
            raise CommandError(f"no user with email {options['user']!r}")
        if options["revert"]:
            self._revert(user, options["revert"])
            return
        if options["apply_plan"]:
            self._apply_plan(user, options["apply_plan"], options["undo_file"],
                             allow_other=options["allow_other"])
            return

        qs = (
            Contact.objects.for_user(user)
            .filter(archived=False, region="")
            .select_related("firm")
            .order_by("id")
        )
        if options["ids"]:
            try:
                ids = [int(x) for x in options["ids"].split(",") if x.strip()]
            except ValueError:
                raise CommandError("--ids must be comma-separated integers")
            qs = qs.filter(id__in=ids)
        contacts = list(qs)
        if options["limit"]:
            contacts = contacts[: options["limit"]]

        self.stdout.write(
            f"{len(contacts)} blank-region contacts for {user.email}; "
            f"one web-search call each."
        )

        placed = []      # (contact, placement)
        stated_other = []
        unknown = []
        failed = []
        tokens_in = tokens_out = searches = 0

        for i, c in enumerate(contacts):
            firm = c.firm.name if c.firm_id else (c.firm_text or "")
            shown = smart_person_name(c.name)
            p = enrich(shown, c.email, firm, role=c.role)
            if p is None:
                failed.append(c)
                self.stdout.write(f"  {c.id:>6}  {shown[:28]:<28} -> (no answer)")
            else:
                tokens_in += p.input_tokens
                tokens_out += p.output_tokens
                searches += p.searches
                tag = f"{p.market:<7} {p.confidence:<6}"
                src = p.source_url[:70] if p.source_url else "-"
                city = f" {p.city}" if p.city else ""
                # The evidence sentence prints under every placement, on the
                # dry run above all: a placement the human cannot audit is a
                # guess with a URL on it. The first live run put a LinkedIn
                # slug reading "carolinemoriarity" under "Caroline Baenen" at
                # a Chicago-headquartered firm, and nothing on the line said
                # whether the page named her at the firm or just the firm's
                # city — the exact trap the module docstring is about.
                if p.writable or (options["allow_other"] and p.stated):
                    placed.append((c, p))
                    self.stdout.write(f"  {c.id:>6}  {shown[:28]:<28} -> {tag}{city} | {src}")
                    self.stdout.write(f"          evidence: {p.evidence[:160]}")
                elif p.stated and p.market == "other":
                    stated_other.append((c, p))
                    self.stdout.write(f"  {c.id:>6}  {shown[:28]:<28} -> {tag}{city} | {src}  (other: not written without --allow-other)")
                    self.stdout.write(f"          evidence: {p.evidence[:160]}")
                else:
                    unknown.append((c, p))
                    self.stdout.write(f"  {c.id:>6}  {shown[:28]:<28} -> unknown  ({p.market}/{p.confidence}, matched={p.person_matched})")
            if i + 1 < len(contacts) and options["sleep"]:
                time.sleep(options["sleep"])

        by_market = Counter(p.market for _, p in placed)
        self.stdout.write("")
        self.stdout.write(
            f"{len(placed)} placeable ({dict(by_market)}), "
            f"{len(stated_other)} stated 'other', "
            f"{len(unknown)} unknown, {len(failed)} no answer. "
            f"{searches} searches, {tokens_in} in / {tokens_out} out tokens."
        )

        if not options["apply"]:
            plan_path = Path(
                options["plan_out"]
                or f"region_enrich_plan_{datetime.now(dt_timezone.utc):%Y%m%dT%H%M%S}.json"
            )
            plan = {"user": user.email, "placements": {}}
            for c, p in placed + stated_other:
                plan["placements"][str(c.id)] = {
                    "name": smart_person_name(c.name), "market": p.market,
                    "city": p.city, "confidence": p.confidence,
                    "source_url": p.source_url, "evidence": p.evidence,
                    "person_matched": p.person_matched,
                }
            plan_path.write_text(json.dumps(plan, indent=2))
            self.stdout.write(self.style.WARNING(
                f"Dry run — nothing written. Plan saved to {plan_path}; "
                f"apply it with --apply-plan {plan_path} (no second search)."
            ))
            return
        if not placed:
            self.stdout.write("Nothing to write.")
            return

        undo_path = Path(
            options["undo_file"]
            or f"region_enrich_undo_{datetime.now(dt_timezone.utc):%Y%m%dT%H%M%S}.json"
        )
        undo = {"user": user.email, "written": {}, "evidence": {}}
        for c, p in placed:
            # `.update()`, not `save()` — see the module docstring.
            Contact.objects.for_user(user).filter(id=c.id, region="").update(
                region=p.market, region_source=Contact.REGION_SOURCE_WEB
            )
            undo["written"][str(c.id)] = p.market
            undo["evidence"][str(c.id)] = {
                "city": p.city, "confidence": p.confidence,
                "source_url": p.source_url, "evidence": p.evidence,
            }
        undo_path.write_text(json.dumps(undo, indent=2))
        self.stdout.write(self.style.SUCCESS(
            f"Wrote {len(placed)} regions. Undo file: {undo_path}"
        ))

    def _apply_plan(self, user, plan_file: str, undo_file: str | None, *, allow_other: bool):
        """Write what a dry run found, without searching again. Every row is
        re-checked against the same rules as a live placement (`Placement`),
        and against the row's CURRENT state — a contact placed by hand since
        the dry run is left alone, and a plan for a different account is
        refused outright."""
        try:
            plan = json.loads(Path(plan_file).read_text())
        except OSError as exc:
            raise CommandError(f"cannot read plan file: {exc}")
        if plan.get("user") != user.email:
            raise CommandError(
                f"plan file is for {plan.get('user')!r}, not {user.email!r}"
            )
        wanted = plan.get("placements", {})
        rows = {
            c.id: c for c in Contact.objects.for_user(user)
            .filter(id__in=[int(k) for k in wanted], archived=False)
        }
        undo_path = Path(
            undo_file
            or f"region_enrich_undo_{datetime.now(dt_timezone.utc):%Y%m%dT%H%M%S}.json"
        )
        undo = {"user": user.email, "written": {}, "evidence": {}}
        skipped_placed = skipped_rules = 0
        for key, rec in wanted.items():
            c = rows.get(int(key))
            if c is None or c.region:
                skipped_placed += 1
                continue
            p = Placement(
                market=rec.get("market", "unknown"), city=rec.get("city", ""),
                confidence=rec.get("confidence", "low"),
                source_url=rec.get("source_url", ""), evidence=rec.get("evidence", ""),
                person_matched=bool(rec.get("person_matched", False)),
            )
            if not (p.writable or (allow_other and p.stated)):
                skipped_rules += 1
                continue
            Contact.objects.for_user(user).filter(id=c.id, region="").update(
                region=p.market, region_source=Contact.REGION_SOURCE_WEB
            )
            undo["written"][str(c.id)] = p.market
            undo["evidence"][str(c.id)] = {
                "city": p.city, "confidence": p.confidence,
                "source_url": p.source_url, "evidence": p.evidence,
            }
        if undo["written"]:
            undo_path.write_text(json.dumps(undo, indent=2))
        self.stdout.write(self.style.SUCCESS(
            f"Wrote {len(undo['written'])} regions from the plan; "
            f"skipped {skipped_placed} already placed or gone, "
            f"{skipped_rules} not placeable"
            + (f". Undo file: {undo_path}" if undo["written"] else ".")
        ))

    def _revert(self, user, undo_file: str):
        try:
            undo = json.loads(Path(undo_file).read_text())
        except OSError as exc:
            raise CommandError(f"cannot read undo file: {exc}")
        if undo.get("user") != user.email:
            raise CommandError(
                f"undo file is for {undo.get('user')!r}, not {user.email!r}"
            )
        written = undo.get("written", {})
        rows = Contact.objects.for_user(user).filter(
            id__in=[int(k) for k in written]
        )
        to_revert, skipped = [], 0
        for c in rows:
            # Only while it still holds exactly what this run wrote, and
            # still says the web wrote it: a region the user corrected since
            # is their word now.
            if (c.region == written[str(c.id)]
                    and c.region_source == Contact.REGION_SOURCE_WEB):
                to_revert.append(c.id)
            else:
                skipped += 1
        reverted = 0
        if to_revert:
            reverted = Contact.objects.for_user(user).filter(
                id__in=to_revert
            ).update(region="", region_source="")
        self.stdout.write(self.style.SUCCESS(
            f"Reverted {reverted} contacts to blank; "
            f"left {skipped} that were edited since."
        ))
