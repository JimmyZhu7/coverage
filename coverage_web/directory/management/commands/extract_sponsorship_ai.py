"""LLM third pass for sponsorship extraction, over the residue steps 1-3 of
docs/founder-decisions-2026-08-20.md, Decision 3, could not answer for free:
`classify.extract_sponsorship`'s regex (the Workday structured field plus
the missed phrasings) found nothing, AND `directory.sponsorship`'s firm
per-region policy fallback found nothing either.

See `directory/ai_extract.py`'s module docstring, and its "SPONSORSHIP
EXTRACTION" section specifically, for the full rationale and the grounding
rule that keeps this safe to ship. Short version: this only fills silence,
never overwrites a posting- or firm-stated answer, costs real money per call
(an Anthropic API key must be set — see .env.example), and is deliberately
its own command so a human runs it with --limit and watches the estimated
cost before ever pointing it at the full board.

FOUNDER-RUN ONLY. This command must never be wired into a cron, a deploy
step, or any other automatic sweep — see `extract_deadlines_ai`'s identical
posture, and docs/credit-system-plan.md §1's "founder-run, not metered"
list, which this command joins.

    python manage.py extract_sponsorship_ai --limit 20            # see cost/impact first, writes nothing
    python manage.py extract_sponsorship_ai --limit 200 --commit  # write for up to 200 rows
    python manage.py extract_sponsorship_ai --commit              # every eligible row

Dry-run (report only, no write) is the DEFAULT here — the opposite of
`extract_deadlines_ai`'s default — because Decision 3 explicitly asks for a
cost estimate to be seen before any spend commits: `--commit` is required to
actually write, every run (dry or not) still calls the API for the rows it
sends (that's the only way to know the real answer or the real cost), and
the row count and estimated dollar cost print before a single call is made
so a `--limit` mistake is visible before it's paid for.
"""

from __future__ import annotations

import re

from django.core.management.base import BaseCommand

from directory.ai_extract import AIExtractError, extract_sponsorship_ai, is_configured
from directory.classify import TARGET_BUCKETS
from directory.models import Opportunity
from directory.sponsorship import effective_sponsorship

# The same keyword gate Decision 3 measured against the live board: 946 of
# 2,304 not-stated open campus rows mention one of these; the other 1,358
# genuinely say nothing about sponsorship at all, and sending those to the
# model would only buy a guaranteed "no answer" at full price. Word-boundary
# matched so "opt"/"cpt" don't fire inside unrelated words ("option",
# "adopt", "accept").
_KEYWORD = re.compile(
    r"\b(sponsor\w*|visa|work\s+authori[sz]ation|right\s+to\s+work|"
    r"h-?1b|opt|cpt)\b",
    re.IGNORECASE,
)

# Decision 3's own estimate: ~1,500 input tokens and ~80 output tokens per
# row, at roughly $1.00 / 1M input and $5.00 / 1M output tokens (Haiku
# pricing) — 310 rows costs about $0.47 + $0.12 ≈ $0.59, the "$1 one-time"
# the decision names. Deliberately approximate: this is a heads-up printed
# before spending, not an invoice, and the real per-call cost varies with
# each posting's actual text length.
_EST_INPUT_TOKENS_PER_ROW = 1500
_EST_OUTPUT_TOKENS_PER_ROW = 80
_EST_INPUT_PRICE_PER_1K_TOKENS = 0.001
_EST_OUTPUT_PRICE_PER_1K_TOKENS = 0.005


def _estimated_cost(n_rows: int) -> float:
    per_row = (
        _EST_INPUT_TOKENS_PER_ROW / 1000 * _EST_INPUT_PRICE_PER_1K_TOKENS
        + _EST_OUTPUT_TOKENS_PER_ROW / 1000 * _EST_OUTPUT_PRICE_PER_1K_TOKENS
    )
    return n_rows * per_row


