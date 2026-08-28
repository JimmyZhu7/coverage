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
        # Read the action's RESOLVED firm name, not the contact's raw
        # `firm_text`. A contact the CSV import successfully matched to a
        # directory firm has `firm_text` CLEARED (accounts/services.py: a
        # linked firm is the source of truth, the free text is dropped so
        # it can't go stale) — so reading `firm_text` here told the model
        # that every correctly-linked contact had "no firm on file" while
        # the one contact whose firm FAILED to match was the only one
        # carrying a name. Two review rounds in a row the daily brief then
        # confidently asserted the exact inverse of the student's own data
        # ("she's the only one connected to a target firm"). The model was
        # reading wrong data correctly. `firm_name` on the action is built
        # by coverage_domain.cadence from the linked firm first, falling
        # back to firm_text, which is the same precedence every other
        # surface in this app uses.
        firm = a.get("firm_name") or contact.get("firm_text") or "no firm on file"
        label = a.get("label") or a.get("action") or "follow up"
        reason = a.get("reason") or ""
        line = f"- {name} ({firm}): {label} — {reason}".rstrip(" —")
        closes_on = a.get("closes_on")
        if closes_on:
            line += f" (closes {closes_on})"
        lines.append(line)
    return "\n".join(lines)


# How many situation events (assistant.situation.build_situation) the prompt
# gets to see. Deliberately small and separate from the CARDS cap (also 3,
# see crm/today.py's week()) — this is the same number for the same reason:
# a brief that leads with the 4th-most-urgent change nobody's card shows
# would be talking about something the student can't see on the page.
MAX_SITUATION_SUMMARIZED = 3


def _summarize_situation(events: list[dict]) -> str:
    """A short plain-English line per situation event, for the prompt only —
    the CARDS on the page are built straight from the typed event data with
    no model involved (crm/today.py); this text exists purely so the
    one-sentence brief can decide whether a change is the single most
    important thing to lead with today, e.g. a deadline moving up outranks
    the queue's own top contact."""
    lines = []
    for e in events[:MAX_SITUATION_SUMMARIZED]:
        firm = e.get("firm") or "a firm"
        title = e.get("title") or "a role"
        kind = e.get("kind")
        if kind == "deadline_moved":
            old, new = e.get("old_value") or "no prior date", e.get("new_value") or "no date"
            lines.append(f"- {title} at {firm}: deadline moved from {old} to {new}")
        elif kind == "role_closed":
            lines.append(f"- {title} at {firm}: this posting just closed")
        elif kind == "new_role_at_known_firm":
            lines.append(f"- {firm} just opened a new role: {title}")
    return "\n".join(lines)


def get_cached(user) -> str | None:
    """Today's brief IF it has already been generated, else None. Never calls
    the model, never writes.

    Exists so the Today page can render the brief when it is already there —
    one indexed read — without the FIRST load of each day paying for the
    generation inline. Measured: with the row present the page took 55.7ms,
    and without it the model's latency landed on the response almost exactly
    1:1 (a 2.0s reply made a 2079.9ms page). That first load is the morning
    one, so the whole day's impression of the product's speed was being set
    by the one request that had to wait for an LLM.

    `crm.views.daily_brief` is the htmx endpoint that does the generating,
    after the page is already interactive. See its docstring.
    """
    today = timezone.localdate()
    row = DailyBrief.objects.for_user(user).filter(date=today).first()
    return row.text if row is not None else None


def is_pending(user) -> bool:
    """Whether a brief could still be generated for today — i.e. the feature
    is live and nothing has been written yet. False means the Today page must
    not render a placeholder that would never resolve."""
    return is_configured() and get_cached(user) is None


def get_or_build(user, actions: list[dict], situation: list[dict] | None = None, *, client=None) -> str | None:
    """Today's brief. Returns None — never raises, never shows an error —
    if the feature is dark, there is nothing worth saying today, or
    generation fails for any reason; the Today page simply omits the card
    on a day this doesn't work, the same graceful-dark posture every other
    optional integration in this app already has.

    `situation` is the flat event list from `assistant.situation.
    build_situation` (optional — callers that don't have one, or tests
    written before it existed, simply omit it and get the old queue-only
    behaviour). It extends the SAME prompt rather than triggering a second
    model call: a deadline that just moved is often more urgent than the
    queue's own top contact, and the model gets to decide that with both
    facts in front of it at once, not two independent sentences stitched
    together afterward."""
    today = timezone.localdate()
    existing = DailyBrief.objects.for_user(user).filter(date=today).first()
    if existing is not None:
        return existing.text

    if client is None and not is_configured():
        return None

    queue_summary = _summarize_actions(actions)
    situation_summary = _summarize_situation(situation or [])
    if not queue_summary and not situation_summary:
        return None  # nothing to say — don't spend a call to say so

    prompt = (
        f"Today's date is {today.isoformat()}. Any deadline below is a "
        "calendar date, not a relative one — work out how far off it "
        "actually is from today's date rather than guessing.\n\n"
        "You are Coverage's recruiting advisor. In 1-2 SHORT "
        "sentences, tell this student what matters most today — "
        "lead with the single highest-priority thing, name it "
        "specifically. No greeting, no summary of everything in the "
        "list, no hedging.\n\nWrap exactly ONE short span in **bold** — "
        "whichever single detail matters most to act on right "
        "now: a person's name, or the exact deadline/day "
        "count if that is the real urgency. Never bold more "
        "than one span, never a whole sentence, and use no "
        "other markdown at all."
    )
    if queue_summary:
        prompt += "\n\nToday's queue:\n" + queue_summary
    if situation_summary:
        prompt += (
            "\n\nThings that changed recently on roles this student tracks "
            "or firms they know (a moved deadline or a closed posting can "
            "outrank anything in the queue above):\n" + situation_summary
        )

    try:
        client = client or get_client()
        response = client.messages.create(
            model=BRIEF_MODEL,
            max_tokens=MAX_TOKENS,
            messages=[{"role": "user", "content": prompt}],
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
    # get_or_create, not a plain save(): this endpoint is reachable twice for
    # the same student/day (a double-load of Today, two tabs, a client-side
    # retry against a slow response) and both requests can pass the
    # `existing is None` check above before either has written its row.
    # `uniq_daily_brief_user_date` is exactly the guard against two rows for
    # one day, but a bare `.save()` on the loser meant the DB constraint threw
    # an uncaught IntegrityError instead of the graceful "no card" this
    # module promises everywhere else — Django's get_or_create retries its
    # own `get()` when the nested create hits that constraint, so the loser
    # quietly returns whichever text actually won the race.
    #
    # `.objects.for_user(user).get_or_create(user=user, ...)`, deliberately
    # not the unscoped escape-hatch manager: assistant/tests/test_isolation.py
    # bans that name outright anywhere in this package — this module has no
    # legitimate cross-tenant reason to reach for it. `.for_user(user)`
    # already scopes the `get()` half; `user=user` in the kwargs is what the
    # `create()` half needs, since get_or_create builds the new row from its
    # own kwargs/defaults, not from whatever filters happen to already be on
    # the queryset.
    row, _created = DailyBrief.objects.for_user(user).get_or_create(
        user=user, date=today, defaults={"text": text},
    )
    return row.text
