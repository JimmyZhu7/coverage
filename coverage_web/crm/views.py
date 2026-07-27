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
from typing import Any
from urllib.parse import quote, urlencode

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Count as models_Count, Max as models_Max, Q
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from analytics.events import record_event
from analytics.models import UserOpportunity
from coverage_domain import cadence, scoring
from coverage_domain.pipeline import CHANNELS, TOUCH_TRANSITIONS
from crm.forms import ChatDebriefForm, ContactForm
from directory.classify import TARGET_BUCKETS
from directory.models import Firm
from directory.models import Firm, FirmDate, Opportunity

from . import coverage, debrief as debrief_svc, services
from .models import ChatDebrief, Contact, Touch, UserFirm

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


def _build_actions(user):
    """The cadence queue, shared by Today and Network: fetch the user's
    contacts/touches/tiers/firm-dates, run `cadence.due_actions`, and dress
    each action for display (label, prose reason, warmth, compose link).
    Returns (actions, contacts, capture_address)."""
    now = timezone.now()
    contacts = list(
        Contact.objects.for_user(user)
        .filter(archived=False)
        .filter(Q(snoozed_until__isnull=True) | Q(snoozed_until__lte=now))
    )
    touches = list(Touch.objects.for_user(user))

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
        }
        for c in contacts
    ]

    actions = cadence.due_actions(
        contact_dicts,
        _touch_dicts(touches),
        firm_dates,
        as_of=timezone.now(),
        firms=firm_meta,
        params=_cadence_params(user),
    )

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
    "advance": "outreach",
    "confirm_chat": "chat",
}

# Weekly pace target — touches logged Monday-to-now. The product default, used
# when the user hasn't set their own `weekly_touch_goal`.
WEEKLY_TOUCH_GOAL = 10


def _cockpit_lane(priority: int) -> str:
    """cadence priority (0 = most urgent) -> the Today cockpit lane."""
    if priority <= 0:
        return "overdue"
    if priority == 1:
        return "due"
    return "later"


def _cockpit_context(user) -> dict:
    """The Today cockpit: the cadence queue split into Overdue / Due Now /
    Keep Warm lanes, a weekly pace figure, and a recent-activity feed."""
    today = timezone.localdate()
    actions, contacts, _ = _build_actions(user)

    lanes: dict[str, list] = {"overdue": [], "due": [], "later": []}
    for a in actions:
        a["touch_kind"] = _ACTION_TOUCH.get(a["action"], "outreach")
        lanes[_cockpit_lane(a["priority"])].append(a)

    # Pace: touches logged since Monday against the weekly goal.
    week_start = today - timedelta(days=today.weekday())
    done = Touch.objects.for_user(user).filter(ts__date__gte=week_start).count()
    # `or` (not a None check) on purpose: a stored 0 is not a goal of zero —
    # a zero-touch week target would make the ring meaningless and divide by
    # zero below — so it falls back to the product default like NULL does.
    goal = getattr(user, "weekly_touch_goal", None) or WEEKLY_TOUCH_GOAL
    pace = {
        "done": done,
        "goal": goal,
        "pct": min(100, round(done / goal * 100)) if goal else 0,
        "remaining": max(0, goal - done),
        "hit": done >= goal,
    }

    # Activity feed: the last touches logged — what changed since last look.
    kind_labels = dict(TOUCH_KIND_LABELS)
    recent = Touch.objects.for_user(user).select_related("contact").order_by("-ts")[:8]
    activity = [
        {
            "name": t.contact.name,
            "contact_id": t.contact_id,
            "kind": t.kind,
            "kind_label": kind_labels.get(t.kind, t.kind.replace("_", " ").capitalize()),
            "ts": t.ts,
            "inbound": t.kind == "reply_received",
        }
        for t in recent
    ]

    return {
        "lanes": lanes,
        "queue_total": len(actions),
        # Chats from the last week that nobody has written down yet. Its own
        # lane rather than a cadence action: the cadence engine is pure and
        # knows nothing about ChatDebrief, and this prompt is about capturing
        # what already happened rather than about the next outbound move.
        "debriefs": debrief_svc.pending(user),
        "pace": pace,
        "activity": activity,
        "contact_count": len(contacts),
    }


@login_required
def week(request: HttpRequest) -> HttpResponse:
    """Today: a two-column cockpit — the cadence queue (Overdue / Due Now /
    Keep Warm) beside a pace ring and activity feed — under the stat cards
    and a slim week ribbon."""
    return render(
        request,
        "crm/week.html",
        {**_cockpit_context(request.user), **_dashboard_context(request.user)},
    )


@login_required
@require_POST
def today_act(request: HttpRequest, pk: int, verb: str) -> HttpResponse:
    """A Today-card quick action: log a Sent/Reply touch (advancing the
    cadence), Park a contact out of the cadence entirely, or Snooze/Skip it
    out of today's queue. Re-renders the whole cockpit so the queue, pace,
    and activity feed stay in sync."""
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
        "channels": CHANNEL_LABELS,
        "mailto": _mailto(
            contact.email, _capture_address(user), body=(contact.opener or "")
        ),
        "capture_address": _capture_address(user),
    }
