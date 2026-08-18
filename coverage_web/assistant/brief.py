"""The daily brief: one advisor-written paragraph surfaced at the top of
the Today page, generated at most once per student per calendar day.

WHY THIS EXISTS: everything else on Talk to Coverage requires the student to
remember to open it. This is the one habit loop that doesn't — it's already
on the page they open every day anyway (Today), so the advisor gets to say
something before it's asked a question, the same way ChatGPT/Claude never
do but a genuinely daily product needs to.

NO NEW IMPORT FROM `crm` HERE — deliberately. `crm.today` already imports
from `assistant` (`tools.py` reads `crm.today._build_actions`), so this
module must never import anything from `crm`, or Python hits a circular
import the moment either app loads. The caller (crm/today.py's own view,
which already computes the day's actions for the queue itself) passes the
action list IN; this module never goes and fetches it.

COST SHAPE: exactly one model call per student per calendar day, ever — the
`DailyBrief` row is the cache, checked before anything else runs, and once
it exists this function is a single indexed read for the rest of the day.
Always the cheap tier, like the title generator (assistant.agent._ai_title):
this is bookkeeping copy, not the judgement call a student is on a plan for.
"""

from __future__ import annotations

from django.utils import timezone

from .client import get_client, is_configured
from .models import DailyBrief

BRIEF_MODEL = "claude-haiku-4-5-20251001"
MAX_TOKENS = 150
MAX_ACTIONS_SUMMARIZED = 8
MAX_BRIEF_CHARS = 600


def _summarize_actions(actions: list[dict]) -> str:
    lines = []
    for a in actions[:MAX_ACTIONS_SUMMARIZED]:
        contact = a.get("contact") or {}
        name = contact.get("name") or "someone"
        firm = contact.get("firm_text") or "no firm on file"
        label = a.get("label") or a.get("action") or "follow up"
        reason = a.get("reason") or ""
        line = f"- {name} ({firm}): {label} — {reason}".rstrip(" —")
        closes_on = a.get("closes_on")
        if closes_on:
            line += f" (closes {closes_on})"
        lines.append(line)
    return "\n".join(lines)


def get_or_build(user, actions: list[dict], *, client=None) -> str | None:
    """Today's brief. Returns None — never raises, never shows an error —
    if the feature is dark, there is nothing worth saying today, or
    generation fails for any reason; the Today page simply omits the card
    on a day this doesn't work, the same graceful-dark posture every other
    optional integration in this app already has."""
    today = timezone.localdate()
    existing = DailyBrief.objects.for_user(user).filter(date=today).first()
    if existing is not None:
        return existing.text

    if client is None and not is_configured():
        return None

    queue_summary = _summarize_actions(actions)
    if not queue_summary:
        return None  # nothing in the queue — don't spend a call to say so

    try:
        client = client or get_client()
        response = client.messages.create(
            model=BRIEF_MODEL,
            max_tokens=MAX_TOKENS,
            messages=[
                {
                    "role": "user",
                    "content": (
                        "You are Coverage's recruiting advisor. In 1-2 SHORT "
                        "sentences, tell this student what matters most in "
                        "their queue today — lead with the single highest-"
                        "priority person or deadline, name them by name. No "
                        "greeting, no summary of everything in the list, no "
                        "hedging.\n\nWrap exactly ONE short span in **bold** — "
                        "whichever single detail matters most to act on right "
                        "now: the person's name, or the exact deadline/day "
                        "count if that is the real urgency. Never bold more "
                        "than one span, never a whole sentence, and use no "
                        "other markdown at all.\n\nToday's queue:\n" + queue_summary
                    ),
                }
            ],
        )
        text = "".join(
            (getattr(b, "text", None) or (isinstance(b, dict) and b.get("text")) or "")
            for b in response.content
        ).strip()
    except Exception:  # noqa: BLE001 — never break the Today page over this
        return None

    if not text:
        return None
    text = text[:MAX_BRIEF_CHARS]
    DailyBrief(user=user, date=today, text=text).save()
    return text
