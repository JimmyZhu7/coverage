"""scrape — assemble BoardConfigs and run the ingest service.

    python manage.py scrape                       # every documented board
    python manage.py scrape --provider greenhouse # one provider
    python manage.py scrape --provider greenhouse --limit 2
    python manage.py scrape --firm williamblair   # one firm's board(s)
    python manage.py scrape --no-close            # skip closed-detection

WHERE BOARD IDENTIFIERS COME FROM
---------------------------------
From `directory/boards.py` — a documented catalog transcribed from the
founder's live-verified `_ATS_BOARDS`, NOT from the `firms` table. The `firms`
schema has no column for ATS board tokens (greenhouse token / lever org /
workday tenant+site+search_text), and that schema is owned by another
workstream and must not be altered here. See `boards.py` and the project report
for this gap. Each catalog entry names its firm by slug; `scrape` reads the
seeded `Firm` row to stamp the board with the firm's canonical name so `ingest`
resolves it back to exactly that row (no duplicate firms).

Request volume: only the selected boards are fetched, once each, through the
connector package's own `ThreadPoolExecutor(max_workers=8)`. No polling loop.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand

from directory import ingest
from directory.boards import DEFAULT_TRACKS, select_boards
from directory.models import Firm

# Keep in lockstep with coverage_connectors.CONNECTORS — a provider missing
# here still scrapes in the full pass but cannot be selected with --provider.
_PROVIDERS = ("greenhouse", "lever", "workday", "oracle", "talnet", "sitemap",
              "mckinsey", "phenom", "goldmansachs", "talentgateway", "eightfold",
              "beisen", "avature", "lumesse", "socgen", "talentsoft", "icims")


class Command(BaseCommand):
    help = "Fetch the documented ATS boards and upsert postings into the shared opportunities table."

    def add_arguments(self, parser):
        parser.add_argument("--provider", choices=_PROVIDERS, default=None)
        parser.add_argument("--firm", default=None, help="Restrict to one firm slug.")
        parser.add_argument("--limit", type=int, default=None, help="Cap the number of boards fetched.")
        parser.add_argument("--no-close", action="store_true",
                            help="Skip closed-detection (upsert only, never flip status to closed).")

    def handle(self, *args, **opts):
        selected = select_boards(
            provider=opts["provider"], firm_slug=opts["firm"], limit=opts["limit"]
        )
        if not selected:
            self.stderr.write(self.style.WARNING("No boards matched the given filters."))
            return

        # Stamp each board with the seeded firm's canonical name (so ingest
        # resolves back to that Firm row rather than forking a new one).
        # Catalog firms missing from the DB (not in the founder's seed set —
        # Brattle, Oliver Wyman, ...) are pre-created here under their CATALOG
        # slug with their DEFAULT_TRACKS vertical, instead of letting ingest's
        # auto-create derive a drifting slugified name with no tracks.
        firm_by_slug = {f.slug: f for f in Firm.objects.filter(slug__in=[s for s, _ in selected])}
        boards = []
        for slug, board in selected:
            firm = firm_by_slug.get(slug)
            if firm is None:
                firm = Firm.objects.create(
                    slug=slug, name=board.firm,
                    tracks=DEFAULT_TRACKS.get(slug, []), status="active",
                )
                firm_by_slug[slug] = firm
                self.stdout.write(f"  created catalog firm: {slug} ({board.firm})")
            elif not firm.tracks and slug in DEFAULT_TRACKS:
                firm.tracks = DEFAULT_TRACKS[slug]
                firm.save(update_fields=["tracks"])
            boards.append(_with_firm_name(board, firm.name))

        label = opts["provider"] or ("firm:" + opts["firm"] if opts["firm"] else "all")
        self.stdout.write(f"Scraping {len(boards)} board(s) [{label}] ...")

        run = ingest.ingest_boards(boards, label=label, mark_closed=not opts["no_close"])
        s = run.stats
        self.stdout.write(self.style.SUCCESS(
            f"run #{run.id} [{run.status}]: "
            f"boards {s['boards_ok']}/{s['boards_total']} ok, "
            f"fetched {s['fetched']}, "
            f"created {s['created']}, updated {s['updated']}, unchanged {s['unchanged']}, "
            f"reopened {s['reopened']}, closed {s['closed']}"
        ))
        if s["created_firms"]:
            self.stdout.write(f"  auto-created firms (not in seed set): {', '.join(s['created_firms'])}")
        if s["errors"]:
            for e in s["errors"]:
                self.stderr.write(self.style.WARNING(f"  board failed — {e['firm']} ({e['provider']}): {e['error']}"))


def _with_firm_name(board, name: str):
    """Return a copy of the frozen BoardConfig with `firm` replaced. Uses
    dataclasses.replace to preserve provider/token/site/etc."""
    import dataclasses
    return dataclasses.replace(board, firm=name)
