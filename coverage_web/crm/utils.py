"""Shared, mostly-pure helpers for the CRM's pages.

Split out of the 1,900-line crm/views.py (2026-08-05) so the Today engine
(crm/today.py) and the contact/calendar pages can stop sharing one module to
share six functions. Nothing here touches a request; everything is safe to
import from anywhere in the app without creating a cycle.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote, urlencode

# ---------------------------------------------------------------------------
# Persistence -> domain adapter.
# ---------------------------------------------------------------------------
# directory.FirmDate.confidence is stored as a FloatField (the directory UI
# reads it as a 0-1 display band), but the domain engines — cadence.due_actions'
# re-ping branch and scoring.score_firm's timeline-readiness axis — key off the
# categorical label the founder's data actually carries and only ever act on
# "confirmed_official". The seed maps the label to a lossless 3-value float
# band; here at the single point where stored rows feed the domain, we map it
# back so those branches fire. (Deeper fix — store firm_dates.confidence
# categorically and have the directory display read the label — is noted in the
# integration follow-ups; it also touches directory's timeline view, so it's
# left as a reviewed change rather than done inline here.)
_CONFIDENCE_LABELS = {1.0: "confirmed_official", 0.6: "reported", 0.3: "rumor"}


def _confidence_label(value) -> str:
    """Map a stored FirmDate.confidence to the domain's categorical label.
    Passes a string through unchanged (forward-compatible with a categorical
    column); anything unrecognized degrades to a non-confirmed label so it
    never spuriously triggers a re-ping."""
    if isinstance(value, str):
        return value
    try:
        return _CONFIDENCE_LABELS.get(round(float(value), 1), "reported")
    except (TypeError, ValueError):
        return "reported"


# ---------------------------------------------------------------------------
# Presentation constants.
# ---------------------------------------------------------------------------
# The warmth ladder, low -> high (pipeline.WARMTH_RANK order). Drives the
# animated meter: the fill grows to (index+1)/len so even a cold contact
# shows a sliver, and a ratchet move animates a visible jump.
WARMTH_ORDER = ["cold", "replied", "chatted", "advocate"]

# Friendly labels for the log-a-touch control and the weekly-list verbs.
TOUCH_KIND_LABELS: list[tuple[str, str]] = [
    ("outreach", "Reached out"),
    ("follow_up", "Followed up"),
    ("reply_received", "They replied"),
    ("chat_scheduled", "Chat scheduled"),
    ("chat", "Chat happened"),
    ("thank_you", "Sent thank-you"),
    ("reping", "Re-pinged"),
    ("maintain", "Kept warm"),
]
CHANNEL_LABELS: list[tuple[str, str]] = [
    ("email", "Email"),
    ("linkedin", "LinkedIn"),
    ("coffee_chat", "Coffee chat"),
    ("call", "Call"),
    ("event", "Event"),
    ("other", "Other"),
]

# cadence.due_actions() "action" key -> a short human verb for the UI.
ACTION_LABELS: dict[str, str] = {
    "thank_you": "Send thank-you",
    "confirm_chat": "Confirm the chat",
    "reping": "Re-ping",
    "maintain": "Keep warm",
    # cadence branch 5b. Shares the advocate branch's verb because it IS the
    # same ask (a real touch on a keep-in-touch clock); the two differ only in
    # who they're aimed at and how fast the clock runs.
    "keep_warm": "Keep warm",
    "first_outreach": "First outreach",
    "follow_up": "Follow up",
    "park": "Park it",
    "advance": "Propose a chat",
}


def _mailto(to_email: str, *, subject: str = "", body: str = "") -> str:
    """A `mailto:` URL with `to` (and optional subject/body) prefilled —
    composes start from Coverage so a contact's opener is one click away.
    `quote_via=quote` keeps spaces as %20 and the `@` as %40, which every
    mail client accepts.

    A `bcc` parameter used to live here too, pointed at the user's BCC
    capture address (docs/build-plan.md §5's v1) — retired 2026-08-19 now
    that Gmail Live reads sent mail directly, no BCC habit required.

    PRIVACY: `body` is addressed TO the contact, so only `Contact.opener` — the
    field that exists to be a draft email — may be passed here. `Contact.angle`
    must never be: it is the user's private note ABOUT the person ("USC alum,
    super responsive"), and it used to seed this body, which meant clicking
    Compose pre-filled an email to someone containing the user's assessment of
    them. Pinned by test_angle_never_leaks_into_mailto."""
    params: list[tuple[str, str]] = []
    if subject:
        params.append(("subject", subject))
    if body:
        params.append(("body", body))
    query = urlencode(params, quote_via=quote)
    base = f"mailto:{quote(to_email or '')}"
    return f"{base}?{query}" if query else base


def _warmth_pct(warmth: str) -> int:
    idx = WARMTH_ORDER.index(warmth) if warmth in WARMTH_ORDER else 0
    return round((idx + 1) / len(WARMTH_ORDER) * 100)


def _touch_dicts(touches) -> list[dict[str, Any]]:
    """Shape Touch rows into the plain dicts the domain engines read."""
    return [
        {"contact_id": t.contact_id, "ts": t.ts, "kind": t.kind, "note": t.note}
        for t in touches
    ]



def _clock(at) -> str:
    """A time short enough for a rail row: "9am", "12:30pm"."""
    minutes = f":{at.minute:02d}" if at.minute else ""
    return f"{at.strftime('%I').lstrip('0') or '12'}{minutes}{at.strftime('%p').lower()}"



FIRM_DATE_LABELS = {
    "app_open": "Applications open",
    "app_close": "Applications close",
    "insight_open": "Insight programme opens",
    "insight_deadline": "Insight deadline",
}


