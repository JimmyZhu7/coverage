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

# Cross-app read, same posture as `crm.today`'s own import of
# `directory.open_runs` — directory never imports this module, so there is
# no cycle. See `FIRM_DATE_LABELS` below for why.
from directory.timeline import EVENT_LABELS as _FIRM_DATE_EVENT_LABELS


# ---------------------------------------------------------------------------
# Time.
# ---------------------------------------------------------------------------
def _calendar_days_ago(ts, *, as_of=None, as_of_date=None) -> int:
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

    `as_of_date` is that same "today", already converted, for callers running
    this per row — the caller-supplies-the-batch posture `directory.views.
    _urgency_item` already holds for `cutoffs`, and for the same reason: the
    left-hand side of this subtraction is one fact for the whole request, and
    re-deriving it per row is one `timezone.localtime` call per row that
    answers a question already answered. Measured 2026-09-01: `localtime` is
    1.7 µs and this function 3.0, so the hoist is 55% of the call, over
    32,000 calls on a `?role=all` feed render. The arithmetic does NOT move —
    a caller passing `as_of_date` is skipping one conversion, not spelling
    the day out for itself.
    """
    if as_of_date is None:
        as_of_date = local_date(as_of or timezone.now()).date()
    return (as_of_date - local_date(ts).date()).days


def local_date(ts):
    """`ts` re-expressed on the account's own clock. Same instant, one zone.

    THE ONE CONVERSION every calendar-day fact about a stored timestamp has to
    go through. `settings.TIME_ZONE` is UTC and a user's real zone is activated
    per request by `accounts.middleware.TimezoneMiddleware`, so a raw
    `Touch.ts.date()` answers a question nobody asked: which day it was in UTC.
    This product has already settled that "today" means the user's today (see
    `accounts.tests.test_timezone`), and this is the single call that enforces
    it.

    Returned as a datetime, not a date, so callers that need the day take
    `.date()` and callers handing the value to a domain engine (which does its
    own `.date()`) pass it straight through. Both then read the same day, which
    is the whole point — see `_touch_dicts`.
    """
    return timezone.localtime(ts)


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
    never spuriously triggers a re-ping.

    ROUNDED TO 2dp, NOT 1. At one decimal place the mapping did the opposite
    of what the sentence above promises, at the one end where it matters:
    `round(0.99, 1)` is 1.0, so anything from 0.96 up came back
    "confirmed_official". The column's own CheckConstraint permits 0.99 —
    it bounds the range, not the band — and `FirmDateAdmin` and a
    `manage.py shell` write are both unbounded within it, which is the same
    class of path the `confidence=95.0` incident arrived through.
    "confirmed_official" is the label `cadence._closing_soon` and the
    engine's re-ping branch act on, so a date NOBODY confirmed would have
    fired a pre-deadline re-ping and printed a countdown, laundered into
    certainty by a rounding step. Two places is still tolerant of any float
    round-tripping (the three real values are written from exact literals
    0.3 / 0.6 / 1.0 and come back exact) while it no longer reaches across a
    band boundary."""
    if isinstance(value, str):
        return value
    try:
        return _CONFIDENCE_LABELS.get(round(float(value), 2), "reported")
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
    """Shape Touch rows into the plain dicts the domain engines read.

    `ts` IS LOCALIZED HERE, at the one boundary every domain engine's touch
    timestamps cross, rather than at any single call site.

    THE BUG THIS CLOSES, measured on the founder's live account 2026-08-31.
    `cadence.due_actions` derives its whole calendar from the zone its inputs
    arrive in: `today = as_of.date()`, and each touch's day from
    `_as_date(t["ts"])`. `crm.today._build_actions` was already handing it a
    LOCAL `as_of` (`timezone.localtime(timezone.now())`, and its comment
    explains why) while these dicts still carried UTC. His account is
    America/Los_Angeles; Youqi Chen's `chat_scheduled` touch is stored
    2026-08-24 01:37Z, which is 2026-08-23 18:37 where he lives. The engine
    read Aug 24 and the card's own ledger line read Aug 23, so ONE card, in
    ONE render, said "5 business days" in its sentence and "6 business days
    ago" in the row beneath it. Six is the right answer: the product has a
    `TimezoneMiddleware` and a test file establishing that "today" means the
    user's today.

    A per-call-site patch would have fixed the one card and left the next
    caller to rediscover the same skew, so the conversion lives at the funnel:
    everything downstream of here is on one clock by construction, and the
    ledger line reads its day through `local_date` too.

    Harmless for the other consumer. `coverage_domain.scoring` measures in
    continuous elapsed time (`_days_between` is a float timedelta), which is
    zone-independent, so `crm.views.contact_detail`'s two calls are unaffected
    either way.
    """
    return [
        {
            "contact_id": t.contact_id,
            "ts": local_date(t.ts),
            "kind": t.kind,
            "note": t.note,
        }
        for t in touches
    ]



def _clock(at) -> str:
    """A time short enough for a rail row: "9am", "12:30pm"."""
    minutes = f":{at.minute:02d}" if at.minute else ""
    return f"{at.strftime('%I').lstrip('0') or '12'}{minutes}{at.strftime('%p').lower()}"



def _sentence_case(label: str) -> str:
    """Title Case -> sentence case: only the first word keeps its capital.
    An already-uppercase word (an acronym, none of which occur in the firm-
    dates vocabulary today) survives unchanged rather than being lowercased
    into something unpronounceable."""
    words = label.split(" ")
    return " ".join(
        w if i == 0 or w.isupper() else w.lower() for i, w in enumerate(words)
    )


