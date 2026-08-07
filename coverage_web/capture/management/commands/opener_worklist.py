"""opener_worklist — contacts whose Compose button opens an empty email.

    python manage.py opener_worklist --email you@example.com [--limit 8]

Feeds the opener-drafting scheduled task the same way capture_worklist feeds
the Gmail sync: real selection logic lives here, tested, instead of pasted
into a skill file as an untested shell snippet.

WHO QUALIFIES
-------------
Active (non-archived, non-parked) contacts with no opener and enough recorded
substance to draft one honestly: a name, a resolvable firm, and at least one
of role / notes / angle. The substance bar is the point — a drafting pass
given nothing but "Ben, Citi" can only pad the gap with invented rapport, and
an invented opener is worse than the empty Compose window it replaces. Those
contacts are listed under NEEDS_SUBSTANCE instead, so the report can say "add
one line about these people and tomorrow's pass covers them".

Cold-first ordering: the contacts the cadence engine will push hardest (never
touched, then longest silent) are the ones whose Compose button gets used
soonest.

Output, tab-separated one contact per line:
    id \t name \t firm \t role \t school \t angle \t notes
then a blank line and NEEDS_SUBSTANCE: name (firm) per line.
"""

from __future__ import annotations

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db.models import Max

from crm.models import Contact


class Command(BaseCommand):
    help = "Contacts with no opener draft, coldest first, substance-gated."

    def add_arguments(self, parser):
        parser.add_argument("--email", required=True)
        parser.add_argument("--limit", type=int, default=8)

    def handle(self, *args, **opts):
        User = get_user_model()
        try:
            user = User.objects.get(email=opts["email"])
        except User.DoesNotExist:
            raise CommandError(f"no user {opts['email']!r}")

        base = (Contact.objects.for_user(user)
                .filter(archived=False, opener="")
                .exclude(thread_state="parked")
                .select_related("firm")
                # last_touch_ts is an annotation, not a column — the same
                # Max("touches__ts") the Network views derive.
                .annotate(last_touch_ts=Max("touches__ts")))

        draftable, thin = [], []
        for c in base:
            firm = c.firm.name if c.firm else (c.firm_text or "")
            substance = any(((c.role or "").strip(), (c.notes or "").strip(),
                             (c.angle or "").strip()))
            if (c.name or "").strip() and firm.strip() and substance:
                draftable.append((c, firm))
            elif (c.name or "").strip():
                thin.append((c, firm))

        # Never touched first, then longest silent — the cadence engine's own
        # urgency order, so drafts land where Compose gets clicked soonest.
        draftable.sort(key=lambda pair: (pair[0].last_touch_ts is not None,
                                         pair[0].last_touch_ts or pair[0].created))

        def clean(s):
            return " ".join((s or "").split())

        for c, firm in draftable[: opts["limit"]]:
            self.stdout.write("\t".join([
                str(c.id), clean(c.name), clean(firm), clean(c.role),
                clean(c.school), clean(c.angle), clean(c.notes)[:400],
            ]))
        self.stdout.write("")
        for c, firm in thin:
            self.stdout.write(f"NEEDS_SUBSTANCE: {clean(c.name)} ({clean(firm) or 'no firm'})")
