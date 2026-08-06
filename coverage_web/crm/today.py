"""The Today engine: the cadence queue, the cockpit, and its actions.

Moved whole from crm/views.py (2026-08-05), which had grown to 1,914 lines
with three pages entangled in one namespace. The public names — `week`,
`today_park_all`, `today_act`, and the tested internals — are re-exported by
crm.views, so URLs, tests, and anything else importing the old paths keep
working unchanged.
"""

from __future__ import annotations

from datetime import timedelta
from math import ceil

from django.contrib.auth.decorators import login_required
from django.db.models import Count as models_Count, Max as models_Max, Q
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, render
from django.utils import timezone
from django.utils.timesince import timesince
from django.views.decorators.http import require_POST

from analytics.events import record_event
from analytics.models import UserOpportunity
from coverage_domain import cadence
from coverage_domain.pipeline import CHANNELS, MANUAL_OVERRIDE_KIND, TOUCH_TRANSITIONS
from directory.classify import TARGET_BUCKETS
from directory.models import Firm, FirmDate, Opportunity

from . import debrief as debrief_svc, services
from .models import CalendarEvent, ChatDebrief, Contact, Touch, UserFirm
from .utils import (
    ACTION_LABELS,
    FIRM_DATE_LABELS as _FIRM_DATE_LABELS,
    TOUCH_KIND_LABELS,
    CHANNEL_LABELS,
    _capture_address,
    _clock,
    _confidence_label,
    _mailto,
    _touch_dicts,
    _warmth_pct,
    WARMTH_ORDER,
)

import re as _re


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


def _pace_history(user, today, weeks: int = 8) -> list[dict]:
    """The last N weeks of the user's own outbound work, oldest first.

    The ring shows this week and forgets every other: goal hit, gone Monday.
    A habit needs a trace — eight bars under the ring turn "how am I doing"
    from a feeling into a shape. Same kind-filter as the ring
    (PACE_TOUCH_KINDS), same Monday weeks (TruncWeek), so the last bar always
    equals the ring's own number.
    """
    from django.db.models.functions import TruncWeek

    start = today - timedelta(days=today.weekday(), weeks=weeks - 1)
    counts = {}
    for row in (Touch.objects.for_user(user)
                .filter(kind__in=PACE_TOUCH_KINDS, ts__date__gte=start)
                .annotate(week=TruncWeek("ts"))
                .values("week")
                .annotate(n=models_Count("id"))):
        key = row["week"].date() if hasattr(row["week"], "date") else row["week"]
        counts[key] = counts.get(key, 0) + row["n"]

    goal = user.weekly_touch_goal or WEEKLY_TOUCH_GOAL
    out = []
    for i in range(weeks):
        monday = start + timedelta(weeks=i)
        n = counts.get(monday, 0)
        out.append({
            "n": n,
            "hit": n >= goal,
            "label": f"week of {monday:%b} {monday.day}: {n}",
        })
    scale = max([goal] + [w["n"] for w in out])
    for w in out:
        w["pct"] = round(100 * w["n"] / scale) if scale else 0
    return out


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
        elif offset < 7:
            label = day.strftime("%a")
            when = label if ev.all_day else f"{_clock(at)} {label}"
        else:
            # Beyond a week a weekday is AMBIGUOUS on this horizon: with 14
            # days in view there are two Fridays, and "Fri" on the second one
            # reads as the first. Past that point the date is the only honest
            # label, and the clock time matters less than which week it is.
            when = f"{day.strftime('%b')} {day.day}"
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

    # NOT capped here. The rail shows six, but `_chat_prep` and `_daybar`
    # read this list too — capping at the source meant a seventh event today
    # silently lost its prep card and its dot on the day track. The cap is a
    # display decision, so it happens where the display is built.
    rows.sort(key=lambda r: r["sort"])
    return rows


_DAYBAR_START, _DAYBAR_END = 8 * 60, 20 * 60      # 8am -> 8pm


def _daybar(schedule, now) -> dict:
    """Today's timed events as positions on one 8am-8pm track.

    A list tells you WHAT is on today; it does not tell you the shape of the
    day — whether the three things are stacked into one morning or spread from
    breakfast to dinner. One track answers that before a word is read, and it
    is the only place on this page where the answer is free: the times are
    already loaded.

    Times outside the window clamp to its ends rather than vanishing. A 7am
    call is genuinely "first thing", and dropping it to keep the scale honest
    would lose the event to keep the axis pretty.
    """
    span = _DAYBAR_END - _DAYBAR_START
    dots = []
    for row in schedule:
        if not (row["is_today"] and row["timed"] and row["at"]):
            continue
        minutes = row["at"].hour * 60 + row["at"].minute
        pct = (min(max(minutes, _DAYBAR_START), _DAYBAR_END) - _DAYBAR_START) / span
        dots.append({
            "pct": round(pct * 100, 2),
            "label": row["title"],
            "when": row["when"],
            "kind": row["kind"],
        })

    now_minutes = now.hour * 60 + now.minute
    in_window = _DAYBAR_START <= now_minutes <= _DAYBAR_END
    return {
        "dots": dots,
        "now_pct": round((now_minutes - _DAYBAR_START) / span * 100, 2) if in_window else None,
        # The track is only worth its pixels once something is actually on it.
        "show": bool(dots),
    }


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
    # Six, not eight. The rail carries four cards now; the feed is the
    # longest and the least time-critical of them, so it is the one that
    # gives ground to keep the whole column inside a laptop viewport.
    recent = Touch.objects.for_user(user).select_related("contact").order_by("-ts")[:6]
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
        "pace_history": _pace_history(user, today),
        # The timed layer. `schedule` merges real calendar datetimes with the
        # chats nobody has put a time on yet; `chat_prep` is the subset
        # happening today, with the last debrief pulled up alongside.
        "schedule": schedule[:6],
        "daybar": _daybar(schedule, timezone.localtime(timezone.now())),
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
        {**_cockpit_context(request.user), **_dashboard_context(request.user),
         # Signup lands on the /welcome/ wizard, but nothing ever looked at
         # whether it was FINISHED: close the tab at step one and every later
         # login lands here, on an empty queue over an unpersonalized feed,
         # with no path back. A banner, never a redirect — the app must stay
         # usable mid-setup, it just shouldn't be silent about what's missing.
         "needs_onboarding": request.user.onboarded_at is None},
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


