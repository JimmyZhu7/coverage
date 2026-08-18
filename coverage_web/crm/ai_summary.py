"""Two or three sentences on where one relationship actually stands, written
from that contact's own logged history and cached on the row.

WHY THIS EXISTS
---------------
`crm/ai_brief.py` drafts a prep page for a chat that hasn't happened yet.
This is the other half: the memory of the chats that already did. A student
with two hundred contacts opens a name they last spoke to in March and gets a
list of enum rows — `reply_received · email`, `chat · coffee_chat` — with no
one sentence saying what this person is to them. That sentence is what this
module writes, once, on request, and keeps on the row until it's rewritten.

WHERE IT MAY WRITE — the whole safety story
-------------------------------------------
`Contact.notes` and `Contact.angle` are the STUDENT'S own words about this
person. Nothing here writes to either, ever. They are read as CONTEXT for the
prompt and the model is told not to restate them; the only destination of any
write in this module is `Contact.ai_summary` / `ai_summary_generated_at`, and
the `update_fields` on the single `save()` below names those two columns and
nothing else — so a bug in the prompt, the parsing, or the caller cannot
reach the student's notes even by accident.

Everything else follows `assistant/brief.py`'s posture, which this is shaped
after:

  - ALWAYS the cheap tier (`SUMMARY_MODEL`), whatever plan the student is on.
    A relationship recap is bookkeeping prose, not the judgement call a Pro
    plan buys (`assistant.plans.limits_for` is deliberately not consulted).
  - NEVER raises. No key configured, a network failure, a malformed reply:
    all of them return `None` and leave the stored summary exactly as it was.
    The contact page must render identically whether or not this works.
  - RETURNS None RATHER THAN FILLER. Under `MIN_TOUCHES` logged interactions
    there is nothing specific to say, so no call is spent; and a model that
    can't find anything specific in what it was given is told to answer
    `NOTHING TO SAY`, which is also a `None`. A generated "this is a valuable
    contact" would be worse than an empty box.

GENERATION IS EXPLICIT, NOT LAZY-ON-VIEW. `crm.views.contact_ai_summary` is a
POST behind a button, the same rule `crm.views.contact_ai_brief` already
states out loud: this is a paid call once `ANTHROPIC_API_KEY` is set, so it
must never fire from a prefetch, a browser reload, or a crawler walking the
contact list. The contact page instead COUNTS the touches logged since the
stamp (`touches_since_summary`) and says when the note has fallen behind —
staleness is shown, never silently spent on.
"""

from __future__ import annotations

from django.utils import timezone

from directory.ai_extract import complete_text, is_configured

# Never the plan-selected model (assistant.plans) — see module docstring.
# Same constant as assistant.brief.BRIEF_MODEL, for the same reason.
SUMMARY_MODEL = "claude-haiku-4-5-20251001"
MAX_TOKENS = 220
MAX_SUMMARY_CHARS = 600
# The same slice `assistant.tools._get_contact` hands the advisor when it
# looks a contact up — one definition of "recent interactions" for both, so a
# summary can never be written from a history the advisor cannot also see.
MAX_TOUCHES = 8
MAX_NOTE_CHARS = 400
# Under two logged interactions there is no relationship to narrate, only a
# single event the history already shows in one line. Don't spend a call to
# paraphrase it.
MIN_TOUCHES = 2
# Touches logged since the stamp before the page calls the summary stale.
# Three is "enough new history to plausibly change the story" — one reply
# rarely rewrites a relationship, and a lower bar would nag on every touch.
STALE_AFTER_TOUCHES = 3

# The model's own escape hatch. Anything containing this is read as "I have
# nothing specific to say", the same honesty `assistant.brief` gets for free
# by not calling at all on an empty queue.
_NOTHING_TO_SAY = "NOTHING TO SAY"

_PROMPT_HEADER = """You are writing one short relationship note inside a student's private recruiting CRM. It says where the student stands with ONE contact, so they can pick the relationship back up months later without re-reading the whole log.

Use ONLY the facts given below. Never invent a meeting, a topic, a title, a firm detail, or an opinion that is not stated here, and never guess what either person is thinking or feeling beyond what the log shows.

Write 2 to 3 sentences of plain prose. No markdown, no bullet points, no headings, no preamble, no sign-off. Do not address the student or the contact directly, and do not start with the contact's name as a label.

Be specific or write nothing. Name the real channel, the real thing that was discussed, the real gap since the last exchange. Generic filler -- "a valuable contact", "a promising connection", "keep nurturing the relationship" -- is worse than a shorter note.

The student's own private notes appear below as CONTEXT for you to write from. Do not quote or restate them verbatim: the student can already read their own notes. Write what the interaction history shows.

If the history below is too thin to say anything specific, reply with exactly: NOTHING TO SAY
"""


