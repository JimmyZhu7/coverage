"""CRM UI views (docs/build-plan.md §4 weekly list, §5 mailto-BCC compose,
§6 fit score). Mounted at /app/ (see coverage_web/urls.py); every view is
login-required and scopes every private-zone read with
`.for_user(request.user)` (see coverage_web/tenancy.py).

The domain engines are PURE (they read no DB, no wall clock): this layer
fetches the user's rows, shapes them into the plain dicts the engines want,
calls them with an explicit `as_of=timezone.now()`, and renders the result.
State-mutating writes go through `crm.services` (the reviewed pipeline
adapter) only — never a hand-rolled UPDATE.
"""

from __future__ import annotations

from datetime import timedelta
from math import ceil
from typing import Any
from urllib.parse import quote, urlencode

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Count as models_Count, Max as models_Max, Q
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.timesince import timesince
from django.views.decorators.http import require_POST

from analytics.events import record_event
from analytics.models import UserOpportunity
from coverage_domain import cadence, scoring
from coverage_domain.pipeline import CHANNELS, MANUAL_OVERRIDE_KIND, TOUCH_TRANSITIONS
from crm.forms import ChatDebriefForm, ContactForm
from directory.classify import TARGET_BUCKETS
from directory.models import Firm, FirmDate, Opportunity

from . import coverage, debrief as debrief_svc, services
from .models import CalendarEvent, ChatDebrief, Contact, Touch, UserFirm

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


def _capture_address(user) -> str:
    """The user's per-user inbound capture address, `u-<slug>@<domain>`
    (docs/build-plan.md §5). Domain is settings-driven with the plan's
    default so this works before the capture app ships its own config."""
    domain = getattr(settings, "CAPTURE_INBOUND_DOMAIN", "in.coverage.app")
    return f"u-{user.capture_slug}@{domain}"


def _mailto(to_email: str, bcc: str, *, subject: str = "", body: str = "") -> str:
    """A `mailto:` URL with `to` + `bcc` (and optional subject/body) prefilled
    — the v1 capture surface (§5): composes start from Coverage so outreach is
    BCC'd to the capture address without any Gmail API access. `quote_via=quote`
    keeps spaces as %20 and the `@` as %40, which every mail client accepts.

    PRIVACY: `body` is addressed TO the contact, so only `Contact.opener` — the
    field that exists to be a draft email — may be passed here. `Contact.angle`
    must never be: it is the user's private note ABOUT the person ("USC alum,
    super responsive"), and it used to seed this body, which meant clicking
    Compose pre-filled an email to someone containing the user's assessment of
    them. Pinned by test_angle_never_leaks_into_mailto."""
    params: list[tuple[str, str]] = []
    if bcc:
        params.append(("bcc", bcc))
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


# ---------------------------------------------------------------------------
# 1. Weekly priority list — the authed hub at /app/.
# ---------------------------------------------------------------------------
# The ONLY cadence rule parameters a user may override, each with the range it
# has to stay inside. Everything else in coverage_domain.cadence.CADENCE_DEFAULTS
# stays a product constant.
#
# This is a whitelist, not a blocklist, and it is enforced here rather than at
# write time because `User.cadence_params` is a JSONField: it can be populated
# by a form, a fixture, a shell, or a future import path, and only the read
# side is guaranteed to run for every request. An unknown key or an
# out-of-range value is DROPPED, never passed through — the engine would
# otherwise happily accept e.g. max_cold_touches: 10000 (a contact that is
# never parked) or a negative window (a follow-up due forever).
TUNABLE_CADENCE_PARAMS: dict[str, tuple[int, int]] = {
    "followup_after_business_days": (1, 30),
    "park_after_business_days": (1, 120),
    # Capped at 2, not left open — this is what enforces "never a second
    # follow-up" as a structural fact rather than a default someone could
    # raise. cadence.due_actions' branch 6 sends exactly one outreach note and
    # one follow-up; `outbound >= max_cold_touches` is what routes a contact to
    # `park` instead of a further follow-up. A cap of 3+ would let that branch
    # fire a second follow-up on a longer wait — the staged-window behavior
    # tried and reverted on 2026-07-28 (see cadence.py's DIVERGENCE note) — so
    # the range itself, not just the default, has to stay at (1, 2).
    "max_cold_touches": (1, 2),
    "advocate_touch_min_weeks": (1, 52),
    # The keep-warm clock for someone you have actually met but who is not yet
    # an advocate. Same range as the advocate clock because it is the same kind
    # of judgement — how long is too long to go quiet on a warm contact — and a
    # student who wants one tuned usually wants both.
    #
    # PAIRED WITH accounts.forms.CADENCE_LABELS: that form iterates this dict
    # and does a hard label lookup, so a key here without an entry there is an
    # immediate 500 on the Settings page. Add and remove them together.
    "chatted_touch_min_weeks": (1, 52),
    "pre_deadline_reping_days": (1, 90),
}


def _cadence_params(user) -> dict[str, int]:
    """The user's validated cadence overrides — safe to hand to
    `cadence.due_actions(params=...)`. Silently drops anything that isn't a
    whitelisted key holding an in-range integer (`bool` is excluded on purpose:
    it's an int subclass in Python, and `True` is not a sane window length)."""
    raw = getattr(user, "cadence_params", None)
    if not isinstance(raw, dict):
        return {}
    out: dict[str, int] = {}
    for key, (low, high) in TUNABLE_CADENCE_PARAMS.items():
        value = raw.get(key)
        if isinstance(value, bool) or not isinstance(value, int):
            continue
        if low <= value <= high:
            out[key] = value
    return out


# Cadence action kinds that a Snooze/Skip must NOT be able to hide.
#
# Snoozing used to be implemented by dropping the contact from the engine's
# INPUT, which silenced every action they could produce for the whole snooze
# window — including a priority-0 pre-deadline re-ping that fires two weeks
# before a confirmed close, the highest-value nudge the engine has. Snoozing
# one nagging follow-up card is not consent to miss a deadline. The filter now
# runs over the OUTPUT (below), and these two kinds are exempt from it.
_SNOOZE_EXEMPT_ACTIONS = frozenset({"reping", "confirm_chat"})


