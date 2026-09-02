"""replay_states — does each contact's state agree with their own ledger?

    python manage.py replay_states --email you@example.com          # dry run
    python manage.py replay_states --email you@example.com --apply

WHAT IT ANSWERS. `contacts.warmth` / `contacts.thread_state` are a cache of
what the touch log says: replay every touch in TIMESTAMP order through
`TOUCH_TRANSITIONS`, apply every `manual_override` where it is dated, and the
stored pair should fall out. Where it does not, something moved state in an
order the ledger does not support.

WHY IT EXISTS. Until 2026-09-01 `coverage_domain.pipeline.apply_touch`
ratcheted in WRITE order while the Gmail backfill stamped touches at
`occurred_at` (p90 35 days back), so an older event written later overturned a
newer decision. Measured on the founder's account: 302 of 306 contacts
replayed cleanly and 4 did not, all four the same mechanism — two contacts
un-parked with no un-park row, one regressed from `chat_scheduled` to
`replied`, one re-warmed after a correction. The engine now refuses to move
state on a touch older than what is already on file, so no NEW disagreement
can appear; this command is how the ones already written get fixed.

DRY RUN BY DEFAULT, and the dry run is the whole point: it prints every
contact whose stored state differs, with both pairs and the ledger's own
ordering, and writes nothing. `--apply` then moves each one through
`crm.services.set_contact_state` — the same audited override path every other
state change in this product goes through, one `manual_override` touch per
contact carrying `source="replay"` and a note saying why — never a bulk
UPDATE, and never a silent `.save()`.

The replay is deliberately the same one `crm_life_q1.py` ran read-only during
the audit, and it reads the override note through `crm.views`'s own
`_MANUAL_OVERRIDE_PARSE` rather than a second copy of that regex.
"""

from __future__ import annotations

from collections import defaultdict

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from coverage_domain.pipeline import (
    MANUAL_OVERRIDE_KIND,
    THREAD_STATES,
    TOUCH_TRANSITIONS,
    WARMTH,
    WARMTH_RANK,
)
from crm import services
from crm.models import Contact, Touch
# ONE definition of the override note's shape, not a second copy of the same
# regex: this is the parse the contact page's History tab and the parked-
# cohort grouping both already read overrides with.
from crm.views import _MANUAL_OVERRIDE_PARSE as _PARSE


def replay(touches) -> tuple[str, str]:
    """The state a contact's touches imply, read in `ts` order.

    Mirrors `apply_touch`'s ratchet exactly — warmth only up, `advocate` a
    terminal thread_state — plus `set_state`'s override, which is not
    rank-guarded and which wins for as long as nothing newer moves state.
    Starts from the model defaults ("cold", "no_reply"), which is what a row
    with no touches at all still holds."""
    warmth, state = "cold", "no_reply"
    for touch in touches:
        if touch.kind == MANUAL_OVERRIDE_KIND:
            match = _PARSE.match(touch.note or "")
            if not match:
                continue
            fields = match.group("fields") or ""
            for pair in fields.split(","):
                if "=" not in pair:
                    continue
                key, value = (part.strip() for part in pair.split("=", 1))
                if key == "warmth" and value in WARMTH:
                    warmth = value
                    # `set_state`'s implicit rule: advocate warmth with no
                    # explicit thread_state also sets the state.
                    if value == "advocate" and "thread_state" not in fields:
                        state = "advocate"
                elif key == "thread_state" and value in THREAD_STATES:
                    state = value
            continue
        new_warmth, new_state = TOUCH_TRANSITIONS.get(touch.kind, (None, None))
        if new_warmth and WARMTH_RANK[new_warmth] > WARMTH_RANK[warmth]:
            warmth = new_warmth
        if new_state and state != "advocate":
            state = new_state
    return warmth, state


class Command(BaseCommand):
    help = (
        "Replay each contact's touch log in timestamp order and report (or "
        "repair) the ones whose stored warmth/thread_state disagrees with it."
    )

    def add_arguments(self, parser):
        parser.add_argument("--email", help="One account. Omit to check all.")
        parser.add_argument(
            "--apply", action="store_true",
            help=(
                "Write the replayed state, through the audited override path, "
                "one manual_override touch per changed contact. Without this "
                "the command only reports."
            ),
        )

    def handle(self, *args, **opts):
        User = get_user_model()
        if opts["email"]:
            try:
                users = [User.objects.get(email__iexact=opts["email"])]
            except User.DoesNotExist as exc:
                raise CommandError(f"no user with email {opts['email']}") from exc
        else:
            users = list(User.objects.filter(deleted_at__isnull=True).order_by("email"))

        total_checked = total_changed = 0
        for user in users:
            contacts = list(Contact.objects.for_user(user).order_by("id"))
            if not contacts:
                continue
            by_contact = defaultdict(list)
            for touch in (
                Touch.objects.for_user(user)
                .filter(contact_id__in=[c.id for c in contacts])
                .order_by("ts", "id")
            ):
                by_contact[touch.contact_id].append(touch)

            drift = []
            for contact in contacts:
                stored = (contact.warmth, contact.thread_state)
                replayed = replay(by_contact[contact.id])
                if stored != replayed:
                    drift.append((contact, stored, replayed, len(by_contact[contact.id])))
            total_checked += len(contacts)

            self.stdout.write(self.style.MIGRATE_HEADING(
                f"{user.email}: {len(contacts) - len(drift)} of {len(contacts)} "
                f"contacts agree with their own ledger"
            ))
            if not drift:
                continue
            self.stdout.write(
                f"  {'id':>6}  {'contact':<24} {'stored':<24} {'ledger says':<24} touches"
            )
            for contact, stored, replayed, n in drift:
                self.stdout.write(
                    f"  {contact.id:>6}  {contact.name[:24]:<24} "
                    f"{'/'.join(stored):<24} {'/'.join(replayed):<24} {n}"
                )
            total_changed += len(drift)

            if not opts["apply"]:
                continue
            for contact, stored, replayed, _ in drift:
                services.set_contact_state(
                    user.id, contact.id,
                    warmth=replayed[0], thread_state=replayed[1],
                    note=(
                        "Status replayed from your own touch history, which "
                        f"reads {'/'.join(replayed)} in date order. It had "
                        f"been {'/'.join(stored)} because an older message "
                        "was recorded after a newer one."
                    ),
                    source="replay",
                )
            self.stdout.write(self.style.SUCCESS(
                f"  {len(drift)} contact(s) replayed, one audit touch each."
            ))

        verb = "repaired" if opts["apply"] else "would be repaired by --apply"
        self.stdout.write(
            f"{total_changed} of {total_checked} contact(s) {verb}."
            if total_changed else
            self.style.SUCCESS(
                f"All {total_checked} contact(s) agree with their own ledger."
            )
        )