def _recent_touches(contact) -> list:
    """This contact's most recent interactions, newest first, capped."""
    return list(contact.touches.order_by("-ts")[:MAX_TOUCHES])


def _touch_lines(touches) -> list[str]:
    # Imported here, not at module scope: `crm.views` imports this module, so
    # a top-level import back into it is a circular import at app load.
    from crm.views import _display_note

    lines = []
    for t in touches:
        note = _display_note(t.note)[:MAX_NOTE_CHARS]
        line = f"- {t.ts.date().isoformat()}: {t.kind}"
        if t.channel:
            line += f" via {t.channel}"
        if note:
            line += f" -- {note}"
        lines.append(line)
    return lines


def build_prompt(contact, touches) -> str:
    """The full prompt for `contact`, built entirely from rows already scoped
    to that contact's owner. `notes` and `angle` go in as context and are
    labelled as the student's own words — they are never the destination of
    anything this module writes."""
    parts = [_PROMPT_HEADER, "\nCONTACT"]
    parts.append(f"Name: {contact.name}")
    firm_name = contact.firm.name if contact.firm_id else (contact.firm_text or "unknown firm")
    parts.append(f"Firm: {firm_name}")
    if contact.role:
        parts.append(f"Role: {contact.role}")
    if contact.angle:
        parts.append(
            "Student's private note on their angle with this person (context "
            f"only, do not restate): {contact.angle.strip()[:MAX_NOTE_CHARS]}"
        )
    if contact.notes:
        parts.append(
            "Student's own freeform notes (context only, do not restate): "
            f"{contact.notes.strip()[:MAX_NOTE_CHARS]}"
        )

    parts.append("\nINTERACTION HISTORY (most recent first)")
    parts.extend(_touch_lines(touches))
    return "\n".join(parts)


def touches_since_summary(contact, touches=None) -> int:
    """How many interactions have been logged since the stored summary was
    written. 0 when there is no summary yet — "stale" is a claim about an
    existing note, and the page says "none written" for that case instead.

    `touches` lets a caller that has already loaded the history (the contact
    page has) pass it in rather than paying for a second query."""
    stamp = contact.ai_summary_generated_at
    if not contact.ai_summary or stamp is None:
        return 0
    if touches is not None:
        return sum(1 for t in touches if t.ts > stamp)
    return contact.touches.filter(ts__gt=stamp).count()


def is_stale(contact, touches=None) -> bool:
    return touches_since_summary(contact, touches) >= STALE_AFTER_TOUCHES


def regenerate(contact) -> str | None:
    """Write a fresh summary onto `contact` and return it, or return `None`
    and leave the row untouched.

    `None` covers every non-success: the API is dark, the history is too
    thin, the model had nothing specific to say, or the call failed. In all
    of them any previously stored summary and its stamp survive unchanged —
    a failed regeneration must never cost the student the note they already
    had. Never raises; see the module docstring."""
    if not is_configured():
        return None

    touches = _recent_touches(contact)
    if len(touches) < MIN_TOUCHES:
        return None  # nothing to narrate — don't spend a call to say so

    try:
        text = complete_text(
            build_prompt(contact, touches),
            model=SUMMARY_MODEL,
            max_tokens=MAX_TOKENS,
        )
    except Exception:  # noqa: BLE001 — a summary must never break the page
        return None

    text = (text or "").strip()
    if not text or _NOTHING_TO_SAY in text.upper():
        return None
    text = text[:MAX_SUMMARY_CHARS]

    contact.ai_summary = text
    contact.ai_summary_generated_at = timezone.now()
    # THE write-path guarantee, in one line: these two columns and no others.
    # `notes` and `angle` are not named here and are not assigned above, so
    # neither can be reached from this module even by accident.
    contact.save(update_fields=["ai_summary", "ai_summary_generated_at"])
    return text
