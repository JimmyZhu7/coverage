"""scrub_manual_override_notes — rewrite the free-text half of specific
manual-override touch notes that leak internal engineering language into a
real user's CRM History (report-only by default).

    python manage.py scrub_manual_override_notes            # report only (default)
    python manage.py scrub_manual_override_notes --apply     # write the rewrites

WHY THIS EXISTS
---------------
`crm.views._display_note` now strips the mechanical "manual override:
<column>=<value>, ..." prefix every manual-override touch carries (see that
function and its comment) — that part is machine bookkeeping and never
reaches the page any more, for any of the 111 manual-override rows in the
live DB. But the FREE-TEXT half after the em-dash is human-authored, and on
a handful of rows that text itself is written in internal, not user-facing,
language: "a capture_gmail test batch mistakenly logged a duplicate
chat_scheduled..." names a Python module and an internal state string,
"cutover: aligned to campaign.db stored state" names a migration source
table nobody outside the team has heard of. Stripping a prefix cannot fix
wording chosen inside the sentence itself — the sentence needs rewriting.
DEFAULT_IDS below is exactly the touch rows the current audit round
confirmed carry this: id=358 (James Bai, contact id=312, the row the audit
quoted verbatim) and ids 353-355 (three "cutover: aligned to campaign.db"
rows, a milder instance of the same thing — a real database table name in a
sentence a user reads about their own contact).

This only ever rewrites the HUMAN-AUTHORED half of `Touch.note` — the
"manual override: <column>=<value>" audit prefix is left exactly as
`services.set_contact_state` wrote it, since that half is the genuine audit
trail and is never shown to a user any more regardless (see
`_display_note`). Nothing else about the row (kind, ts, contact, channel)
changes.

Live network: none. Live database: read-only unless --apply, per this
repo's read-only-DB-by-default rule.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand

from crm.models import Touch

# touch_id -> new note text. Confirmed by the current audit round; --ids
# overrides for any future one-off finding so this command doesn't need to
# be rewritten each time a similar leak turns up.
DEFAULT_REPLACEMENTS: dict[int, str] = {
    # The FIRST replacement written for this row (before this change) was a
    # no-op: it restated the exact ops-voice text already in the DB
    # verbatim, so a live dry-run of this command printed "already matches;
    # skipped" and --apply would have changed nothing. "an email sync",
    # "re-threaded email conversation", and "scheduled-chat notification"
    # are all internal engineering vocabulary that survived the prefix-strip
    # fix (crm/views.py's _display_note) because that fix only ever touched
    # the machine "manual override: col=val —" prefix, never the
    # human-authored sentence after it. This rewrite states the same fact
    # in the words a student reading their own History would use.
    358: (
        "manual override: thread_state=chat_done — Correction: a reply on "
        "this email thread was briefly logged as a new scheduled chat. "
        "That call had already happened, so the status is corrected back "
        "to done."
    ),
    353: (
        "manual override: warmth=advocate, thread_state=advocate — "
        "Status aligned to your saved records during an account data "
        "migration."
    ),
    354: (
        "manual override: warmth=advocate, thread_state=advocate — "
        "Status aligned to your saved records during an account data "
        "migration."
    ),
    355: (
        "manual override: warmth=advocate, thread_state=advocate — "
        "Status aligned to your saved records during an account data "
        "migration."
    ),
}


class Command(BaseCommand):
    help = ("Rewrite the human-authored half of specific manual-override "
            "touch notes that leak internal engineering language, e.g. "
            "module names or internal state strings, into a user's CRM "
            "History. Report-only by default.")

    def add_arguments(self, parser):
        parser.add_argument(
            "--ids", type=int, nargs="+", default=None,
            help="Touch ids to rewrite. Defaults to this round's confirmed list.",
        )
        parser.add_argument(
            "--apply", action="store_true", default=False,
            help="Write the rewritten notes. Default is report-only.",
        )

    def handle(self, *args, **opts):
        apply_ = opts["apply"]
        tag = "" if apply_ else "[dry-run] "
        ids = opts["ids"] or list(DEFAULT_REPLACEMENTS)

        rows = list(
            Touch.all_objects.filter(id__in=ids, kind="manual_override")
            .select_related("contact")
        )
        missing = set(ids) - {t.id for t in rows}
        for m in missing:
            self.stdout.write(f"  #{m} — not found, or not a manual_override touch; skipped")
        if not rows:
            self.stdout.write("No matching touches to rewrite.")
            return

        rewritten = 0
        for t in rows:
            new_note = DEFAULT_REPLACEMENTS.get(t.id)
            if new_note is None:
                self.stdout.write(f"  touch #{t.id} — no replacement text supplied; skipped")
                continue
            if new_note == t.note:
                self.stdout.write(f"  touch #{t.id} — already matches; skipped")
                continue
            rewritten += 1
            who = t.contact.name if t.contact_id else f"contact #{t.contact_id}"
            self.stdout.write(f"{tag}touch #{t.id} ({who}):")
            self.stdout.write(f"    before: {t.note!r}")
            self.stdout.write(f"    after:  {new_note!r}")
            if apply_:
                t.note = new_note
                t.save(update_fields=["note"])

        self.stdout.write(self.style.SUCCESS(
            f"{tag}{len(rows)} matched, {rewritten} "
            f"{'rewritten' if apply_ else 'would be rewritten'}."))
