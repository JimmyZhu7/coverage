"""One prep page before a coffee chat: who this person is, what's been said
before, and a few questions worth asking, drafted from the contact's own
history instead of a blank page.

WHY THIS EXISTS
---------------
The cadence engine (crm/today.py) already tells a student WHEN to reach out
to someone. It has never told them WHAT to say — the student re-reads their
own notes, tries to remember the last chat, and starts from a blank
composer every time. This module drafts a short brief from exactly the
private data this app already holds on that contact: their `angle` note,
recent `Touch` history, and (when the contact is tied to a directory Firm)
that firm's most recently-seen cycle activity.

SAFETY POSTURE
---------------
Unlike `directory.ai_extract`'s single-fact grounded-quote contract, a brief
is synthesis, not extraction — there is no one sentence to verify a quote
against. The safety here is upstream instead: the prompt is built ENTIRELY
from data already scoped to this user (their own contact, their own touch
history), tells the model explicitly not to invent facts beyond what it was
given, and every brief is rendered behind a plain "AI-drafted, verify before
you rely on it" label (see crm/templates -- the caller's job, not this
module's, since where and how it's shown is a template concern). This module
never sends anything on the student's behalf; it only drafts text for them
to read or copy.

Returns `None` whenever `ai_extract.is_configured()` is False or the API
call fails -- callers show a plain "not available" state, never an error.
"""

from __future__ import annotations

from directory.ai_extract import complete_text, is_configured
from directory.classify import TARGET_BUCKETS
from directory.models import Opportunity

_MAX_TOUCHES = 6
_MAX_NOTE_CHARS = 400

_PROMPT_HEADER = """You are helping a student prepare for a coffee chat / informational call with a recruiting contact. Use ONLY the facts given below -- never invent a fact about the contact, the firm, or prior conversations that isn't stated here. If you don't have enough information for a section, say so briefly rather than guessing.

Write a short, plain-text prep brief with exactly these three sections:

BACKGROUND
One or two sentences on who this person is and the relationship so far, based only on the notes below.

WHAT WE'VE COVERED
A one-line summary of the most recent contact, if any touches are listed below. If there is no history, say "No prior contact logged."

QUESTIONS WORTH ASKING
3 to 5 specific, non-generic questions this student could ask, grounded in the contact's role/firm and the notes below -- not boilerplate like "what's a typical day like".

Keep the whole brief under 200 words. No preamble, no sign-off, just the three sections.
"""


def _touch_lines(contact) -> list[str]:
    touches = contact.touches.order_by("-ts")[:_MAX_TOUCHES]
    lines = []
    for t in touches:
        note = (t.note or "").strip()[:_MAX_NOTE_CHARS]
        when = t.ts.date().isoformat()
        line = f"- {when} ({t.kind})"
        if note:
            line += f": {note}"
        lines.append(line)
    return lines


def _firm_context(contact) -> str:
    if not contact.firm_id:
        return ""
    open_count = (
        Opportunity.objects.filter(
            firm_id=contact.firm_id, status="open", bucket__in=TARGET_BUCKETS
        ).count()
    )
    if not open_count:
        return ""
    return f"This contact's firm ({contact.firm.name}) currently has {open_count} open campus role(s) on the board."


def build_prompt(contact) -> str:
    parts = [_PROMPT_HEADER, "\nCONTACT"]
    parts.append(f"Name: {contact.name}")
    firm_name = contact.firm.name if contact.firm_id else (contact.firm_text or "unknown firm")
    parts.append(f"Firm: {firm_name}")
    if contact.role:
        parts.append(f"Role: {contact.role}")
    if contact.angle:
        parts.append(f"Student's private note about this person: {contact.angle.strip()[:_MAX_NOTE_CHARS]}")

    firm_ctx = _firm_context(contact)
    if firm_ctx:
        parts.append(f"\n{firm_ctx}")

    touch_lines = _touch_lines(contact)
    parts.append("\nRECENT CONTACT HISTORY (most recent first)")
    parts.extend(touch_lines if touch_lines else ["(none logged)"])

    return "\n".join(parts)


def generate_coffee_chat_brief(contact) -> str | None:
    """A ready-to-read prep brief for `contact`, or `None` when the AI
    feature isn't configured or the API call failed -- see module docstring
    for the caller-facing contract."""
    if not is_configured():
        return None
    prompt = build_prompt(contact)
    return complete_text(prompt, max_tokens=500)