def _build_actions(user):
    """The cadence queue, shared by Today and Network: fetch the user's
    contacts/touches/tiers/firm-dates, run `cadence.due_actions`, and dress
    each action for display (label, prose reason, warmth, compose link,
    last-touch evidence, deadline chip).
    Returns (actions, contacts, capture_address)."""
    now = timezone.now()
    today = timezone.localdate()
    # Deliberately NOT filtered on `snoozed_until` — see _SNOOZE_EXEMPT_ACTIONS.
    # The engine sees every live contact; the snooze is applied to the actions
    # it produces, so a snooze can hide a nag without hiding a deadline.
    contacts = list(Contact.objects.for_user(user).filter(archived=False))
    touches = list(Touch.objects.for_user(user))
    snoozed_ids = {
        c.id for c in contacts if c.snoozed_until and c.snoozed_until > now
    }

    # Firm metadata: names from the directory, tiers from the user's UserFirm
    # rows. cadence falls back to firm_text / a default tier when a contact's
    # firm isn't covered, so this only needs to be best-effort.
    firm_ids = {c.firm_id for c in contacts if c.firm_id}
    tiers = {
        uf.firm_id: uf.tier
        for uf in UserFirm.objects.for_user(user)
        if uf.firm_id
    }
    firm_names = dict(
        Firm.objects.filter(id__in=firm_ids).values_list("id", "name")
    )
    firm_meta = {
        fid: {"name": firm_names.get(fid, fid), "tier": tiers.get(fid, 3)}
        for fid in firm_ids
    }

    firm_dates = [
        {
            "firm_id": fd.firm_id,
            "event_kind": fd.event_kind,
            "region": fd.region,
            "date": fd.date,
            "confidence": _confidence_label(fd.confidence),
        }
        for fd in FirmDate.objects.filter(firm_id__in=firm_ids)
    ]

    # cadence returns action["contact"] as the exact dict we pass in, so we
    # hand it dicts already carrying the display fields the template needs.
    # `angle` is deliberately NOT in here: it is the user's private note about
    # the person, the compose body now comes from `opener`, and nothing else in
    # the queue reads it. Keeping it out means it can't leak into a draft again.
    contact_dicts = [
        {
            "id": c.id,
            "name": c.name,
            "email": c.email,
            "firm_id": c.firm_id,
            "firm_text": c.firm_text,
            "warmth": c.warmth,
            "thread_state": c.thread_state,
            "region": c.region,
            "source": c.source,
            "opener": c.opener,
            "archived": c.archived,
            # Display only. The card needs to know whether the name in the
            # firm slot is an employer or a university: a hand-added contact
            # has no `firm_id` whether the free text says "USC" or "HSBC", so
            # a missing firm_id is NOT evidence of a school and labelling on
            # it called eight HSBC bankers alumni.
            "school_affiliation": c.school_affiliation,
            "school": c.school,
        }
        for c in contacts
    ]

    params = _cadence_params(user)
    actions = cadence.due_actions(
        contact_dicts,
        _touch_dicts(touches),
        firm_dates,
        as_of=now,
        firms=firm_meta,
        params=params,
    )
    # E8: the snooze is a filter on the ACTION list, not on the engine's input,
    # and it cannot touch the two kinds that carry a real deadline behind them.
    actions = [
        a for a in actions
        if a["contact"]["id"] not in snoozed_ids
        or a["action"] in _SNOOZE_EXEMPT_ACTIONS
    ]

    # The card's evidence line: the latest REAL touch per contact. Same
    # definition the engine's idle clocks use (cadence's C2 divergence) —
    # a `manual_override` audit row is the system writing to itself, so
    # showing it as "Last: ..." would claim a contact was touched when the
    # only thing that happened was a state correction.
    kind_labels = dict(TOUCH_KIND_LABELS)
    last_real: dict[int, Touch] = {}
    for t in touches:
        if t.kind == MANUAL_OVERRIDE_KIND:
            continue
        prev = last_real.get(t.contact_id)
        if prev is None or t.ts > prev.ts:
            last_real[t.contact_id] = t

    # Deadline chips reuse the engine's own closing-soon index rather than
    # re-deriving one: same confirmed-only bar, same region scoping, same
    # window, so a chip can never disagree with the re-ping that produced it.
    merged = {**cadence.CADENCE_DEFAULTS, **params}
    reping_days = int(merged["pre_deadline_reping_days"])
    closing = cadence._closing_soon(firm_dates, today, reping_days)

    capture_addr = _capture_address(user)
    for a in actions:
        c = a["contact"]
        a["label"] = ACTION_LABELS.get(a["action"], a["action"])
        a["reason"] = _sentenceize(a.get("reason", ""))
        a["warmth_pct"] = _warmth_pct(c.get("warmth", "cold"))
        # Compose surface: the opener seeds the draft body so the weekly list
        # doubles as the place outreach starts (§5).
        a["mailto"] = _mailto(
            c.get("email", ""),
            capture_addr,
            body=(c.get("opener") or ""),
        )
        # A blank opener means Compose opens an EMPTY email. The card says so
        # rather than letting the click discover it (D).
        a["has_draft"] = bool((c.get("opener") or "").strip())

        last = last_real.get(c["id"])
        a["last_kind"] = kind_labels.get(last.kind, last.kind) if last else None
        a["last_business_days"] = (
            cadence.business_days_since(timezone.localtime(last.ts).date(), today)
            if last else None
        )
        # Drives the "longest silent first" term of the Today sort key. No
        # dateable touch sorts as maximally silent, which is what it is.
        a["idle_business_days"] = (
            a["last_business_days"] if a["last_business_days"] is not None else 10 ** 6
        )

        by_region = closing.get(c.get("firm_id"))
        close = None
        if by_region:
            region = cadence.contact_region(c)
            # Same unknown-region fallback as engine branch 3: match the
            # soonest close across any region rather than guessing one.
            close = min(by_region.values()) if region is None else by_region.get(region)
        a["closes_on"] = close

    return actions, contacts, capture_addr


# Quick-action "Sent" → the touch kind it logs, per cadence action.
# "park" is deliberately ABSENT: it has no "Sent" quick-action at all (see
# today_act's dedicated 'park' verb below) — it doesn't route through
# log_touch, so it needs no touch kind here. It used to map to "maintain",
# which meant clicking "Park it" logged a fabricated "Kept warm" touch and
# left thread_state untouched, so a parked contact kept reappearing in the
# queue with the same nag forever. Parking is a state change (thread_state
# -> 'parked'), not an interaction, and now goes through
# services.set_contact_state instead.
_ACTION_TOUCH: dict[str, str] = {
    "first_outreach": "outreach",
    "follow_up": "follow_up",
    "thank_you": "thank_you",
    "reping": "reping",
    "maintain": "maintain",
    # branch 5b logs an EXISTING kind: TOUCH_TRANSITIONS["maintain"] is
    # (None, None), so a keep-warm note advances no state and needs no
    # pipeline change. The ratchet stays untouched by this feature.
    "keep_warm": "maintain",
    "advance": "outreach",
    # "confirm_chat" is deliberately ABSENT. It used to map to "chat", which
    # meant one click on "Sent" asserted that a conversation had HAPPENED —
    # the single largest claim any button on this page can make, on a card
    # whose whole premise is that we don't know whether it happened. It is a
    # two-step now (see _act_card.html); nothing here logs it in one click.
}

# Weekly pace target — touches logged Monday-to-now. The product default, used
# when the user hasn't set their own `weekly_touch_goal`.
WEEKLY_TOUCH_GOAL = 10

# Touch kinds that are somebody ELSE's action, not the user's work.
#
# `chat_scheduled` is in here, and that is the one genuinely arguable call.
# It reads like user work ("I booked a chat"), but in THIS system it is not:
# `capture/gmail.py` writes it when it classifies a RECEIVED message that
# proposes or confirms a time, and `pipeline.TOUCH_TRANSITIONS` ratchets it to
# warmth "replied" — the same rung as `reply_received` — because it is a
# reply. Measured on the founder's live week, all six `chat_scheduled` rows
# were capture-written off inbound mail ("Amy offered...", "Hannah replied
# proposing..."). Counting them would have the ring reading 6/14 for a week in
# which he sent nothing, which is the exact over-claim this set exists to stop.
_INBOUND_TOUCH_KINDS = frozenset({"reply_received", "chat_scheduled"})

# What the pace ring's numerator counts: work the USER did.
#
# Derived from the ratchet's own vocabulary rather than hand-listed, so a touch
# kind added to `pipeline.TOUCH_TRANSITIONS` later counts as work by default
# and has to be named inbound on purpose to be excluded. `manual_override` is
# excluded structurally and for free: pipeline deliberately keeps its audit
# kind OUT of TOUCH_TRANSITIONS.
#
# Related but NOT the same set as `scoring._OUTBOUND_KINDS` / `cadence.
# _OUTBOUND_KINDS`, and the difference is deliberate. Those two answer "what
# is a send a reply is owed against?", so they exclude the courtesy kinds
# (`thank_you`, `maintain`) nobody is expected to answer. A pace ring asks a
# different question — "what did you do this week?" — and a thank-you note or
# a keep-warm update is unambiguously work you did. `chat` counts for the same
# reason: showing up to a conversation is the most expensive thing on the list.
PACE_TOUCH_KINDS = frozenset(TOUCH_TRANSITIONS) - _INBOUND_TOUCH_KINDS

# Today's plan sizing. Both are reasoned, not measured — revisit against the
# founder's actual clear-rate after a couple of weeks of dogfood.
TODAY_PLAN_MIN = 3    # never plan fewer: below this the page stops building momentum
TODAY_PLAN_MAX = 12   # never plan more: ~6 hours a week is the real ceiling

# Display class per cadence action, lower shown first. This is the Today
# page's ordering, and it lives HERE rather than in the engine on purpose:
# `cadence.due_actions`' `(priority, tier, firm_name)` sort is ported code
# with golden fixtures behind it, and it answers a different question ("what
# does the cadence consider urgent?") than this page does ("who do I contact
# right now?").
#
# The inversion that matters is momentum over tier. Six of eight action kinds
# share cadence priority 1, so the engine's effective sort was firm alphabet:
# measured on the founder's queue, 29 cold non-repliers at Citi/Goldman/HSBC
# occupied positions 1-29 and every warm contact sat below the fold, because
# the warm ones happen to be at tier-3 and unranked firms. A person who
# replied outranks a person who ignored you, whatever the letterhead.
_TODAY_CLASS: dict[str, int] = {
    "reping": 0, "confirm_chat": 0,        # time-critical: a real clock behind them
    "thank_you": 1, "advance": 1,          # momentum: they gave you something
    "keep_warm": 2, "maintain": 2,         # warm upkeep
    "first_outreach": 3, "follow_up": 3,   # cold
    "park": 4,                             # bulk strip, never a plan slot
}
_TODAY_CLASS_DEFAULT = 3

