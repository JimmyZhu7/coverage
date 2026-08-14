"""Post-chat debrief: the write path for `ChatDebrief` and the query that
decides when Today should ask for one.

Deterministic and structured on purpose — no model, no summarization. The
user answers four small questions and the app does the bookkeeping it can
verify: append the note, create the referral contact, create the tasks,
offer (never perform) the advocate promotion.

Two invariants this module owns:

1. IDEMPOTENCE. `record()` is safe to call twice with the same payload.
   One debrief per chat touch is enforced by the DB constraint; each side
   effect is additionally guarded by its own already-done marker on the row
   (first write wins, see `record`). Re-submitting the form cannot produce
   a second referral contact, a second task, or a second note append.

2. EXPIRY. `pending()` only asks about RECENT chats. The cadence engine
   learned this the hard way: replaying months of historical chats at the
   founder cutover produced a wall of stale thank-you prompts, which is why
   `coverage_domain.cadence.CADENCE_DEFAULTS["thank_you_expires_after_days"]`
   exists. The same reasoning applies verbatim here — what you remember from
   a chat decays, so a debrief prompt for a chat six weeks ago is asking for
   a worse answer AND burning the user's trust in the queue. Past the window
   the prompt simply stops.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from django.db import transaction
from django.utils import timezone

from . import services
from .models import ChatDebrief, Contact, Task, Touch
from .utils import _calendar_days_ago

# Mirrors cadence's `thank_you_expires_after_days` (7) deliberately: both
# answer "how long is it still natural to be acting on that conversation?".
# Kept as a local constant rather than a cadence param because the debrief
# is a web-layer feature — coverage_domain neither knows nor should know
# that ChatDebrief exists.
DEBRIEF_EXPIRES_AFTER_DAYS = 7

# How far out the intro follow-up task lands. Short: a warm intro goes cold
# fast, and the person who offered it remembers offering it for about a week.
INTRO_FOLLOW_UP_DAYS = 3


def pending(user, *, as_of=None, limit: int = 6) -> list[dict]:
    """Chats that still want a debrief, most recent first.

    A chat qualifies when it is inside `DEBRIEF_EXPIRES_AFTER_DAYS`, has no
    `ChatDebrief` row (written or dismissed), and belongs to a contact the
    user hasn't already debriefed a LATER chat for — one prompt per person,
    so two chats in one week can't produce two cards for the same face.

    `days_ago` comes from `crm.utils._calendar_days_ago` — a CALENDAR-date
    difference in the request's active timezone, not `(as_of - t.ts).days`
    (a raw timedelta floor: timezone-independent, effectively
    elapsed_hours // 24). One chat used to render "2d ago" here and "3d ago"
    on the Today card for the same fact (Touch 558, ~58.46h elapsed under
    Asia/Hong_Kong: floor gives 2, calendar-diff gives 3) before that helper
    became the one formula for "how many days ago was this touch" — see its
    docstring in `crm/utils.py`.
    """
    as_of = as_of or timezone.now()
    cutoff = as_of - timedelta(days=DEBRIEF_EXPIRES_AFTER_DAYS)

    debriefed = set(
        ChatDebrief.objects.for_user(user).values_list("touch_id", flat=True)
    )
    out: list[dict] = []
    seen_contacts: set[int] = set()
    touches = (
        Touch.objects.for_user(user)
        .filter(kind="chat", ts__gte=cutoff, ts__lte=as_of)
        .select_related("contact")
        .order_by("-ts")
    )
    for t in touches:
        if t.id in debriefed or t.contact_id in seen_contacts:
            continue
        if t.contact.archived:
            continue
        seen_contacts.add(t.contact_id)
        out.append(
            {
                "touch": t,
                "contact": t.contact,
                "days_ago": _calendar_days_ago(t.ts, as_of=as_of),
            }
        )
        if len(out) >= limit:
            break
    return out


def dismiss(user, touch: Touch) -> ChatDebrief:
    """Skip this one. Writes a dismissed debrief row rather than a flag on
    the touch, so the prompt is suppressed by the same uniqueness rule that
    suppresses a completed one and there is exactly one place to look."""
    debrief, _ = ChatDebrief.all_objects.get_or_create(
        user=user,
        touch=touch,
        defaults={"contact": touch.contact, "dismissed": True},
    )
    return debrief


def _append_note(contact: Contact, learned: str, *, on: date) -> None:
    """Append the learnings to `Contact.notes` under a dated header. Plain
    ORM write: `notes` is free text the user owns, not part of the
    warmth/thread_state ratchet that must go through crm.services."""
    header = f"— Chat debrief · {on:%b %d, %Y} —"
    body = f"{header}\n{learned.strip()}"
    existing = (contact.notes or "").rstrip()
    contact.notes = f"{existing}\n\n{body}" if existing else body
    contact.save(update_fields=["notes"])


@transaction.atomic
def record(
    user,
    touch: Touch,
    *,
    learned: str = "",
    intro_name: str = "",
    intro_email: str = "",
    tracked_date: date | None = None,
    date_note: str = "",
    advocate_answer: str = "",
    today: date | None = None,
) -> tuple[ChatDebrief, dict[str, Any]]:
    """Save one debrief and perform its side effects exactly once.

    Every side effect is gated on its own already-done marker, so calling
    this again with the same payload is a no-op:

      - the note append is gated on `learned` having CHANGED
      - the referral contact + its task on `intro_contact_id` being NULL
      - the date task on `date_task_id` being NULL

    The note gate is a changed-check rather than a first-write-wins check,
    unlike the other two, and the difference is deliberate. A referral
    contact and a task are objects in the world: making a second one is a
    duplicate, so first write wins. `learned` is TEXT, and the view renders
    the form bound to the existing row — so the box is editable, the user
    edits it, and under a first-write-wins gate the branch was skipped, the
    edit was thrown away, and the view still flashed "Debrief saved." The
    resubmit is not a duplicate; it is a correction, and the only reason it
    ever looked like one is that both arrive through the same POST.

    A change appends a NEW dated block rather than rewriting the old one:
    `Contact.notes` is an append-only journal everywhere else in this
    codebase, and what the user thought after the chat and what they thought
    two days later are both true things they wrote. `ChatDebrief.learned`
    always holds the latest text, so the form round-trips what you last
    typed.

    Returns (debrief, created_things) where `created_things` names what this
    particular call actually WROTE — empty means nothing was written, and the
    caller uses that to report truthfully instead of claiming work it didn't
    do.
    """
    today = today or timezone.localdate()
    contact = touch.contact
    learned = (learned or "").strip()
    intro_name = (intro_name or "").strip()
    intro_email = (intro_email or "").strip()
    date_note = (date_note or "").strip()

    debrief, _ = ChatDebrief.all_objects.get_or_create(
        user=user, touch=touch, defaults={"contact": contact}
    )
    made: dict[str, Any] = {}
    fields: list[str] = []

    # 1. What you learned -> Contact.notes, once per distinct version.
    if learned and learned != (debrief.learned or ""):
        _append_note(contact, learned, on=today)
        debrief.learned = learned
        fields.append("learned")
        made["note"] = True

    # 2. The intro -> a new Contact at the same firm + a follow-up Task.
    if intro_name and debrief.intro_contact_id is None:
        referral = Contact(
            user=user,
            name=intro_name,
            email=intro_email,
            # Same firm by default — an intro is nearly always sideways
            # within the org. The user can move them afterwards; guessing
            # nothing here would just mean the person lands outside the
            # coverage board entirely.
            firm=contact.firm,
            firm_text=contact.firm_text,
            # Provenance, matching the `source` column's documented
            # meaning ("manual" / "import" / "capture"): this row came from
            # a referral, and the referrer is named in the note below.
            source="referral",
            region=contact.region,
            notes=f"Intro from {contact.name} · {today:%b %d, %Y}",
        )
        referral.save()
        task = Task.all_objects.create(
            user=user,
            title=f"Follow up on intro to {intro_name}",
            why=f"{contact.name} offered to introduce you.",
            due=today + timedelta(days=INTRO_FOLLOW_UP_DAYS),
            kind="intro_follow_up",
            firm=contact.firm,
            source_key=f"debrief:{debrief.pk}:intro",
        )
        debrief.intro_name = intro_name
        debrief.intro_email = intro_email
        debrief.intro_contact = referral
        debrief.intro_task = task
        fields += ["intro_name", "intro_email", "intro_contact", "intro_task"]
        made["intro_contact"] = referral
        made["intro_task"] = task

    # 3. A date they mentioned -> a Task on that date.
    if tracked_date and debrief.date_task_id is None:
        title = date_note or f"Date from your chat with {contact.name}"
        task = Task.all_objects.create(
            user=user,
            title=title,
            why=f"Mentioned by {contact.name} in your chat.",
            due=tracked_date,
            kind="debrief_date",
            firm=contact.firm,
            source_key=f"debrief:{debrief.pk}:date",
        )
        debrief.tracked_date = tracked_date
        debrief.date_note = date_note
        debrief.date_task = task
        fields += ["tracked_date", "date_note", "date_task"]
        made["date_task"] = task

    # 4. The advocate read. Recorded only — the promotion is OFFERED by the
    #    UI and performed by `promote()` if the user takes it.
    if advocate_answer and advocate_answer != debrief.advocate_answer:
        debrief.advocate_answer = advocate_answer
        fields.append("advocate_answer")
        made["advocate_answer"] = advocate_answer

    # A dismissed debrief that later gets filled in is no longer dismissed.
    if debrief.dismissed:
        debrief.dismissed = False
        fields.append("dismissed")
        made["undismissed"] = True

    if fields:
        debrief.save(update_fields=fields)
    return debrief, made


def promote(debrief: ChatDebrief) -> dict[str, str]:
    """Take the offered advocate promotion. Goes through
    `crm.services.set_contact_state` (never a direct UPDATE) so the move
    lands in the touches audit trail with a note saying where it came
    from. Idempotent: a second call is a no-op."""
    if debrief.promoted:
        return {}
    updates = services.set_contact_state(
        debrief.user_id,
        debrief.contact_id,
        warmth="advocate",
        note=(
            "Promoted to advocate from the chat debrief on "
            f"{debrief.created:%Y-%m-%d} (they said they'd advocate for you)."
        ),
    )
    debrief.promoted = True
    debrief.save(update_fields=["promoted"])
    return updates
