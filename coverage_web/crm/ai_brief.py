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

CREDIT METERING (docs/founder-decisions-2026-08-20.md §2b): this is a
user-triggered model call behind a POST button, the same shape as an
advisor chat message, so `crm/views.py::contact_ai_brief` meters it through
`billing.credits` exactly the way `assistant/agent.py::run_turn` meters a
chat turn -- `can_spend` checked BEFORE calling `generate_coffee_chat_brief`
below, `spend` written only AFTER a successful (non-None) generation. This
module itself never touches the ledger: the same separation
`crm/ai_summary.py` keeps from `record_event`, so a prompt-building bug
here can never accidentally double-charge or charge for nothing. There is
no cache to hit -- unlike `crm/ai_summary.py`'s `ai_summary` /
`ai_summary_generated_at` columns on `Contact`, a brief is never persisted,
so every "Generate brief" / "Regenerate" click is a genuine live call and
is charged exactly once per click that succeeds.
"""

from __future__ import annotations

from directory.ai_extract import complete_text, is_configured
from directory.classify import TARGET_BUCKETS
from directory.models import Opportunity

# billing.credits is imported lazily inside credit_block_notice, not at
# module scope: this module has no other reason to depend on billing, and
# the one call site (crm/views.py) already imports billing.credits itself
# for can_spend/spend -- see that view for the actual metering.

_MAX_TOUCHES = 6
_MAX_NOTE_CHARS = 400

#: Cost of one coffee-chat brief, in credits -- 1, the same anchor unit as
#: a Haiku chat message (docs/credit-system-plan.md §1). Not read from
#: CREDIT_PLANS: unlike message_cost, which varies by plan (Free Haiku vs
#: Pro Sonnet), a brief always runs on the same cheap-tier call regardless
#: of the student's plan (see generate_coffee_chat_brief), so its cost is a
#: flat constant, not a per-plan lookup.
BRIEF_COST = 1


def credit_block_notice(user) -> str:
    """The copy for a brief request stopped by the credit system, in the
    same honest, no-error voice as `assistant/agent.py::_credit_block_notice`
    -- reimplemented locally rather than imported from there, because
    `assistant.agent` imports `assistant.tools`, which imports
    `crm.views`, so importing it from here (crm.ai_brief, which crm.views
    itself imports) would be a circular import.

    Same two-reason split as the chat notice: a genuinely empty monthly
    pool reads differently from the daily burst guard tripping while the
    month's balance is still sitting there.
    """
    from billing import credits as billing_credits

    plan = billing_credits.plan_config(user)["plan"]
    label = "Pro" if plan == billing_credits.PRO else "Free"
    if billing_credits.balance(user) > 0:
        return (
            f"That's today's credit limit on the {label} plan — a safety "
            f"net, not your monthly total. It resets at midnight; your "
            f"credits for the month are still there."
        )
    refill = billing_credits.next_refill_date(user)
    upgrade = (
        " Pro comes with three times the credits and a stronger model."
        if label == "Free" else ""
    )
    return (
        f"That's the last of this month's credits on the {label} plan. "
        f"They refill on {refill:%-d %B} — Today, Network and Opportunities "
        f"are all still there.{upgrade}"
    )

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