# class -> the lane it renders in. Semantic (what KIND of work this is), not
# an echo of the priority number: the old lanes mapped priority 0/1/2+ to
# Overdue/Due Now/Keep Warm, and since six kinds share priority 1 the live
# page showed one undifferentiated "Due Now" lane of 36 and nothing else.
_TODAY_LANES = [
    ("critical", "Don't lose these"),
    ("momentum", "Move it forward"),
    ("cold", "Cold follow-ups"),
]


def _today_class(a: dict) -> int:
    return _TODAY_CLASS.get(a["action"], _TODAY_CLASS_DEFAULT)


def _is_critical(a: dict) -> bool:
    """Never capped, never snoozed away: a confirmed deadline, a dying chat
    thread, or anything the engine itself called priority 0 (which is how an
    OVERDUE thank-you gets in here without needing its own class)."""
    return _today_class(a) == 0 or a["priority"] == 0


def _today_sort_key(a: dict):
    """(class, cadence priority, tier, longest-silent first, firm name).

    Tier still breaks ties INSIDE a class — it just no longer outranks the
    relationship. Firm name is last and exists only to make the order stable
    across renders."""
    return (
        _today_class(a),
        a["priority"],
        a["tier"],
        -a.get("idle_business_days", 0),
        str(a["firm_name"]),
    )


def _workdays_left(today) -> int:
    """Mon-Fri days from `today` through the end of this week, minimum 1.

    Minimum 1 rather than 0 so a Saturday plan is "everything left, today"
    instead of a division by zero — and so the weekend isn't quietly treated
    as extra capacity that never existed."""
    return max(1, 5 - today.weekday()) if today.weekday() < 5 else 1


def _daily_cap(goal: int, done: int, today) -> int:
    """How many actions today's plan may hold.

    Derived from the EXISTING `weekly_touch_goal` rather than a new setting:
    a second capacity knob would drift out of sync with the ring the moment
    either was tuned, and there'd be no honest way to say which one the page
    meant. Behind on a Friday, the cap climbs; ahead on a Monday, it drops to
    the floor. `done` is the same corrected numerator the ring renders, so the
    plan and the ring can never disagree about what you've done."""
    remaining = max(0, goal - done)
    return max(TODAY_PLAN_MIN, min(TODAY_PLAN_MAX, ceil(remaining / _workdays_left(today))))


def _pace(user, today) -> dict:
    """The weekly pace ring: touches YOU logged since Monday, against the goal.

    The numerator used to be every touch of any kind. Measured on the founder's
    live data it read 9/14 in a week he had sent nothing at all: 6
    `chat_scheduled` + 2 `reply_received` (other people's actions, written by
    the capture pipeline off inbound mail) + 1 `chat`. A progress meter that
    fills while you do nothing is the same class of over-claim as a "New" badge
    that means "we imported it" — the goal was always honest, the numerator
    never was."""
    week_start = today - timedelta(days=today.weekday())
    done = (
        Touch.objects.for_user(user)
        .filter(ts__date__gte=week_start, kind__in=PACE_TOUCH_KINDS)
        .count()
    )
    # `or` (not a None check) on purpose: a stored 0 is not a goal of zero —
    # a zero-touch week target would make the ring meaningless and divide by
    # zero below — so it falls back to the product default like NULL does.
    goal = getattr(user, "weekly_touch_goal", None) or WEEKLY_TOUCH_GOAL
    return {
        "done": done,
        "goal": goal,
        "pct": min(100, round(done / goal * 100)) if goal else 0,
        "remaining": max(0, goal - done),
        "hit": done >= goal,
    }


def _clock(at) -> str:
    """A time short enough for a rail row: "9am", "12:30pm"."""
    minutes = f":{at.minute:02d}" if at.minute else ""
    return f"{at.strftime('%I').lstrip('0') or '12'}{minutes}{at.strftime('%p').lower()}"


_SCHEDULE_HORIZON_DAYS = 14


def _schedule(user, today) -> list[dict]:
    """What is actually coming, in time order — the page's missing clock.

    Two sources, deliberately merged rather than shown as two cards:

    1. `CalendarEvent` — chats and events with a REAL datetime, whether the
       Gmail sync found them on an invite or the user typed them in.
    2. Contacts sitting at `thread_state="chat_scheduled"` for which no event
       exists. This used to be the whole of "Coming Up", and its docstring
       said the copy could never state when a chat was "because we do not
       store a chat datetime anywhere". That stopped being true when
       CalendarEvent landed — but only for chats whose time somebody knows,
       so these rows survive, saying honestly that no time is set yet.

    A merged list is the point: "what's next" is one question, and answering
    it across two cards makes the reader do the interleaving.
    """
    now = timezone.localtime(timezone.now())
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    horizon = start + timedelta(days=_SCHEDULE_HORIZON_DAYS)

    rows: list[dict] = []
    seen_contacts: set[int] = set()

    for ev in (CalendarEvent.objects.for_user(user)
               .filter(starts_at__gte=start, starts_at__lt=horizon)
               .select_related("contact")
               .order_by("starts_at")):
        at = timezone.localtime(ev.starts_at)
        day = at.date()
        offset = (day - today).days
        if offset == 0:
            when = "today" if ev.all_day else f"{_clock(at)} today"
        elif offset == 1:
            when = "tomorrow" if ev.all_day else f"{_clock(at)} tmrw"
        else:
            label = day.strftime("%a")
            when = label if ev.all_day else f"{_clock(at)} {label}"
        if ev.contact_id:
            seen_contacts.add(ev.contact_id)
        rows.append({
            "sort": (at, 0),
            "title": ev.title,
            "contact": ev.contact,
            "when": when,
            "is_today": offset == 0,
            "timed": not ev.all_day,
            "at": at,
            "kind": ev.kind,
        })

    # Scheduled chats with no event on the books. Same 4-business-day gate the
    # old Coming Up used: it is the exact complement of cadence branch 2, which
    # stays silent that long because there is nothing to chase yet.
    untimed = (
        Contact.objects.for_user(user)
        .filter(archived=False, thread_state="chat_scheduled")
        .annotate(
            last_ts=models_Max("touches__ts", filter=~Q(touches__kind=MANUAL_OVERRIDE_KIND))
        )
    )
    for c in untimed:
        if not c.last_ts or c.id in seen_contacts:
            continue
        set_up = timezone.localtime(c.last_ts).date()
        if cadence.business_days_since(set_up, today) > 4:
            continue
        days_ago = (today - set_up).days
        rows.append({
            # Sorts after every timed row on the same day: a thing with a
            # known time outranks a thing without one.
            "sort": (now.replace(hour=23, minute=59), 1),
            "title": f"{c.name} · chat set up",
            "contact": c,
            "when": "no time yet",
            "is_today": False,
            "timed": False,
            "at": None,
            "kind": "chat",
            "days_ago": days_ago,
        })

    rows.sort(key=lambda r: r["sort"])
    return rows[:6]


def _next_deadlines(user, today, limit=4) -> list[dict]:
    """The next confirmed firm dates, NAMED.

    The ribbon at the foot of the page already counts these ("3 closing in 10
    days"). A count creates a click; a name creates an action — "Morgan
    Stanley insight deadline, 2 days" is a thing you can do something about
    this morning.

    `confidence=1.0` only, the same bar the cadence engine acts on. A calendar
    countdown built on a rumour is worse than no countdown.
    """
    rows = (FirmDate.objects
            .filter(date__gte=today, confidence=1.0)
            .select_related("firm")
            .order_by("date")[:limit])
    out = []
    for fd in rows:
        days = (fd.date - today).days
        out.append({
            "firm": fd.firm,
            "label": _FIRM_DATE_LABELS.get(
                fd.event_kind, fd.event_kind.replace("_", " ")),
            "date": fd.date,
            "days": days,
            "when": "today" if days == 0 else ("1d" if days == 1 else f"{days}d"),
            # Mirrors the cadence engine's own urgency bar, so the colour here
            # and the lane a contact lands in cannot disagree.
            "urgent": days <= 7,
        })
    return out


_FIRM_DATE_LABELS = {
    "app_open": "applications open",
    "app_close": "applications close",
    "insight_open": "insight programme opens",
    "insight_deadline": "insight deadline",
}


