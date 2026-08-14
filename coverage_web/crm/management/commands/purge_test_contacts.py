"""purge_test_contacts — delete dev fixture rows left behind in a real
user's CRM by smoke runs against the live database (report-only by default).

    python manage.py purge_test_contacts            # report only (default)
    python manage.py purge_test_contacts --apply     # actually delete

WHY THIS EXISTS
---------------
Four rows written by automated smoke runs are sitting in the founder's own
production CRM, archived rather than removed:

    id  name                        source                 warmth    touches
    353 ZZZ Smoke Test Contact      automated smoke test   advocate  3
    370 ZZZ Smoke Test Add Contact  automated smoke test   chatted   1
    371 ZZZ Idempotency Test        test                   replied   1
    372 ZZZ Merge Test Contact      (blank)                cold      0

They are visible on two surfaces. `/app/contacts/archived/` lists all four by
name at the bottom of the alphabetical list, with their placeholder emails
and roles ("Test Role", "Merge Verification Row", "smoke test"), and its
header counts them: "25 archived" when 21 archived rows are real people. And
Settings > Your Data reads "Contacts — People in your private CRM. Includes
25 archived" against a value of 158, when 154 of those are people.

Both numbers are arithmetically correct — they count rows that genuinely
exist — so this is not a counter bug and there is nothing to fix in a
template. The rows themselves do not belong in a person's CRM.

id=353 additionally carries warmth='advocate'. It is kept out of the Network
board's ADVOCATES chip (which reads 2) only by its `archived` flag, since
`crm.views` filters that query on archived=False. Un-archiving it — one click
on a row a user might reasonably think is theirs to tidy up — would put a
test fixture into the advocate ladder.

WHY IT IS REPORT-ONLY, AND WHY NOBODY SHOULD RUN --apply UNASKED
----------------------------------------------------------------
This deletes a real user's CRM rows and cascades to their touches (5 of
them, including a manual_override on 353). That is destructive and
irreversible, so it needs the account owner's explicit go-ahead, not an
agent's judgement. Run it without --apply to see exactly what would go, hand
that report to the owner, and only then run --apply.

It is also not a durable fix on its own. Nothing in this repository creates
these rows — they came from ad-hoc smoke runs pointed at the live database
(hence source='automated smoke test'). Delete them today and the next such
run writes them again. The durable fix is that smoke runs get a throwaway
database; this command only cleans up what the ones already run left behind.

SAFETY
------
Every candidate must clear BOTH guards before it can be deleted, even when
named explicitly via --ids: the name must start with the fixture prefix
"ZZZ", and the row must already be archived. A row that fails either guard is
reported and skipped, never deleted. The guards are the point — an id list is
easy to mistype, and the blast radius of a wrong id here is somebody's real
contact.

Live network: none. Live database: read-only unless --apply, per this repo's
read-only-DB-by-default rule.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand
from django.db import transaction

from crm.models import Contact, Touch

# Confirmed live, this audit round. --ids overrides for any future one-off,
# but the guards in handle() still apply to whatever is named.
DEFAULT_IDS: tuple[int, ...] = (353, 370, 371, 372)

# A dev fixture announces itself in its name. Both this and `archived` must
# hold before anything is deleted.
FIXTURE_NAME_PREFIX = "ZZZ"


class Command(BaseCommand):
    help = ("Delete archived dev smoke-test contacts (and their touches) left "
            "in a real user's CRM by runs against the live database. "
            "Report-only by default; --apply deletes.")

    def add_arguments(self, parser):
        parser.add_argument(
            "--ids", type=int, nargs="+", default=None,
            help="Contact ids to purge. Defaults to this round's confirmed list.",
        )
        parser.add_argument(
            "--apply", action="store_true", default=False,
            help="Actually delete. Default is report-only.",
        )

    def handle(self, *args, **opts):
        apply_ = opts["apply"]
        tag = "" if apply_ else "[dry-run] "
        ids = list(opts["ids"] or DEFAULT_IDS)

        rows = list(Contact.all_objects.filter(id__in=ids).order_by("id"))
        for missing in sorted(set(ids) - {c.id for c in rows}):
            self.stdout.write(f"  #{missing} — no such contact; skipped")

        doomed: list[Contact] = []
        for c in rows:
            if not c.name.startswith(FIXTURE_NAME_PREFIX):
                self.stdout.write(
                    f"  #{c.id} {c.name!r} — name does not start with "
                    f"{FIXTURE_NAME_PREFIX!r}; REFUSED (this guard exists so a "
                    f"mistyped id cannot delete a real person)")
                continue
            if not c.archived:
                self.stdout.write(
                    f"  #{c.id} {c.name!r} — not archived, so it is live in the "
                    f"Network board; REFUSED")
                continue
            doomed.append(c)

        if not doomed:
            self.stdout.write("Nothing to purge.")
            return

        touch_total = 0
        for c in doomed:
            touches = list(
                Touch.all_objects.filter(contact=c).order_by("ts")
            )
            touch_total += len(touches)
            firm = c.firm.name if c.firm_id else (c.firm_text or "—")
            self.stdout.write(
                f"{tag}contact #{c.id} {c.name!r} — {firm} · "
                f"role={c.role or '—'} · email={c.email or '—'} · "
                f"source={c.source or '—'} · warmth={c.warmth}")
            for t in touches:
                self.stdout.write(
                    f"      cascade: touch #{t.id} {t.kind} "
                    f"{t.ts:%Y-%m-%d} {(t.note or '')[:60]!r}")

        if apply_:
            with transaction.atomic():
                for c in doomed:
                    c.delete()

        self.stdout.write(self.style.SUCCESS(
            f"{tag}{len(doomed)} contact(s) and {touch_total} touch(es) "
            f"{'deleted' if apply_ else 'would be deleted'}."))
