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
"""

from __future__ import annotations

from django.core.management.base import BaseCommand

from directory.ai_extract import AIExtractError, extract_deadline_ai, is_configured
from directory.classify import TARGET_BUCKETS
from directory.models import Opportunity


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
                                  "bypassing --limit's ordering.")

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
            qs = Opportunity.objects.filter(id__in=ids).select_related("firm")
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
            found += 1
            self.stdout.write(
                f"{tag}+ {opp.firm.name} — {opp.title[:44]}: "
                f"deadline {guess.value}  ·  \"{guess.phrase[:100]}\"")
            if dry:
                continue
            opp.deadline = guess.value
            opp.deadline_precision = "day"
            opp.confidence = guess.confidence
            opp.raw = {**(opp.raw or {}), "deadline_source": "ai"}
            opp.save(update_fields=["deadline", "deadline_precision", "confidence", "raw"])

        self.stdout.write(self.style.SUCCESS(
            f"{tag}{len(rows)} row(s) sent · {found} deadline(s) found · "
            f"{failed} API failure(s)"))