def _waiting_on_reply(user, busy_ids: set[int], limit=12) -> dict:
    """People you have written to who owe you an answer, and who the queue is
    deliberately silent about.

    The gap this fills: between "you sent it" and "the follow-up is due" the
    cadence engine says nothing — correctly, there is no action yet — so those
    contacts vanish from Today entirely. That silence reads as "did I drop
    something?", which is the anxiety a networking tool exists to remove. This
    is reassurance, not work: names only, no buttons, and it never occupies a
    plan slot.

    `busy_ids` is every contact the queue is already talking about, so nobody
    appears twice on one page.
    """
    rows = (
        Contact.objects.for_user(user)
        .filter(archived=False, thread_state="no_reply")
        .exclude(id__in=busy_ids)
        .annotate(
            last_ts=models_Max("touches__ts", filter=~Q(touches__kind=MANUAL_OVERRIDE_KIND))
        )
        .filter(last_ts__isnull=False)
        .order_by("-last_ts")
    )
    people = list(rows[:limit])
    total = rows.count()
    return {
        "people": people,
        "total": total,
        # Named, or counted honestly — never a truncated list passed off as
        # the whole set.
        "more": max(0, total - len(people)),
    }


def _chat_prep(user, today, schedule) -> list[dict]:
    """Chats happening TODAY, with what you knew last time already pulled up.

    A chat at 3pm is the most consequential thing on the page, and the work is
    not "remember it" — it is arriving with the last conversation in your
    head. Everything here already exists in the database; this is assembly,
    not new data: who they are, how warm, what the last debrief taught you,
    and whether their firm has a deadline worth raising.
    """
    out = []
    for row in schedule:
        if not (row["is_today"] and row["timed"] and row["contact"]):
            continue
        c = row["contact"]
        last = (ChatDebrief.objects.for_user(user)
                .filter(contact=c, dismissed=False)
                .exclude(learned="")
                .order_by("-created")
                .first())
        firm_date = None
        if c.firm_id:
            firm_date = (FirmDate.objects
                         .filter(firm_id=c.firm_id, date__gte=today, confidence=1.0)
                         .order_by("date")
                         .first())
        out.append({
            "contact": c,
            "at": row["at"],
            "when": row["when"],
            "title": row["title"],
            "learned": last.learned if last else "",
            "firm_date": firm_date,
            "firm_date_days": (firm_date.date - today).days if firm_date else None,
            "firm_date_label": (
                _FIRM_DATE_LABELS.get(firm_date.event_kind,
                                      firm_date.event_kind.replace("_", " "))
                if firm_date else ""
            ),
        })
    return out


def _cockpit_context(user) -> dict:
    """The Today cockpit: a capped, momentum-ordered daily plan in three
    semantic lanes, an honest held-back remainder, a weekly pace figure, the
    chats that are already on the calendar, and a recent-activity feed."""
    today = timezone.localdate()
    actions, contacts, _ = _build_actions(user)
    pace = _pace(user, today)
    cap = _daily_cap(pace["goal"], pace["done"], today)

    for a in actions:
        a["touch_kind"] = _ACTION_TOUCH.get(a["action"])

    ordered = sorted(actions, key=_today_sort_key)
    park = [a for a in ordered if _today_class(a) == 4]
    critical = [a for a in ordered if _today_class(a) != 4 and _is_critical(a)]
    rest = [a for a in ordered if _today_class(a) != 4 and not _is_critical(a)]

    # Fill rule: class 0 is always shown in full, even past the cap — a
    # confirmed deadline is never something the page decides you'll get to
    # tomorrow. Whatever slots remain fill from class 1 -> 2 -> 3 in sort
    # order, so the oldest-silent cold contacts drain FIFO across the week
    # instead of a 31-card batch landing whole.
    slots = max(0, cap - len(critical))
    planned = critical + rest[:slots]
    held = rest[slots:]

    planned_lanes = {key: [] for key, _ in _TODAY_LANES}
    for a in planned:
        planned_lanes["critical" if _is_critical(a) else
                      ("cold" if _today_class(a) == 3 else "momentum")].append(a)
    held_by_lane: dict[str, int] = {key: 0 for key, _ in _TODAY_LANES}
    for a in held:
        held_by_lane["cold" if _today_class(a) == 3 else "momentum"] += 1

    lanes = []
    for key, label in _TODAY_LANES:
        items = planned_lanes[key]
        if not items:
            continue
        total = len(items) + held_by_lane[key]
        lanes.append({
            "key": key,
            "label": label,
            "items": items,
            "count": len(items),
            "total": total,
            # E2: a capped lane never renders a bare number. It says "2 of 29
            # today" or it says nothing but its own count.
            "capped": total > len(items),
        })

    # E10: when one contact holds both a debrief and a thank-you, the two
    # cards stop pretending not to know about each other.
    debriefs = debrief_svc.pending(user)
    debrief_contact_ids = {d["contact"].id for d in debriefs}
    for a in planned + held:
        a["pairs_with_debrief"] = (
            a["action"] == "thank_you" and a["contact"]["id"] in debrief_contact_ids
        )

    # Activity feed: the last touches logged — what changed since last look.
    kind_labels = dict(TOUCH_KIND_LABELS)
    recent = Touch.objects.for_user(user).select_related("contact").order_by("-ts")[:8]
    now = timezone.now()
    activity = [
        {
            "name": t.contact.name,
            "contact_id": t.contact_id,
            "kind": t.kind,
            "kind_label": kind_labels.get(t.kind, t.kind.replace("_", " ").capitalize()),
            # depth=1: the default two units render "1 hour, 3 minutes", which
            # is noise in a glanceable feed. One unit is the whole signal.
            "ago": timesince(t.ts, now, depth=1),
            "inbound": t.kind in _INBOUND_TOUCH_KINDS,
        }
        for t in recent
    ]

    schedule = _schedule(user, today)

    # Every contact the queue is already speaking about. "Waiting on reply" is
    # the page's silent bucket, so it must not re-list somebody who has a card
    # six inches above it.
    busy_ids = {a["contact"]["id"] for a in planned + held + park}
    busy_ids |= debrief_contact_ids
    busy_ids |= {r["contact"].id for r in schedule if r["contact"]}

    return {
        "lanes": lanes,
        "planned_total": len(planned),
        "held": held,
        "held_total": len(held),
        "park_actions": park,
        "park_total": len(park),
        # >5 is where one-by-one parking stops being a decision and starts
        # being make-work. Below it the strip still renders; it just doesn't
        # offer a single button that changes state on a dozen people at once.
        "park_bulk": len(park) > 5,
        "daily_cap": cap,
        "queue_total": len(actions),
        # Chats from the last week that nobody has written down yet. Its own
        # lane rather than a cadence action: the cadence engine is pure and
        # knows nothing about ChatDebrief, and this prompt is about capturing
        # what already happened rather than about the next outbound move.
        "debriefs": debriefs,
        "pace": pace,
        # The timed layer. `schedule` merges real calendar datetimes with the
        # chats nobody has put a time on yet; `chat_prep` is the subset
        # happening today, with the last debrief pulled up alongside.
        "schedule": schedule,
        "chat_prep": _chat_prep(user, today, schedule),
        "deadlines": _next_deadlines(user, today),
        "waiting": _waiting_on_reply(user, busy_ids),
        "activity": activity,
        "contact_count": len(contacts),
    }


@login_required
def week(request: HttpRequest) -> HttpResponse:
    """Today: a capped daily plan in three semantic lanes, an honest "Up next"
    remainder, and a rail carrying the weekly pace ring, the chats already on
    the calendar, and recent activity. The commodity layer (directory stats)
    sits BELOW the queue — Today is the relationship page."""
    return render(
        request,
        "crm/week.html",
        {**_cockpit_context(request.user), **_dashboard_context(request.user)},
    )


@login_required
@require_POST
def today_park_all(request: HttpRequest) -> HttpResponse:
    """Park every contact currently in the queue's park strip, in one click.

    Written as a LOOP over `services.set_contact_state`, not a bulk
    `.update()`, and that is not an oversight. The audited override is the
    only thing allowed to move `thread_state`, and it writes one
    `manual_override` touch per contact so the log has no gap; a bulk UPDATE
    would change a dozen relationships with nothing on the record saying who
    did it or when. Slower, and correct.

    It re-derives the park list from the engine rather than trusting posted
    ids, so it can only ever park people the page was actually showing as
    parkable at the moment it was rendered."""
    actions, _, _ = _build_actions(request.user)
    park_ids = [a["contact"]["id"] for a in actions if a["action"] == "park"]
    for cid in park_ids:
        services.set_contact_state(
            request.user.id, cid,
            thread_state="parked", note="Parked from the Today queue (bulk)",
        )
    if park_ids:
        record_event("contacts_parked_bulk", user=request.user, source="today")
    return render(request, "crm/_cockpit.html", _cockpit_context(request.user))


