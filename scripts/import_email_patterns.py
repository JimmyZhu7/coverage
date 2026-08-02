"""Port the email-pattern ledger from campaign.db into Coverage.

WHAT THIS DATA IS. Every outbound email either lands or bounces. Counted per
firm, that is a running measurement of whether the address format you guessed
for that firm is the right one — 18 delivered and 0 bounced at Morgan Stanley
says `first.last@morganstanley.com` works; 0 delivered and 2 bounced at
Mizuho says the guess there is wrong and should not be trusted for the next
contact. Months of sending built it and nothing regenerates it, which is why
it is worth moving rather than leaving behind.

WHY IT IS SHARED, NOT PRIVATE. `EmailPatternStats` is keyed on the FIRM, with
no user column, and that is deliberate (build-plan §2): the aggregate counts
help everyone, while the raw bounce events — who you emailed and when — stay
in your own private Touch rows. Nothing identifying moves here; two integers
per firm do.

MERGE, DO NOT REPLACE. Coverage may already hold counts for a firm from its
own sending. The two ledgers describe the same underlying fact from different
windows, so the counts are ADDED. Re-running would therefore double-count,
which is why this writes a marker file and refuses a second run unless
`--force` is passed.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from pathlib import Path

import django

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "coverage_web"))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "coverage_web.settings.local")
django.setup()

from directory.models import EmailPatternStats, Firm  # noqa: E402

CAMPAIGN_DB = Path(
    "/Users/zhujimmy/Claude/Projects/Recruitment Opportunities/campaign/campaign.db"
)
MARKER = Path(__file__).resolve().parent.parent / "data" / "imports" / ".email_patterns_imported"

# campaign.db firm ids that differ from Coverage's slugs. Empty, and checked:
# Coverage's directory was seeded from the same firms.yaml, so the two id
# spaces are identical down to the abbreviations (`db`, `stanchart`,
# `bnpparibas` are the slugs on both sides). The first draft of this script
# "helpfully" expanded those three to `deutsche-bank` / `standard-chartered` /
# `bnp-paribas`, which resolved to nothing and would have dropped 12 delivered
# and 2 bounced on the floor. The dry run caught it. Left here as the seam for
# a genuine future mismatch — but do not add an entry without checking the
# slug actually exists.
SLUG_ALIASES: dict[str, str] = {}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true",
                    help="Import again even though the marker says it already ran. "
                         "Counts are ADDED, so this double-counts.")
    args = ap.parse_args()

    if MARKER.exists() and not args.force:
        print(f"Already imported (marker: {MARKER}).")
        print("Counts are additive, so re-running would double-count. "
              "Pass --force only if you mean it.")
        return 0
    if not CAMPAIGN_DB.exists():
        print(f"campaign.db not found at {CAMPAIGN_DB}", file=sys.stderr)
        return 1

    conn = sqlite3.connect(f"file:{CAMPAIGN_DB}?mode=ro", uri=True)
    rows = conn.execute(
        "SELECT firm, delivered_count, bounced_count FROM email_pattern_stats"
    ).fetchall()
    conn.close()

    by_slug = {f.slug: f for f in Firm.objects.all()}
    tag = "[dry-run] " if args.dry_run else ""
    merged = created = 0
    unresolved: list[str] = []

    for raw_slug, delivered, bounced in sorted(rows, key=lambda r: -r[1]):
        slug = SLUG_ALIASES.get(raw_slug, raw_slug)
        firm = by_slug.get(slug)
        if firm is None:
            unresolved.append(f"{raw_slug} ({delivered}d/{bounced}b)")
            continue

        stats = EmailPatternStats.objects.filter(firm=firm).first()
        if stats is None:
            created += 1
            print(f"{tag}NEW   {slug:<20} {delivered}d / {bounced}b")
            if not args.dry_run:
                EmailPatternStats.objects.create(
                    firm=firm, delivered=delivered, bounced=bounced
                )
        else:
            merged += 1
            print(f"{tag}MERGE {slug:<20} "
                  f"{stats.delivered}d/{stats.bounced}b + {delivered}d/{bounced}b "
                  f"= {stats.delivered + delivered}d/{stats.bounced + bounced}b")
            if not args.dry_run:
                stats.delivered += delivered
                stats.bounced += bounced
                stats.save(update_fields=["delivered", "bounced", "last_updated"])

    if unresolved:
        print("\nNo matching Coverage firm (skipped, nothing invented):")
        for u in unresolved:
            print(f"  {u}")

    print(f"\n{tag}{created} new, {merged} merged, {len(unresolved)} unresolved")
    if not args.dry_run:
        MARKER.parent.mkdir(parents=True, exist_ok=True)
        MARKER.write_text("imported from campaign.db\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
