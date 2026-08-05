"""capture_worklist — who the next Gmail sync should look at, and how far back.

    python manage.py capture_worklist --email you@example.com

WHY A COMMAND AND NOT A SHELL ONE-LINER
---------------------------------------
The daily sync used to build this list with an inline `manage.py shell -c`
snippet pasted into a skill file: untested, unversioned, and impossible to fix
in one place. It is real logic — it decides who gets looked at at all — so it
lives here, with tests.

THE WINDOW IS PER CONTACT, AND THAT IS THE POINT
------------------------------------------------
The sync searches `newer_than:<N>d`, sized from the import ledger so a missed
run cannot leave a hole. That is right for someone Coverage has been watching
all along, and WRONG for someone who just arrived.

A contact with no touches has no history in Coverage at all. That is either
because you genuinely never wrote to them, or because their whole
correspondence predates the day they were added — and a two-day window can
never tell those apart. It reported the second as the first: Cindy So was
added on 1 August carrying a July thread in which she had replied, agreed a
time, and confirmed it, and every sync after that looked only at the last two
days, found nothing, and left her at zero touches. Today then told the owner
to "send the first note" to somebody who had already had a chat booked with
him (observed 2026-08-05).

So: no touches means look BACK, not just recent. It is self-limiting — the
first scan that finds anything gives them a touch and drops them to the normal
window forever — and cheap, because on a healthy board this list is nearly
always empty.
"""

from __future__ import annotations

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db.models import Count

from crm.models import Contact

# How far back a first-ever scan reaches. A recruiting relationship that
# started more than a year ago is not one this sync needs to reconstruct, and
# an unbounded search is a slow, expensive way to find nothing.
BACKFILL_DAYS = 365

# Warmth states worth re-checking. `chatted`/`advocate` are deliberately
# excluded: once a relationship is that warm an automated re-check earns
# nothing. Kept here rather than in the skill file so the rule is testable.
RECHECK_WARMTH = ("cold", "replied")


class Command(BaseCommand):
    help = "List contacts for the next Gmail sync, with a per-contact window."

    def add_arguments(self, parser):
        parser.add_argument("--email", required=True, help="Whose CRM to read.")
        parser.add_argument(
            "--window", type=int, default=2,
            help="Days for contacts Coverage already has history for "
                 "(from `capture_gmail --window`).",
        )

    def handle(self, *args, **opts):
        User = get_user_model()
        try:
            user = User.objects.get(email__iexact=opts["email"])
        except User.DoesNotExist as exc:
            raise CommandError(f"no user with email {opts['email']}") from exc

        rows = (
            Contact.objects.for_user(user)
            .filter(archived=False, warmth__in=RECHECK_WARMTH)
            .annotate(n_touches=Count("touches"))
            .select_related("firm")
            .order_by("name")
        )

        backfill = 0
        for c in rows:
            deep = c.n_touches == 0
            backfill += deep
            days = BACKFILL_DAYS if deep else opts["window"]
            firm = c.firm.name if c.firm else c.firm_text
            self.stdout.write(f"{c.id}|{c.name}|{firm}|{c.email}|{days}")

        self.stderr.write(
            f"{len(rows)} to re-check; {backfill} have no history in Coverage "
            f"and get a {BACKFILL_DAYS}-day first scan."
        )