@login_required
@require_POST
def today_act(request: HttpRequest, pk: int, verb: str) -> HttpResponse:
    """A Today-card quick action: log a touch you attest to having made
    ("Log it"), record that THEY replied, park a contact out of the cadence
    entirely, or snooze/skip it out of today's queue. Re-renders the whole
    cockpit so the queue, pace, and activity feed stay in sync.

    Compose is deliberately not in this list: a `mailto:` is not a send, so
    clicking it must never write a touch (E5). Only an explicit attestation
    does."""
    contact = get_object_or_404(Contact.objects.for_user(request.user), pk=pk)
    now = timezone.now()
    if verb == "sent":
        kind = (request.POST.get("kind") or "outreach").strip()
        if kind in TOUCH_TRANSITIONS:
            services.log_touch(request.user.id, contact.id, kind, "email", None)
            record_event("touch_logged", user=request.user, source="today")
    elif verb == "reply":
        services.log_touch(request.user.id, contact.id, "reply_received", "email", None)
        record_event("touch_logged", user=request.user, source="today")
    elif verb == "park":
        # A deliberate exit from the cadence, not an interaction: goes
        # through the manual-override path (audited touch, no fabricated
        # "Kept warm" entry) and actually changes thread_state so the
        # contact stops reappearing in the queue. See _ACTION_TOUCH's
        # comment for why this can't just be another "sent" kind.
        services.set_contact_state(
            request.user.id, contact.id,
            thread_state="parked", note="Parked from the Today queue",
        )
    elif verb == "snooze":
        Contact.objects.for_user(request.user).filter(pk=pk).update(
            snoozed_until=now + timedelta(days=3)
        )
    elif verb == "skip":
        Contact.objects.for_user(request.user).filter(pk=pk).update(
            snoozed_until=now + timedelta(days=1)
        )
    else:
        return HttpResponse(status=400)
    return render(request, "crm/_cockpit.html", _cockpit_context(request.user))


# ---------------------------------------------------------------------------
# 1b. Post-chat debrief — the structured capture of what a chat taught you.
# ---------------------------------------------------------------------------
@login_required
def debrief(request: HttpRequest, pk: int) -> HttpResponse:
    """The debrief form for one `chat` touch (`pk`), and the saved view of
    it afterwards. Scoped through `.for_user`, so another tenant's touch id
    404s exactly like a missing one.

    On save, `crm.debrief.record` does the bookkeeping (note append,
    referral contact, tasks) idempotently, then this view OFFERS the
    advocate promotion when the answer was "yes" — it never performs it.
    A warmth change is a claim about a relationship, and the user gets to
    make that claim on purpose (via `debrief_promote` below), not as a side
    effect of ticking a radio button."""
    touch = get_object_or_404(
        Touch.objects.for_user(request.user).select_related("contact"), pk=pk, kind="chat"
    )
    existing = ChatDebrief.objects.for_user(request.user).filter(touch=touch).first()

    if request.method == "POST":
        form = ChatDebriefForm(request.POST, instance=existing)
        if form.is_valid():
            saved, made = debrief_svc.record(
                request.user,
                touch,
                **{k: v for k, v in form.cleaned_data.items() if v not in (None, "")},
            )
            record_event("chat_debriefed", user=request.user)
            notes = []
            if made.get("intro_contact"):
                notes.append(f"added {made['intro_contact'].name}")
            if made.get("intro_task") or made.get("date_task"):
                n = bool(made.get("intro_task")) + bool(made.get("date_task"))
                notes.append(f"{n} task{'s' if n > 1 else ''} created")
            # `made` is empty exactly when `record` wrote nothing — an
            # unchanged resubmit. Saying "Debrief saved." there is a lie the
            # user can't check, and it was covering a real bug: the note
            # append used to be gated on `learned` being EMPTY, so every edit
            # to the text was silently discarded under this same green
            # banner. The gate is fixed (see crm.debrief.record); the message
            # now also only claims what happened.
            if made:
                messages.success(
                    request,
                    "Debrief saved" + (f" — {', '.join(notes)}." if notes else "."),
                )
            else:
                messages.info(request, "No changes to save.")
            return redirect("crm:debrief", pk=touch.pk)
    else:
        form = ChatDebriefForm(instance=existing)

    # The promotion is offered only while it would actually change
    # something: answered yes, not taken yet, not already an advocate.
    offer_promotion = bool(
        existing
        and existing.advocate_answer == "yes"
        and not existing.promoted
        and touch.contact.warmth != "advocate"
    )
    return render(
        request,
        "crm/debrief.html",
        {
            "touch": touch,
            "contact": touch.contact,
            "form": form,
            "debrief": existing,
            "offer_promotion": offer_promotion,
        },
    )


@login_required
@require_POST
def debrief_dismiss(request: HttpRequest, pk: int) -> HttpResponse:
    """Skip this debrief. Re-renders the cockpit so the card disappears in
    place, like the other Today quick actions."""
    touch = get_object_or_404(
        Touch.objects.for_user(request.user).select_related("contact"), pk=pk, kind="chat"
    )
    debrief_svc.dismiss(request.user, touch)
    return render(request, "crm/_cockpit.html", _cockpit_context(request.user))


@login_required
@require_POST
def debrief_promote(request: HttpRequest, pk: int) -> HttpResponse:
    """Accept the offered advocate promotion. The state change itself goes
    through `crm.services.set_contact_state`, which writes the audit touch
    — see `crm.debrief.promote`."""
    touch = get_object_or_404(Touch.objects.for_user(request.user), pk=pk, kind="chat")
    row = get_object_or_404(
        ChatDebrief.objects.for_user(request.user), touch=touch
    )
    debrief_svc.promote(row)
    record_event("advocate_promoted", user=request.user, source="debrief")
    messages.success(request, f"{row.contact.name} is now an advocate.")
    return redirect("crm:contact_detail", pk=row.contact_id)


import re as _re

_PAREN = _re.compile(r"\s*\([^)]*\)")


def _sentenceize(reason: str) -> str:
    """Rewrite a cadence-engine reason fragment as clean prose: strip
    parentheticals, turn em dashes and colons into sentence breaks,
    capitalize each sentence, collapse whitespace, end with a period. The
    engine's raw fragments stay untouched at the source (coverage_domain is
    another workstream); this is purely presentation."""
    if not reason:
        return reason
    text = _PAREN.sub("", reason)                       # drop "(confirmed)", "(within 24h)"
    text = text.replace(" — ", ". ").replace("—", ". ").replace(": ", ". ").replace(":", ". ")
    text = _re.sub(r"\s+", " ", text).strip()
    parts = [p.strip() for p in text.split(". ") if p.strip()]
    fixed = [(p[0].upper() + p[1:]).rstrip(".") for p in parts if p]
    out = ". ".join(fixed)
    return out + "." if out else out


def _dashboard_context(user) -> dict:
    """The Today dashboard's ledger stat cards. Stats read the SHARED zone
    (campus openings, deadlines) plus the user's own application funnel."""
    today = timezone.localdate()

    campus = Opportunity.objects.filter(status="open", bucket__in=TARGET_BUCKETS)
    all_open = Opportunity.objects.filter(status="open")
    open_now = campus.count()
    closing_10 = campus.filter(deadline__range=(today, today + timedelta(days=9))).count()
    # HK/US split shares the all-open denominator with tracked_live below, so
    # neither regional figure can exceed the headline (they were computed over
    # the smaller campus set before, which read as an inconsistency).
    hk = all_open.filter(Q(region__iexact="hk") | Q(firm__regions__contains=["hk"])).count()
    us = all_open.filter(Q(region__iexact="us") | Q(firm__regions__contains=["us"])).count()

    uo = UserOpportunity.objects.for_user(user)
    funnel = {
        "submitted": uo.filter(applied_status__iexact="submitted").count(),
        "interview": uo.filter(applied_status__iexact="interview").count(),
        "offer": uo.filter(applied_status__iexact="offer").count(),
    }

    return {
        "dash": {
            "open_now": open_now,
            "closing_10": closing_10,
            "tracked_live": all_open.count(),
            "hk": hk,
            "us": us,
            "funnel": funnel,
        },
    }


# ---------------------------------------------------------------------------
# 2. Contact list + detail.
# ---------------------------------------------------------------------------
# Needing-action buckets, in display order (radar layout).
_ACTION_GROUPS = [
    ("first_note", "Send the First Note", {"first_outreach"}),
    ("follow_up", "Follow Up", {"follow_up", "reping"}),
    ("thank_you", "Send the Thank-You Note", {"thank_you"}),
    ("others", "Others", set()),  # catch-all: confirm_chat, advance, maintain, park
]

