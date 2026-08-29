"""LLM second pass for deadline extraction, over rows the regex extractor
in `classify.extract_deadline_from_text` found nothing on.

See `directory/ai_extract.py`'s module docstring for the full rationale and
the grounding rule that keeps this safe to ship. Short version: this only
fills silence, never overwrites a regex- or provider-stated deadline, costs
real money per call (an Anthropic API key must be set — see .env.example),
and is deliberately its own command so a human runs it with --limit and
watches the bill before ever pointing it at the full board.

    python manage.py extract_deadlines_ai --dry-run --limit 20   # see cost/impact first
    python manage.py extract_deadlines_ai --limit 200            # write for up to 200 rows
    python manage.py extract_deadlines_ai                        # every eligible row

`--ids` NARROWS THE ELIGIBLE SET; IT DOES NOT UNLOCK IT. The flag used to
replace the whole queryset with a bare `filter(id__in=ids)`, which dropped
every one of the three guards that make this command safe to run at all:
`deadline__isnull=True` (the never-overwrite rule this module and
`directory.ai_extract` both open by stating), `status="open"`, and the
campus-bucket scope. A single `--ids` typo was therefore enough to have an
LLM's 0.5-confidence reading replace a deadline the board itself published at
1.0 — with no record that it had, since this command writes no
`OpportunityChange`. The named ids are now intersected with the same eligible
queryset instead: `--ids` still bypasses `--limit`'s ordering, which is all
it was ever documented to do.
"""

from __future__ import annotations

from datetime import date

from django.core.management.base import BaseCommand

from directory.ai_extract import AIExtractError, extract_deadline_ai, is_configured
from directory.classify import TARGET_BUCKETS
from directory.models import Opportunity


def _parsed(value) -> date | None:
    """`DeadlineGuess.value` as a real calendar date, or None.

    `ai_extract` gates the model's answer with `re.fullmatch(r"20\\d{2}-\\d{2}-
    \\d{2}", ...)`, which is a SHAPE check, not a date check: "2026-02-30" and
    "2026-13-01" both pass it. Assigning either straight to `Opportunity.
    deadline` raises on save, and there is no try/except around that save — so
    one malformed answer took down the rest of a paid batch run partway
    through, after some rows had already been written. Parsing here turns that
    into one skipped row.
    """
    try:
        return date.fromisoformat(str(value or "")[:10])
    except (TypeError, ValueError):
        return None


class Command(BaseCommand):
    help = "Fill missing deadlines via an LLM second pass over cached posting text."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true",
                             help="Call the API and report what would be written, write nothing.")
        parser.add_argument("--limit", type=int, default=50,
                             help="Max rows to send to the API this run (0 = no limit). "
                                  "Default is deliberately small: every row is a paid call.")
        parser.add_argument("--ids", type=str, default="",
                             help="Comma-separated Opportunity ids to restrict the run to, "
                                  "bypassing --limit's ordering. Still subject to every "
                                  "eligibility guard: an id that already has a deadline, "
                                  "is closed, or is out of the campus buckets is skipped.")

    def handle(self, *args, **opts):
        if not is_configured():
            self.stdout.write(self.style.WARNING(
                "ANTHROPIC_API_KEY is not set — nothing to do. "
                "See .env.example's \"AI features\" section."))
            return

        dry, limit = opts["dry_run"], opts["limit"]
        tag = "[dry-run] " if dry else ""

        qs = (Opportunity.objects.filter(status="open", bucket__in=TARGET_BUCKETS, deadline__isnull=True)
              .exclude(raw__detail_text="")
              .select_related("firm").order_by("id"))
        if opts["ids"]:
            ids = [int(x) for x in opts["ids"].split(",") if x.strip()]
            qs = qs.filter(id__in=ids)
            named = set(ids)
            reachable = set(qs.values_list("id", flat=True))
            for skipped in sorted(named - reachable):
                self.stdout.write(
                    f"  #{skipped} — not eligible (already has a deadline, not open, "
                    f"or outside the campus buckets); skipped")
        elif limit:
            qs = qs[:limit]

        rows = [o for o in qs if (o.raw or {}).get("detail_text")]
        if not rows:
            self.stdout.write("No eligible rows (open, campus-bucket, no deadline, has cached text).")
            return

        found = failed = 0
        for opp in rows:
            text = (opp.raw or {}).get("detail_text") or ""
            try:
                guess = extract_deadline_ai(text)
            except AIExtractError as e:
                failed += 1
                self.stderr.write(f"  ! {opp.firm.name} — {opp.title[:44]}: {e}")
                continue
            if guess is None:
                continue
            parsed = _parsed(guess.value)
            if parsed is None:
                failed += 1
                self.stderr.write(
                    f"  ! {opp.firm.name} — {opp.title[:44]}: model returned "
                    f"{guess.value!r}, which is not a real date; skipped")
                continue
            found += 1
            self.stdout.write(
                f"{tag}+ {opp.firm.name} — {opp.title[:44]}: "
                f"deadline {parsed.isoformat()}  ·  \"{guess.phrase[:100]}\"")
            if dry:
                continue
            opp.deadline = parsed
            opp.deadline_precision = "day"
            opp.confidence = guess.confidence
            opp.raw = {**(opp.raw or {}), "deadline_source": "ai"}
            opp.save(update_fields=["deadline", "deadline_precision", "confidence", "raw"])

        self.stdout.write(self.style.SUCCESS(
            f"{tag}{len(rows)} row(s) sent · {found} deadline(s) found · "
            f"{failed} API failure(s)"))
