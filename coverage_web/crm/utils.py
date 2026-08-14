"""Shared, mostly-pure helpers for the CRM's pages.

Split out of the 1,900-line crm/views.py (2026-08-05) so the Today engine
(crm/today.py) and the contact/calendar pages can stop sharing one module to
share six functions. Nothing here touches a request; everything is safe to
import from anywhere in the app without creating a cycle.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote, urlencode

from django.utils import timezone


# ---------------------------------------------------------------------------
# Time.
# ---------------------------------------------------------------------------
def _calendar_days_ago(ts, *, as_of=None) -> int:
    """How many days ago `ts` happened, as a CALENDAR-date difference in the
    active timezone (`localtime(as_of).date() - localtime(ts).date()`) — not
    `(as_of - ts).days`, a raw timedelta floor that is timezone-independent
    and effectively `elapsed_hours // 24`.

    The single source of truth for this fact: `crm.debrief.pending`,
    `crm.today._schedule`, and `crm.views._contact_card` each computed it
    independently, and the raw-floor version drifted from this one once
    elapsed time crossed a local calendar-date boundary — e.g. Touch 558,
    ~58.46h elapsed under Asia/Hong_Kong: floor gives 2, calendar-diff gives
    3, and two surfaces showing the same touch disagreed on "how long ago".
    """
    as_of = as_of or timezone.now()
    return (timezone.localtime(as_of).date() - timezone.localtime(ts).date()).days


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
#
# `manual_override` rides at the end on purpose: it is not a real interaction
# a student logs (log_touch rejects it — see TOUCH_TRANSITIONS), only the
# audit trail's own kind for a direct state correction (Park, or a fix from
# the student or their advisor). `bulk_received` is here for the opposite
# reason — log_touch ACCEPTS it, so leaving it out of the <select> is the
# only thing stopping "Bulk email received" from being something a student
# can claim happened; it is a verdict the capture pipeline reaches from
# message headers (capture.inbound), never a thing a person does. Both are
# skipped by `_contact_live.html`'s "Interaction" <select>; every OTHER
# reader of this list (Today's activity feed,
# the contact page's History, the advisor's own tool responses) wants a
# plain label here rather than falling back to "Manual override" — see
# crm.views._override_label for the richer, note-aware label History uses
# instead of this generic one.
TOUCH_KIND_LABELS: list[tuple[str, str]] = [
    ("outreach", "Reached out"),
    ("follow_up", "Followed up"),
    ("reply_received", "They replied"),
    ("bulk_received", "Bulk email received"),
    ("chat_scheduled", "Chat scheduled"),
    ("chat", "Chat happened"),
    ("thank_you", "Sent thank-you"),
    ("reping", "Re-pinged"),
    ("maintain", "Kept warm"),
    ("manual_override", "Updated manually"),
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
    """A Gmail compose URL with `to` (and optional subject/body) prefilled —
    composes start from Coverage so a contact's opener is one click away.

    WHY NOT `mailto:` (changed 2026-08-22): a `mailto:` hands off to whatever
    the OS has registered as the default mail client, which on a stock Mac is
    Apple Mail — an app most students here have never opened and never signed
    into. The click then either launches a blank unconfigured client or does
    nothing at all, and the draft is simply lost. Every user of this product
    has Gmail by construction (the whole capture engine reads it), so send
    them where their mail actually is. The draft opens in Gmail's own
    composer, they edit and send it there, and it lands in Sent — which the
    inbox scan already reads, so the outbound touch still gets recorded
    without this function pretending to know that a send happened.

    The `mailto:` name is kept for now because several call sites and their
    tests reference it; renaming is a mechanical follow-up, not a behaviour
    change, and doing it here would collide with concurrent edits to
    today.py/views.py.

    A `bcc` parameter used to live here too, pointed at the user's BCC
    capture address (docs/build-plan.md §5's v1) — retired 2026-08-19 now
    that Gmail Live reads sent mail directly, no BCC habit required.

    MULTI-ACCOUNT CAVEAT: this opens whichever Google account the browser has
    as its default session. A user signed into several will sometimes land in
    the wrong one. Fixing that needs the connected Gmail address threaded
    through to an `authuser` parameter, which means changing this signature
    and every call site — deliberately deferred rather than adding an unused
    keyword that looks reachable and isn't (see cadence.py's own note on why
    a dormant knob is worse than no knob).

    PRIVACY: `body` is addressed TO the contact, so only `Contact.opener` — the
    field that exists to be a draft email — may be passed here. `Contact.angle`
    must never be: it is the user's private note ABOUT the person ("USC alum,
    super responsive"), and it used to seed this body, which meant clicking
    Compose pre-filled an email to someone containing the user's assessment of
    them. Pinned by test_angle_never_leaks_into_mailto."""
    params: list[tuple[str, str]] = [("view", "cm"), ("fs", "1")]
    if to_email:
        params.append(("to", to_email))
    if subject:
        params.append(("su", subject))
    if body:
        params.append(("body", body))
    return "https://mail.google.com/mail/?" + urlencode(params, quote_via=quote)


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