# Contact-card sections below the coverage board, in display order.
_WARMTH_SECTIONS = [
    ("replied", "Replied"),
    ("chatted", "Chatted"),
    ("advocate", "Advocates"),
    ("no_reply", "Emailed, No Reply"),
]


def contact_region(c) -> str | None:
    """The region a Contact row belongs to, or None when it genuinely isn't
    known — the SAME answer `cadence.contact_region` gives the engine.

    Delegated rather than reimplemented on purpose. The cadence engine already
    decides this question (branch 3 scopes its pre-deadline re-ping by region),
    and if the Network page answered it differently the product would show a
    person under "Hong Kong" while re-pinging them against US deadlines. One
    function, one answer: the explicit `Contact.region` column, or None.

    `source` is still passed for shape compatibility with the engine's input
    dicts, but nothing reads it — the legacy provenance-text inference was
    retired from the read path (see `cadence.contact_region`). That retirement
    is what finally lets `_in_scope`'s firm fallback below actually run: it
    was written for a None that the old inference never returned.
    """
    return cadence.contact_region({"region": c.region, "source": c.source})


def _in_scope(c, scope: str) -> bool:
    """Does contact `c` belong in the `scope` region tab?

    Precedence, mirroring `cadence.contact_region` exactly so the two can never
    disagree about a person:

      1. Resolved region (the explicit `Contact.region` column) matches the
         scope -> in, and ONLY in, that one tab.
      2. Resolved region is None — the contact has no region set, i.e.
         genuinely unknown — fall back to the firm's regions, which can put
         the contact in more than one tab.

    Step 2 is deliberately the LAST resort rather than the first, which is the
    whole fix. A firm's `regions` describes the FIRM, not the person: most
    bulge brackets carry ['us', 'hk'], so filtering on it put one contact in
    both tabs and made the two lists near-duplicates. A person works in one
    place; a firm recruits in several.

    Showing a genuinely-unknown contact in every tab is the honest answer (we
    do not know, so we do not hide them from the tab they might belong to), but
    it is an admission of ignorance, not a regional match — so the caller marks
    these unconfirmed and the contact card renders no region pill for them. It
    never asserts a region nobody set.
    """
    # Singapore and Europe are tabs the FIRM directory supports but the contact
    # vocabulary does not: `Contact.REGION_CHOICES` is us/hk only, so a person
    # can never resolve to "sg" and asking their region would empty those tabs
    # for everyone. There the firm is the only evidence that exists, so it stays
    # the whole test — which is also exactly how these tabs behaved before.
    if scope not in Contact.REGION_VALUES:
        return bool(c.firm and scope in (c.firm.regions or []))
    region = contact_region(c)
    if region is not None:
        return region == scope
    return bool(c.firm and scope in (c.firm.regions or []))


def _contact_card(c, *, tier, today, capture_addr):
    """One full contact card (radar style): initials, pills, firm · role,
    note bullets in plain grammar, and days since the last touch."""
    parts = [p for p in (c.name or "").split() if p]
    initials = "".join(p[0] for p in parts[:2]).upper()
    bullets = []
    for raw in (c.notes, c.angle):
        for frag in (raw or "").replace(";", "\n").splitlines():
            frag = frag.strip().strip("-• ").rstrip(".")
            if frag:
                bullets.append(frag[0].upper() + frag[1:])
    last = c.last_touch_ts
    days_since = (timezone.now() - last).days if last else None
    return {
        "c": c,
        "initials": initials or "?",
        "gender": (c.gender or "")[:1].upper(),
        "tier": tier,
        "school": c.school,
        # Blank when unknown — the chip simply doesn't render, rather than the
        # card asserting a region nobody set.
        "region": (c.region or "").upper(),
        "bullets": bullets[:3],
        "days_since": days_since,
        # Compose surface: same rule as every other mailto: on the site (§5)
        # — BCC'd to the capture address, body from `opener` ONLY, never
        # `angle` (that's the user's private note ABOUT the person, not a
        # draft addressed TO them). Before this, the Network board's email
        # link was a bare `mailto:` with no BCC, so a send started here was
        # invisible to Coverage's capture pipeline.
        "mailto": _mailto(c.email or "", capture_addr, body=(c.opener or "")),
    }