class Command(BaseCommand):
    help = ("Fill missing sponsorship answers via an LLM third pass, over rows the "
            "regex and firm-policy fallback both found nothing on. Reports by default.")

    def add_arguments(self, parser):
        parser.add_argument("--commit", action="store_true",
                             help="Write the recovered answers. Default is report-only "
                                  "(the API is still called and billed either way).")
        parser.add_argument("--limit", type=int, default=50,
                             help="Max rows to send to the API this run (0 = no limit). "
                                  "Default is deliberately small: every row is a paid call.")
        parser.add_argument("--ids", type=str, default="",
                             help="Comma-separated Opportunity ids to restrict the run to, "
                                  "bypassing --limit's ordering.")

    def _eligible_rows(self, ids_arg: str, limit: int) -> list[Opportunity]:
        """Open campus rows where BOTH the posting and the firm's policy are
        silent (see directory.sponsorship.effective_sponsorship — the exact
        precedence the feed filter and _eligibility use), with cached text
        that mentions a sponsorship-adjacent keyword. Computed in Python,
        not a queryset filter: `effective_sponsorship` needs each row's own
        firm, and the eligible set (a few hundred rows at most) is far too
        small to justify a bespoke ORM query for it."""
        if ids_arg:
            ids = [int(x) for x in ids_arg.split(",") if x.strip()]
            candidates = Opportunity.objects.filter(id__in=ids).select_related("firm")
        else:
            candidates = (
                Opportunity.objects.filter(status="open", bucket__in=TARGET_BUCKETS)
                .exclude(raw__detail_text="")
                .select_related("firm")
                .order_by("id")
            )

        rows = []
        for o in candidates.iterator():
            text = (o.raw or {}).get("detail_text") or ""
            if not text or not _KEYWORD.search(text):
                continue
            value, _source = effective_sponsorship(o)
            if value != "unknown":
                continue
            rows.append(o)
            if not ids_arg and limit and len(rows) >= limit:
                break
        return rows

    def handle(self, *args, **opts):
        if not is_configured():
            self.stdout.write(self.style.WARNING(
                "ANTHROPIC_API_KEY is not set — nothing to do. "
                "See .env.example's \"AI features\" section."))
            return

        commit, limit = opts["commit"], opts["limit"]
        tag = "" if commit else "[dry-run, no write] "

        rows = self._eligible_rows(opts["ids"], limit)
        if not rows:
            self.stdout.write("No eligible rows (open, campus-bucket, cached text, "
                               "sponsorship keyword present, both posting and firm silent).")
            return

        estimate = _estimated_cost(len(rows))
        self.stdout.write(
            f"{len(rows)} row(s) eligible · estimated cost ${estimate:.2f} "
            f"(~{_EST_INPUT_TOKENS_PER_ROW} input + ~{_EST_OUTPUT_TOKENS_PER_ROW} "
            f"output tokens/row) · {'WRITING' if commit else 'dry-run, no write'}")

        found = failed = 0
        for opp in rows:
            text = (opp.raw or {}).get("detail_text") or ""
            try:
                guess = extract_sponsorship_ai(text)
            except AIExtractError as e:
                failed += 1
                self.stderr.write(f"  ! {opp.firm.name} — {opp.title[:44]}: {e}")
                continue
            if guess is None:
                continue
            found += 1
            self.stdout.write(
                f"{tag}+ {opp.firm.name} — {opp.title[:44]}: "
                f"sponsorship {guess.value}  ·  \"{guess.phrase[:100]}\"")
            if not commit:
                continue
            opp.sponsorship = guess.value
            opp.raw = {**(opp.raw or {}), "sponsorship_source": "ai",
                       "sponsorship_quote": guess.phrase}
            opp.save(update_fields=["sponsorship", "raw"])

        self.stdout.write(self.style.SUCCESS(
            f"{tag}{len(rows)} row(s) sent · {found} answer(s) found · "
            f"{failed} API failure(s) · est. cost ${estimate:.2f}"))
