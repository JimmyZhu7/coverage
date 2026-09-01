"""audit_chat_claims — list every contact standing at a chat state, and what
evidence put them there (report-only by default).

    python manage.py audit_chat_claims --email you@example.com
    python manage.py audit_chat_claims --email you@example.com --revert 462 --commit

WHY THIS EXISTS
---------------
`chat_scheduled` and `chat_done` are the two most expensive states in the
product. `chat_done` sets warmth to `chatted`, and `capture_worklist`'s
`RECHECK_WARMTH` drops a `chatted` contact from every future re-check, so a
wrong one is unrecoverable by any later scan. `chat_scheduled` puts a
standing "did it happen? log the chat or reschedule" card on Today.

Three separate capture paths could write those states off a language
judgement with no corroboration. Every one of them is gated now (see
`capture.providers.corroborated_chat_status`: a chat claim needs a stated
time). This command is for the rows written before that gate existed.

WHY IT DOES NOT DECIDE FOR YOU
------------------------------
The obvious rule -- "no CalendarEvent means no chat" -- is wrong, and
measurably so. On the founder's own board six contacts carry a chat state
with no calendar row, and they are not the same case:

    James Bai      "replied twice and confirmed a call"   a real chat, no .ics
    Tanner K.      "Wrote to you from a firm address"     not a chat at all
    Patina Zhu     "Reply from patina"                    a reply, filed as a chat
    Youqi Chen     "Discovered by mailbox scan"           no citation, no evidence
    Patina Chu     "Discovered by mailbox scan"           no citation, no evidence
    Liwen Zhang    (empty note)                           no evidence at all

A calendar row proves a chat was booked. Its ABSENCE proves only that no
.ics was ever parsed, which is the normal case for a chat agreed in prose.
Reverting all six would erase James Bai's real history to clean up five
bad rows, and this codebase's rule for that trade is already written down
in `reclassify_inbound_touches`: demoting someone who really did write
back "silences a real relationship, and unlike the noise it is fixing, a
silence is invisible".

So this reports, sorts by how well corroborated each claim is, and prints
the evidence. A human reads it and names the ones to walk back. Nothing is
written without BOTH `--revert <contact_id>` and `--commit`.

Live network: none. Live database: read-only unless `--commit`.
"""

from __future__ import annotations

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

CHAT_STATES = ("chat_scheduled", "chat_done")
CHAT_TOUCH_KINDS = ("chat", "chat_scheduled")


def _evidence(user, contact):
    """What actually stands behind this contact's chat state.

    Three tiers, strongest first, and the ordering is the whole point: a
    calendar row is a fact the student's own calendar agrees with, a
    message citation means a specific email was read and can be re-read,
    and a bare note is somebody's summary with nothing to check it against.
    """
    from crm.models import CalendarEvent, Touch

    events = CalendarEvent.all_objects.filter(user=user, contact=contact).count()
    touch = (
        Touch.all_objects.filter(user=user, contact=contact, kind__in=CHAT_TOUCH_KINDS)
        .order_by("ts")
        .first()
    )
    note = (touch.note or "").strip() if touch else ""
    cited = note.startswith("[gmail:")
    if events:
        tier = "CALENDAR"
    elif cited:
        tier = "CITED"
    else:
        tier = "UNCITED"
    return tier, events, touch, note


class Command(BaseCommand):
    help = "Report contacts standing at a chat state and the evidence for it."

    def add_arguments(self, parser):
        parser.add_argument("--email", required=True)
        parser.add_argument(
            "--revert", type=int, default=None,
            help="Contact id to walk back to `replied`/`replied`. Needs --commit.",
        )
        parser.add_argument("--commit", action="store_true")

    def handle(self, *args, **opts):
        from crm.models import Contact

        User = get_user_model()
        user = User.objects.filter(email=opts["email"]).first()
        if user is None:
            raise CommandError(f"no user with email {opts['email']}")

        rows = list(
            Contact.objects.for_user(user)
            .filter(thread_state__in=CHAT_STATES, archived=False)
            .order_by("name")
        )

        # `--revert` is answered BEFORE the empty-board shortcut below. A
        # first version returned "no contact is standing at a chat state"
        # and exited 0 for a `--revert` naming a contact that genuinely was
        # not at one, which reads as "done" rather than as the refusal it is.
        target = opts["revert"]
        if target is not None:
            return self._revert(user, rows, target, commit=opts["commit"])

        if not rows:
            self.stdout.write("No contact is standing at a chat state.")
            return

        # Report. Weakest evidence LAST so the tail of the output is the part
        # worth acting on, which is where a reader's attention actually is.
        order = {"CALENDAR": 0, "CITED": 1, "UNCITED": 2}
        graded = sorted(
            ((_evidence(user, c), c) for c in rows),
            key=lambda pair: (order[pair[0][0]], pair[1].name),
        )
        for (tier, events, touch, note), contact in graded:
            when = f"{touch.ts:%Y-%m-%d}" if touch else "no chat touch"
            self.stdout.write(
                f"[{tier:9}] #{contact.id} {contact.name} "
                f"({contact.warmth}/{contact.thread_state})"
            )
            self.stdout.write(f"            {when}  {note[:88] or '(no note)'}")
        weak = sum(1 for (t, *_), _ in graded if t == "UNCITED")
        self.stdout.write("")
        self.stdout.write(
            f"{len(graded)} at a chat state. {weak} rest on a note with no "
            f"message citation and no calendar row."
        )
        self.stdout.write(
            "A missing calendar row is not proof a chat did not happen. Read "
            "the evidence, then revert only the ones you know are wrong:"
        )
        self.stdout.write(
            f"  manage.py audit_chat_claims --email {opts['email']} "
            f"--revert <id> --commit"
        )

    def _revert(self, user, rows, contact_id, *, commit):
        """Walk ONE contact back to `replied`, through the audited path."""
        contact = next((c for c in rows if c.id == contact_id), None)
        if contact is None:
            raise CommandError(
                f"contact {contact_id} is not standing at a chat state for this user"
            )
        tier, events, touch, note = _evidence(user, contact)
        self.stdout.write(
            f"#{contact.id} {contact.name}: {contact.warmth}/{contact.thread_state} "
            f"-> replied/replied   (evidence: {tier})"
        )
        if tier == "CALENDAR":
            self.stdout.write(self.style.WARNING(
                f"  This contact has {events} calendar event(s). A chat was "
                f"genuinely booked. Reverting is probably wrong."
            ))
        if not commit:
            self.stdout.write("Nothing written. Re-run with --commit.")
            return
        # `set_contact_state`, never a bare UPDATE: it is the only path that
        # can move warmth DOWN, and it writes its own `manual_override` audit
        # touch so the contact's History shows why they cooled.
        from crm import services as crm_services

        crm_services.set_contact_state(
            user.id, contact.id,
            warmth="replied", thread_state="replied",
            note="Chat claim withdrawn: no corroborating time or calendar row.",
        )
        self.stdout.write(self.style.SUCCESS("Written, with an audit touch."))