@login_required
def contact_list(request: HttpRequest) -> HttpResponse:
    """The Network board (radar layout): scope tabs (US / Hong Kong /
    School), Contacts Needing Action grouped by the cadence verb, Firm
    Coverage grouped by the user's own tiers (draggable — tier drives the
    cadence engine's priorities), then full contact cards sectioned by
    warmth."""
    user = request.user
    today = timezone.localdate()
    actions, _, capture_addr = _build_actions(user)

    contacts = list(
        Contact.objects.for_user(user)
        .filter(archived=False)
        .select_related("firm")
        .annotate(
            last_touch_ts=models_Max("touches__ts"),
            touch_count=models_Count("touches"),
        )
    )

    scope = request.GET.get("scope", "").strip().lower()
    if scope == "school":
        contacts = [c for c in contacts if c.school or c.school_affiliation]
    elif scope in ("us", "hk", "sg", "eu"):
        contacts = [c for c in contacts if _in_scope(c, scope)]
    # How many of the shown contacts are here on a guess rather than a set
    # region. Rendered as a one-line caveat under the Contacts header so a
    # region tab never silently passes off "unknown" as "confirmed".
    unconfirmed_total = (
        sum(1 for c in contacts if not c.region)
        if scope in ("us", "hk", "sg", "eu")
        else 0
    )
    scoped_ids = {c.id for c in contacts}
    scoped_firm_ids = {c.firm_id for c in contacts if c.firm_id}

    # --- Contacts Needing Action (left column) -------------------------
    actions = [a for a in actions if a["contact"]["id"] in scoped_ids]
    grouped = {key: [] for key, _, _ in _ACTION_GROUPS}
    for a in actions:
        for key, _, kinds in _ACTION_GROUPS:
            if a["action"] in kinds:
                grouped[key].append(a)
                break
        else:
            grouped["others"].append(a)
    action_groups = [
        {"key": key, "label": label, "items": grouped[key]}
        for key, label, _ in _ACTION_GROUPS
    ]

    # --- Firm Coverage (right column), grouped by the user's tiers -----
    user_firms = list(
        UserFirm.objects.for_user(user).select_related("firm")
    )
    firm_ids = [uf.firm_id for uf in user_firms]
    campus = Opportunity.objects.filter(
        status="open", bucket__in=TARGET_BUCKETS, firm_id__in=firm_ids
    )
    open_by_firm = dict(
        campus.values_list("firm_id").annotate(n=models_Count("id")).values_list("firm_id", "n")
    )
    soon_by_firm = dict(
        campus.filter(deadline__range=(today, today + timedelta(days=14)))
        .values_list("firm_id").annotate(n=models_Count("id")).values_list("firm_id", "n")
    )
    for fd in FirmDate.objects.filter(
        firm_id__in=firm_ids, precision="day",
        date__range=(today, today + timedelta(days=30)),
    ):
        soon_by_firm[fd.firm_id] = soon_by_firm.get(fd.firm_id, 0) + 1
    act_by_firm: dict[int, int] = {}
    for a in actions:
        fid = a["contact"].get("firm_id")
        if fid:
            act_by_firm[fid] = act_by_firm.get(fid, 0) + 1

    by_firm_contacts: dict[int, list] = {}
    for c in contacts:
        if c.firm_id:
            by_firm_contacts.setdefault(c.firm_id, []).append(c)

    # The advocates-per-firm yardstick every card and every tier-cost line
    # measures against (User.assets["advocate_target"], default 2).
    adv_target = coverage.advocate_target(user)

    def firm_card(uf):
        cs = by_firm_contacts.get(uf.firm_id, [])
        total = len(cs) or 1
        segments = [
            {"warmth": w, "pct": round(sum(1 for c in cs if c.warmth == w) * 100 / total)}
            for w in ("cold", "replied", "chatted", "advocate")
        ]
        advocates = sum(1 for c in cs if c.warmth == "advocate")
        return {
            "firm": uf.firm,
            "tier": uf.tier,
            "open": open_by_firm.get(uf.firm_id, 0),
            "soon": soon_by_firm.get(uf.firm_id, 0),
            "act_now": act_by_firm.get(uf.firm_id, 0),
            "sponsors": uf.firm.sponsors is True or uf.firm.sponsors == "true",
            "contact_count": len(cs),
            "segments": segments,
            # "1/2 advocates" against the user's target. `met` is the whole
            # point of showing a fraction rather than a count: a firm that
            # has hit the target should read as finished, not as 2 more
            # things you haven't done.
            "advocates": advocates,
            "adv_target": adv_target,
            "adv_met": advocates >= adv_target,
        }

    tier_sections = []
    for tier, label in ((1, "Tier 1"), (2, "Tier 2"), (3, "Tier 3"), (None, "Unranked")):
        cards = [firm_card(uf) for uf in user_firms if uf.tier == tier]
        if scope in ("us", "hk", "sg", "eu"):
            # CORRECT AS-IS — deliberately still `firm.regions`, and NOT the
            # per-contact rule above. A firm genuinely does span regions:
            # Goldman really recruits in both Hong Kong and the US, so it
            # belongs on both boards. Only a PERSON has one location, which is
            # why the contact filter had to stop asking the firm.
            #
            # The card's numbers are already region-correct without touching
            # this line: `by_firm_contacts` is built from the scoped `contacts`
            # list above, so under the HK tab Goldman's warmth bars, contact
            # count and advocate fraction now count only its HK people.
            cards = [fc for fc in cards if scope in (fc["firm"].regions or [])]
        if cards or tier in (1, 2, 3):
            cards.sort(key=lambda fc: (-fc["act_now"], -fc["open"], fc["firm"].name))
            tier_sections.append({
                "tier": tier,
                "label": label,
                "cards": cards,
                # What this tier is committing the user to, in advocates.
                # Only for real tiers: "Unranked" is not a commitment.
                "cost": coverage.tier_cost(cards, adv_target) if tier else None,
            })

    # --- Coverage Gaps strip (top of the page) ---------------------------
    # Only CONFIRMED official close dates count toward urgency — the same
    # bar cadence._closing_soon holds. Anything rumored or merely reported
    # must not move a firm up the strip.
    closes: dict[int, Any] = {}
    for fd in FirmDate.objects.filter(
        firm_id__in=firm_ids, event_kind="app_close", date__gte=today
    ):
        if _confidence_label(fd.confidence) != cadence.CONFIRMED:
            continue
        if fd.firm_id not in closes or fd.date < closes[fd.firm_id]:
            closes[fd.firm_id] = fd.date

    gaps = coverage.rank_gaps(
        [
            {
                "firm_id": uf.firm_id,
                "name": uf.firm.name,
                "tier": uf.tier,
                "warmths": [c.warmth for c in by_firm_contacts.get(uf.firm_id, [])],
                "app_close": closes.get(uf.firm_id),
            }
            for uf in user_firms
        ],
        today=today,
        target=adv_target,
    )
    # One click to act on each gap: somewhere to start when the firm is
    # empty, and the warmest person who isn't an advocate yet when it
    # isn't — that contact is the shortest path to closing the gap.
    firms_by_id = {uf.firm_id: uf.firm for uf in user_firms}
    lever_rank = {"chatted": 0, "replied": 1, "cold": 2}
    for g in gaps:
        firm = firms_by_id.get(g["firm_id"])
        g["slug"] = firm.slug if firm else ""
        candidates = [
            c for c in by_firm_contacts.get(g["firm_id"], []) if c.warmth != "advocate"
        ]
        # Sort by warmth then id so the pick is stable across renders.
        candidates.sort(key=lambda c: (lever_rank.get(c.warmth, 3), c.id))
        g["lever"] = candidates[0] if candidates else None

    # --- Full contact cards ---------------------------------------------
    # Warmth sections normally; in School scope the sections ARE the
    # universities — each section header is the person's school.
    tiers_by_firm = {uf.firm_id: uf.tier for uf in user_firms}
    sections = []
    if scope == "school":
        by_school: dict[str, list] = {}
        for c in contacts:
            by_school.setdefault((c.school or "School").upper(), []).append(c)
        for name in sorted(by_school):
            sections.append({
                "key": "school",
                "label": name,
                "cards": [
                    _contact_card(c, tier=tiers_by_firm.get(c.firm_id), today=today, capture_addr=capture_addr)
                    for c in by_school[name]
                ],
            })
    else:
        for key, label in _WARMTH_SECTIONS:
            if key == "no_reply":
                members = [c for c in contacts if c.warmth == "cold" and c.touch_count]
            else:
                members = [c for c in contacts if c.warmth == key]
            sections.append({
                "key": key,
                "label": label,
                "cards": [
                    _contact_card(c, tier=tiers_by_firm.get(c.firm_id), today=today, capture_addr=capture_addr)
                    for c in members
                ],
            })

    return render(
        request,
        "crm/contact_list.html",
        {
            "scope": scope,
            "gaps": gaps,
            "adv_target": adv_target,
            "action_groups": action_groups,
            "action_total": len(actions),
            "tier_sections": tier_sections,
            "firm_total": len(user_firms),
            "sections": sections,
            "contact_total": len(contacts),
            "unconfirmed_total": unconfirmed_total,
        },
    )


@login_required
@require_POST
def set_firm_tier(request: HttpRequest) -> HttpResponse:
    """Drag-and-drop target: move one of the user's firms to a new tier.
    Tier drives the cadence engine's prioritization (firm_meta in
    `_build_actions`), so a drag literally reorders tomorrow's queue."""
    try:
        firm_id = int(request.POST.get("firm", ""))
    except ValueError:
        return HttpResponse(status=400)
    tier_raw = request.POST.get("tier", "")
    tier = int(tier_raw) if tier_raw in ("1", "2", "3") else None
    updated = UserFirm.objects.for_user(request.user).filter(firm_id=firm_id).update(tier=tier)
    if not updated:
        return HttpResponse(status=404)
    record_event("firm_tier_set", user=request.user)
    return HttpResponse(status=204)


@login_required
def contact_new(request: HttpRequest) -> HttpResponse:
    """Hand-add a contact — the coffee-chat path the CRM was missing. A
    ?firm=<slug> query pre-selects a firm (used by the firm page's button)."""
    if request.method == "POST":
        form = ContactForm(request.POST)
        if form.is_valid():
            contact = form.save(commit=False)
            contact.user = request.user
            contact.source = "manual"
            contact.save()
            record_event("contact_added", user=request.user, source="manual")
            messages.success(request, f"Added {contact.name}.")
            return redirect("crm:contact_detail", pk=contact.pk)
    else:
        initial = {}
        firm_slug = request.GET.get("firm")
        if firm_slug:
            initial["firm"] = Firm.objects.filter(slug=firm_slug).first()
        form = ContactForm(initial=initial)
    return render(request, "crm/contact_form.html", {"form": form, "mode": "new"})


@login_required
def contact_edit(request: HttpRequest, pk: int) -> HttpResponse:
    """Edit an existing contact. Scoped through `.for_user`, so another
    tenant's id 404s indistinguishably from a missing one."""
    contact = get_object_or_404(Contact.objects.for_user(request.user), pk=pk)
    if request.method == "POST":
        form = ContactForm(request.POST, instance=contact)
        if form.is_valid():
            form.save()
            messages.success(request, "Contact updated.")
            return redirect("crm:contact_detail", pk=contact.pk)
    else:
        form = ContactForm(instance=contact)
    return render(request, "crm/contact_form.html",
                  {"form": form, "mode": "edit", "contact": contact})


# ---------------------------------------------------------------------------
# 2b. Archive / unarchive — the contact lifecycle's exit AND its way back.
# ---------------------------------------------------------------------------
# `Contact.archived` has existed since the first migration and every query in
# the app filters on it, but nothing could ever SET it from the UI and no page
# ever listed the rows it hid. That made it a one-way trapdoor operated only by
# automated paths: 25 of the founder's 137 contacts sat archived and invisible,
# and because both capture resolvers filter `archived=False`, a later genuine
# reply from one of them FORKED a new contact rather than resurrecting the old
# one — the history split in two and neither half was complete.
#
# The three views below are the missing half. Archiving is now something a
# person does on purpose and can undo; correspondingly, no automated path
# archives at all any more (see capture/gmail.py's bounce block).
def _set_archived(request: HttpRequest, pk: int, *, archived: bool) -> Contact:
    """Flip `archived` on one of the user's contacts. A plain ORM write on
    purpose: `archived` is a UI/lifecycle flag, not part of the
    warmth/thread_state ratchet that must go through `crm.services`. It
    changes nothing about the relationship's history — every touch stays on
    the row and comes back with it."""
    contact = get_object_or_404(Contact.objects.for_user(request.user), pk=pk)
    if contact.archived != archived:
        contact.archived = archived
        contact.save(update_fields=["archived"])
    return contact