# The CRM's own casing of `directory.timeline.EVENT_LABELS`, not a second
# hand-typed vocabulary. Until 2026-09-01 this dict was maintained by hand
# and had drifted from the canonical one in two ways:
#
#   1. It covered 4 of the 8 `event_kind`s. The other 4 fell through five
#      CRM call sites' own `event_kind.replace("_", " ")` fallback -- an
#      `app_deadline` row would have rendered "app deadline".
#   2. The one kind both maps DID cover under the same key still disagreed:
#      this dict said `"insight_deadline": "Insight deadline"` -- the exact
#      sentence-cased-raw-slug string `directory.timeline.EVENT_LABELS`'s own
#      docstring records fixing, for the identical key, to "Insight
#      Programme Deadline". Live effect: Morgan Stanley (id 32)'s
#      `insight_deadline` row read "Insight deadline" on Today's rail and the
#      reasoning-panel label, while the firm timeline three clicks away
#      correctly said "Insight Programme Deadline" for the same row -- the
#      same defect class `directory.timeline` was written to end,
#      reintroduced by keeping a second, independently-maintained map of the
#      same vocabulary.
#
# CASING IS DELIBERATELY NOT THE SAME AS THE SOURCE MAP. The firm timeline
# (`directory/views.py`'s `_firm_date_row`) renders `EVENT_LABELS` Title
# Case in a table column, next to other Title Case headers. Every CRM call
# site prints this map's value as a short pill or inline phrase beside
# `crm.utils.ACTION_LABELS` ("Send thank-you", "Keep warm", "Follow up") --
# sentence case, one capital, on the exact same card
# (`templates/crm/_cockpit.html`'s `p.firm_date_label` sits beside
# `a.label`). `crm/views.py`'s own comment on the old map already called it
# "the crm surface's own lowercase vocabulary... so the three [Today strip,
# calendar, reasoning panel] cannot drift" -- true and worth keeping, so the
# casing is derived here rather than dropped, and the SOURCE of the words
# is now the one map instead of two.
FIRM_DATE_LABELS = {
    kind: _sentence_case(label) for kind, label in _FIRM_DATE_EVENT_LABELS.items()
}


# ---------------------------------------------------------------------------
# What "confirmed" means for a firm date. ONE definition, five call sites.
# ---------------------------------------------------------------------------
# `directory.views._firm_date_row` — the firm timeline, the page that shows
# these rows with their provenance attached — has always required TWO things
# of a date before calling it confirmed rather than rumoured:
#
#     confidence >= 0.8  AND  precision in ("day", "month", "")
#
# Every one of the CRM's own confirmed-date readers re-spelled that bar as
# `confidence=1.0` alone and dropped the second half: Today's deadlines rail
# (`crm.today._next_deadlines`), the Network board's own Coverage-Gaps
# exposure input (`crm.views.contact_list`), the chat-prep card
# (`_chat_prep`), and the calendar's layer 3 plus its .ics feed. (Today's own
# coverage-gap lane, `crm.today._coverage_cards`, was a sixth call site until
# it was retired 2026-08-31.) The two
# halves are not redundant: `confidence` says how sure we are the firm holds
# this date, `precision` says how exactly the stored day locates it.
# `precision="estimated"` means the date is a MONTH-level guess, which the
# timeline renders honestly as "~ Sep 2027" — and which the CRM would render
# as a hard "3d" countdown, a deadline-coloured cell on one specific square
# of the grid, and a VALARM waking a phone a week before a day nobody stated.
#
# No live row currently pairs 1.0 with "estimated" (the 25 estimated rows all
# sit at 0.6), but nothing stops one: `import_firm_dates` reads `confidence`
# and `precision` from two INDEPENDENT keys of the same YAML entry, so a
# single seed line saying `confidence: confirmed_official` / `precision:
# estimated` produces it. Excluding it here costs nothing today and closes
# the gap before a row walks through it.
CONFIRMED_CONFIDENCE = 1.0
# Precisions a confirmed date may carry. "" and "day" locate a real day;
# "month" is shown as a month by every renderer that reads the field.
# "estimated" is deliberately absent — see above.
CONFIRMED_PRECISIONS = ("", "day", "month")


def firm_date_confidence(fd) -> str:
    """One stored `FirmDate` row's confidence label for the DOMAIN engines
    (`coverage_domain.cadence`, `scoring.score_firm`), which only ever act on
    "confirmed_official".

    The same two-part bar as `confirmed_firm_dates` above, applied to a row
    rather than a queryset: a date whose own `precision` says it is a
    month-level estimate is not `confirmed_official` however high its
    confidence reads, so it cannot fire `cadence._closing_soon`'s
    pre-deadline re-ping or move `score_firm`'s timeline-readiness axis. It
    degrades to "reported" — the same landing place `_confidence_label`
    already uses for everything it does not recognise — rather than being
    dropped, because the row is still a real fact about the firm; it just is
    not one anything may set a countdown by.
    """
    label = _confidence_label(fd.confidence)
    if (fd.precision or "") not in CONFIRMED_PRECISIONS:
        return "reported"
    return label


def confirmed_firm_dates():
    """`FirmDate` rows the CRM is allowed to put a countdown on.

    A queryset, not a list: every caller narrows it further (by date window,
    by firm) and none of them wants the whole table.
    """
    from directory.models import FirmDate  # local: avoids an app-import cycle

    return FirmDate.objects.filter(
        confidence=CONFIRMED_CONFIDENCE,
        precision__in=CONFIRMED_PRECISIONS,
    )