@login_required
@require_POST
def contact_archive(request: HttpRequest, pk: int) -> HttpResponse:
    """Archive a contact: off the Network board, out of the cadence queue,
    out of coverage counts — but not deleted, and one click from coming
    back."""
    contact = _set_archived(request, pk, archived=True)
    record_event("contact_archived", user=request.user)
    messages.success(
        request,
        f"Archived {contact.name}. They're in Archived Contacts if you want "
        "them back.",
    )
    return redirect("crm:contact_list")


@login_required
@require_POST
def contact_unarchive(request: HttpRequest, pk: int) -> HttpResponse:
    """Bring a contact back, with their whole touch history intact."""
    contact = _set_archived(request, pk, archived=False)
    record_event("contact_unarchived", user=request.user)
    messages.success(request, f"{contact.name} is back on your board.")
    return redirect("crm:contact_detail", pk=contact.pk)


@login_required
def contact_archived(request: HttpRequest) -> HttpResponse:
    """The archived list — the view that makes archiving reversible in
    practice rather than only in principle. Deliberately plain: this is a
    recovery surface, not a second Network board."""
    contacts = list(
        Contact.objects.for_user(request.user)
        .filter(archived=True)
        .select_related("firm")
        .annotate(last_touch_ts=models_Max("touches__ts"))
        .order_by("name")
    )
    return render(
        request,
        "crm/contact_archived.html",
        {"contacts": contacts, "contact_total": len(contacts)},
    )


@login_required
def contact_detail(request: HttpRequest, pk: int) -> HttpResponse:
    # for_user() 404s cleanly for another tenant's id (indistinguishable from
    # a non-existent id — the tenancy guarantee, §2). Not filtered on
    # `archived`: an archived contact's page must stay reachable, or the
    # Archived list would have nowhere to link and unarchiving would have no
    # home.
    contact = get_object_or_404(Contact.objects.for_user(request.user), pk=pk)
    context = _contact_live_context(request, contact)
    # §6: the fit score is computed on the fly and shown here — record the view.
    record_event("score_viewed", user=request.user)
    return render(request, "crm/contact_detail.html", context)


# ---------------------------------------------------------------------------
# 3. Log-a-touch (htmx) — the capture-rate hook (§5): visible warmth movement.
# ---------------------------------------------------------------------------
@login_required
@require_POST
def log_touch(request: HttpRequest, pk: int) -> HttpResponse:
    """POST kind+channel, ratchet the state via the reviewed pipeline adapter,
    then re-render the live panel so the user SEES warmth move in the same
    session. Returns the `#contact-live` fragment for an htmx outerHTML swap."""
    contact = get_object_or_404(Contact.objects.for_user(request.user), pk=pk)

    kind = (request.POST.get("kind") or "").strip()
    channel = (request.POST.get("channel") or "").strip()
    note = (request.POST.get("note") or "").strip() or None

    error = None
    updates: dict[str, str] = {}
    before_warmth, before_state = contact.warmth, contact.thread_state

    if kind not in TOUCH_TRANSITIONS:
        error = "Pick an interaction type."
    elif channel not in CHANNELS:
        error = "Pick a channel."
    else:
        updates = services.log_touch(request.user.id, contact.id, kind, channel, note)
        record_event("touch_logged", user=request.user, source="manual")
        contact.refresh_from_db()

    moved = {
        "logged": error is None,
        "error": error,
        "kind": kind,
        "kind_label": dict(TOUCH_KIND_LABELS).get(kind, kind),
        "changed": bool(updates),
        "from_warmth": before_warmth,
        "from_state": before_state,
    }
    context = _contact_live_context(request, contact, moved=moved)
    return render(request, "crm/_contact_live.html", context)


# ---------------------------------------------------------------------------
# Shared context for the live panel (used by the detail page and the htmx
# fragment, so both render identically).
# ---------------------------------------------------------------------------
def _contact_live_context(
    request: HttpRequest, contact: Contact, *, moved: dict | None = None
) -> dict[str, Any]:
    user = request.user
    now = timezone.now()

    touches = list(
        Touch.objects.for_user(user).filter(contact=contact).order_by("-ts")
    )
    touch_dicts = _touch_dicts(touches)

    contact_score = scoring.score_contact(
        {
            "id": contact.id,
            "role": contact.role,
            "school_affiliation": contact.school_affiliation,
        },
        touch_dicts,
        as_of=now,
    )

    # Optional firm-fit view (§6): only when the contact belongs to a
    # directory firm. Reuses the user's other contacts at that firm so the
    # network axis shares one definition of warmth.
    firm_score = None
    firm = contact.firm
    if firm is not None:
        firm_contacts = list(
            Contact.objects.for_user(user).filter(firm=firm, archived=False)
        )
        fc_ids = [c.id for c in firm_contacts]
        firm_touches = _touch_dicts(
            Touch.objects.for_user(user).filter(contact_id__in=fc_ids)
        )
        firm_dates = [
            {
                "event_kind": fd.event_kind,
                "region": fd.region,
                "date": fd.date,
                "confidence": _confidence_label(fd.confidence),
            }
            for fd in FirmDate.objects.filter(firm=firm)
        ]
        # The Network axis measures against `advocate_target` full-strength
        # advocates as its 100-point yardstick (scoring._score_network). Left
        # at `params=None` this silently falls back to
        # `scoring.DEFAULT_PARAMS["advocate_target"] = 2` — but
        # `coverage.advocate_target(user)` reads the user's own tunable
        # `User.assets["advocate_target"]`, which is what every OTHER
        # coverage number on this page (firm cards, "N/target advocates")
        # is measured against. Without building the params bundle here, a
        # firm would read as "covered" on the contact-detail fit score and
        # not-covered on the firm-coverage list the instant a user changed
        # their target — same firm, two different answers. `version` is
        # tagged with the target so a changed setting is a visible,
        # rehashable event rather than the same `inputs_hash` silently
        # meaning two different things.
        adv_target = coverage.advocate_target(user)
        firm_score = scoring.score_firm(
            {
                "id": user.id,
                "regions": user.regions,
                "tracks": user.tracks,
                # Derived per firm-region from the user's own work
                # authorization, so the structural axis actually moves. This
                # used to be a hardcoded None — "unknown" for every user
                # forever, which neutralized the sponsorship component of the
                # score permanently. `needs_sponsorship` still returns None
                # when the user has no entry for the regions in play; unknown
                # is a real answer, it just isn't the only one now.
                "needs_sponsorship": scoring.needs_sponsorship(
                    user.work_authorization, user.regions, firm.regions
                ),
            },
            {
                "id": firm.id,
                "regions": firm.regions,
                "tracks": firm.tracks,
                "sponsors": firm.sponsors,
            },
            [
                {
                    "id": c.id,
                    "role": c.role,
                    "school_affiliation": c.school_affiliation,
                }
                for c in firm_contacts
            ],
            firm_touches,
            firm_dates,
            as_of=now,
            params={
                **scoring.DEFAULT_PARAMS,
                "advocate_target": adv_target,
                "version": f"scoring-v1+at{adv_target}",
            },
        )

    # Warmth-meter animation endpoints. On a plain GET both are the current
    # level (no visible motion); on a POST that ratcheted, `from` is the old
    # level so the fill animates the jump.
    to_pct = _warmth_pct(contact.warmth)
    from_pct = _warmth_pct(moved["from_warmth"]) if moved else to_pct

    return {
        "contact": contact,
        "touches": touches,
        "contact_score": contact_score,
        "firm_score": firm_score,
        "firm": firm,
        "warmth_from_pct": from_pct,
        "warmth_to_pct": to_pct,
        "warmth_order": WARMTH_ORDER,
        "moved": moved,
        "touch_kinds": TOUCH_KIND_LABELS,
        # Which interaction the log form opens on. Today's `confirm_chat`
        # card links here with ?log=chat instead of one-click-logging a chat
        # itself: "the chat happened" is too large a claim for a card whose
        # whole reason to exist is that we don't know whether it did. Landing
        # on a pre-filled form is a two-step, and the second step is a human
        # confirming it. Ignored unless it names a real touch kind.
        "preselect_kind": (
            request.GET.get("log") if request.GET.get("log") in TOUCH_TRANSITIONS else None
        ),
        "channels": CHANNEL_LABELS,
        "mailto": _mailto(
            contact.email, _capture_address(user), body=(contact.opener or "")
        ),
        "capture_address": _capture_address(user),
    }
