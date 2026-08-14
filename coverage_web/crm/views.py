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

import re
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
from django.urls import reverse
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

from . import ai_brief, coverage, debrief as debrief_svc, services
from .models import CalendarEvent, ChatDebrief, Contact, Touch, UserFirm


# The Today engine lives in crm/today.py; the shared helpers in crm/utils.py.
# Both are re-exported here because this module is the import surface the
# URLconf and a few hundred test assertions already use — the split changes
# where code LIVES, not how it is reached.
from .today import (  # noqa: F401
    PACE_TOUCH_KINDS,
    TODAY_PLAN_MAX,
    TODAY_PLAN_MIN,
    TUNABLE_CADENCE_PARAMS,
    WEEKLY_TOUCH_GOAL,
    _INBOUND_TOUCH_KINDS,
    _build_actions,
    _cadence_params,
    _cockpit_context,
    _daily_cap,
    _dashboard_context,
    _pace,
    _schedule,
    _today_class,
    _today_sort_key,
    _workdays_left,
    today_act,
    today_park_all,
    week,
)
from .utils import (  # noqa: F401
    CHANNEL_LABELS,
    FIRM_DATE_LABELS as _FIRM_DATE_LABELS,
    TOUCH_KIND_LABELS,
    WARMTH_ORDER,
    _capture_address,
    _clock,
    _confidence_label,
    _mailto,
    _touch_dicts,
    _warmth_pct,
)

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
                    "Debrief saved" + (f": {', '.join(notes)}." if notes else "."),
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

# Warmest-first tie-break for "who to work next at this firm" — chatted
# beats replied beats cold, an advocate is never a candidate (there's
# nothing left to convert them into).
_LEVER_RANK = {"chatted": 0, "replied": 1, "cold": 2}


def _pick_lever(contacts):
    """The single best contact to work next at a firm: the warmest
    non-advocate, ties broken by id so the pick is stable across renders.

    Shared by the Coverage Gaps strip and every firm card on the board
    below it — before this, each computed its own copy, so a firm's "top
    pick" in the gaps strip could disagree with itself if the two were
    ever edited separately."""
    candidates = [c for c in contacts if c.warmth != "advocate"]
    candidates.sort(key=lambda c: (_LEVER_RANK.get(c.warmth, 3), c.id))
    return candidates[0] if candidates else None


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


# The keep-warm clock each warmth class runs on, in days — the same windows
# the cadence engine acts on (weeks * 7 for the two check-in clocks; cold
# contacts run on the follow-up window). The staleness ring divides elapsed
# silence by this, so a full ring means "the engine is about to nag you
# about this person", not an arbitrary redness.
def _stale_window_days(c, params) -> int:
    from coverage_domain.cadence import CADENCE_DEFAULTS

    merged = {**CADENCE_DEFAULTS, **params}
    warmth = (c.warmth or "").lower()
    if warmth == "advocate":
        return merged["advocate_touch_min_weeks"] * 7
    if warmth == "chatted":
        return merged["chatted_touch_min_weeks"] * 7
    # Cold and replied both run on the follow-up window; business days are
    # roughly seven-fifths of calendar days.
    return max(round(merged["followup_after_business_days"] * 7 / 5), 1)


def _contact_card(c, *, tier, today, capture_addr, cadence=None):
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
    window = _stale_window_days(c, cadence or {})
    # 0.0 = touched today, 1.0 = the engine's clock has run out. Never-touched
    # contacts show a full ring: silence since forever is the stalest state.
    stale = (min(days_since / window, 1.0) if days_since is not None else 1.0)
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
        "stale_pct": round(stale * 100),
        # The tint threshold decided here, where the arithmetic lives, not by
        # a CSS substring hack that cannot tell 8% from 80%.
        "stale_tint": ("due" if stale >= 1.0 else
                       "warming" if stale >= 0.7 else "fresh"),
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
    from .today import _cadence_params

    # One read of the user's cadence overrides for every card's staleness
    # ring, rather than one per contact.
    cadence_overrides = _cadence_params(request.user)
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
    # `cadence.due_actions` sorts its OWN output by (priority, tier, firm
    # name) — never by how long the contact has gone quiet, because it has
    # no opinion on lane order. Once split into these verb-lanes, "Follow
    # Up" alone can hold 80+ people, all the same action, all the same
    # priority, differing only in how overdue they are — and that was the
    # one thing the list wasn't sorted by. Longest-silent-first inside each
    # lane, priority still breaking ties first (a re-ping guarding a real
    # deadline outranks an ordinary follow-up regardless of either one's
    # idle time). Not `_today_sort_key`: that key's leading term is
    # `_today_class`, the Today page's OWN critical/momentum/cold split,
    # which doesn't exist here — the lane a card is in already answers the
    # question that term is for.
    for items in grouped.values():
        items.sort(key=lambda a: (
            a["priority"], -a.get("idle_business_days", 0),
            a["tier"], str(a["firm_name"]),
        ))
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
        adv_met = advocates >= adv_target
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
            "adv_met": adv_met,
            # Socket booleans for the template: [True, False] is "one of two
            # filled". Capped at the target — a third advocate at a
            # two-target firm overfills nothing, it just keeps the ✓.
            "adv_slots": [i < advocates for i in range(adv_target)],
            # What the Coverage Gaps strip already computes for its worst 6
            # firms, extended to all 69: WHERE this firm sits on the warmth
            # ladder, and the one contact who moves it forward. A card used
            # to state status only ("2 Act Now", a bar, a fraction) with no
            # path from reading it to doing something about it — the click
            # target existed for six firms and nowhere else on the board.
            "gap_label": (None if adv_met else
                         coverage.GAP_LABELS[coverage.gap_state(
                             (c.warmth for c in cs), advocates, adv_target)]),
            "lever": None if adv_met else _pick_lever(cs),
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
    # `_pick_lever` — same function every firm card below uses.
    firms_by_id = {uf.firm_id: uf.firm for uf in user_firms}
    for g in gaps:
        firm = firms_by_id.get(g["firm_id"])
        g["slug"] = firm.slug if firm else ""
        g["lever"] = _pick_lever(by_firm_contacts.get(g["firm_id"], []))

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
                    _contact_card(c, tier=tiers_by_firm.get(c.firm_id), today=today,
                                  capture_addr=capture_addr, cadence=cadence_overrides)
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
                    _contact_card(c, tier=tiers_by_firm.get(c.firm_id), today=today,
                                  capture_addr=capture_addr, cadence=cadence_overrides)
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
    # ?quick renders the three-field fast path: the real moment this page
    # serves is "met someone, have ten seconds", and the full form is 11
    # fields of friction against a name you are about to forget. Same form
    # class, same POST, same validation — only the fields SHOWN change, and
    # everything hidden stays editable later on the contact page.
    quick = "quick" in request.GET or request.POST.get("quick") == "1"
    return render(request, "crm/contact_form.html",
                  {"form": form, "mode": "new", "quick": quick})


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


# The bulk verbs the Network board offers over a multi-selection, and the
# past-tense word each reports back. All three are REVERSIBLE, and that is
# the selection criterion, not an accident of what was easy:
#
#   snooze  -> `snoozed_until`, expires by itself in 3 days.
#   park    -> `thread_state="parked"`; the contact leaves the cadence queue
#              but keeps every touch, and logging one more puts them back.
#   archive -> off the board and out of coverage counts, restored in one
#              click from Archived Contacts.
#
# There is deliberately no `delete`. The product has no hard-delete path for
# a contact ANYWHERE — `contact_archived.html` says so in as many words
# ("Nothing is deleted: every touch stays on the record and comes back with
# the person") — and a multi-select is the worst possible place to
# introduce the first one, because the same mis-click that snoozes three
# people would erase eighty-three and their entire correspondence history.
# Archive is what "get rid of these" means here, and it means it safely.
_BULK_VERBS = {
    "snooze": "snoozed for 3 days",
    "park": "taken out of the follow-up queue",
    "archive": "archived",
}


@login_required
@require_POST
def contacts_bulk(request: HttpRequest) -> HttpResponse:
    """Apply one reversible verb to a hand-picked set of contacts.

    Unlike `today_park_all`, which re-derives its ids from the engine, this
    one MUST trust the posted ids — the whole feature is the user choosing
    an arbitrary subset. The safety property is tenancy, not derivation:
    every id is resolved through `Contact.objects.for_user`, so another
    tenant's id is silently absent from the queryset rather than acted on,
    and a stale id from a re-rendered page simply matches nothing.
    """
    verb = (request.POST.get("verb") or "").strip()
    if verb not in _BULK_VERBS:
        return HttpResponse(status=400)

    ids = []
    for raw in request.POST.getlist("ids"):
        try:
            ids.append(int(raw))
        except (TypeError, ValueError):
            continue

    scope = (request.POST.get("scope") or "").strip()
    back = reverse("crm:contact_list") + (f"?scope={quote(scope)}" if scope else "")
    if not ids:
        messages.info(request, "Nothing was selected.")
        return redirect(back)

    rows = list(
        Contact.objects.for_user(request.user)
        .filter(pk__in=ids, archived=False)
        .values_list("id", "name")
    )
    if not rows:
        messages.info(request, "Nothing was selected.")
        return redirect(back)

    if verb == "snooze":
        Contact.objects.for_user(request.user).filter(
            pk__in=[cid for cid, _ in rows]
        ).update(snoozed_until=timezone.now() + timedelta(days=3))
    elif verb == "park":
        # A LOOP over the audited override, not a bulk `.update()` — the
        # same call and the same reasoning as `today_park_all`: parking
        # moves `thread_state`, and only the override path is allowed to,
        # writing one `manual_override` touch per contact so the log never
        # has a gap about who left the queue or when. Slower, and correct.
        for cid, _ in rows:
            services.set_contact_state(
                request.user.id, cid,
                thread_state="parked",
                note="Parked from the Network board (bulk)",
            )
    else:  # archive
        # A plain ORM write, matching `_set_archived`: `archived` is a
        # UI/lifecycle flag, not part of the warmth/thread_state ratchet, so
        # it does not go through the pipeline.
        Contact.objects.for_user(request.user).filter(
            pk__in=[cid for cid, _ in rows]
        ).update(archived=True)

    record_event(
        f"contacts_bulk_{verb}", user=request.user,
        source="network", count=len(rows),
    )
    # Name them when it's a handful, count them when it isn't — a list of
    # eighty-three names is not a confirmation, it's a wall.
    who = (", ".join(name for _, name in rows[:3])
           + (f" and {len(rows) - 3} more" if len(rows) > 3 else ""))
    undo = ("They're in Archived Contacts if you want them back."
            if verb == "archive" else
            "Logging a touch puts anyone back in the queue."
            if verb == "park" else "")
    messages.success(
        request,
        f"{len(rows)} contact{'' if len(rows) == 1 else 's'} "
        f"{_BULK_VERBS[verb]}: {who}. {undo}".strip(),
    )
    return redirect(back)


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
        # Words for the flag: the enums ("no_reply → replied") had leaked
        # into the one sentence whose whole job is celebrating the change.
        "from_state_label": _STATE_LINES.get(before_state, before_state),
        "to_state_label": _STATE_LINES.get(contact.thread_state, contact.thread_state),
    }
    context = _contact_live_context(request, contact, moved=moved)
    return render(request, "crm/_contact_live.html", context)


# ---------------------------------------------------------------------------
# Shared context for the live panel (used by the detail page and the htmx
# fragment, so both render identically).
# ---------------------------------------------------------------------------
# Thread state, said in words. The page used to print the raw enum — a chip
# reading "no_reply" — right next to a warmth chip and a fit-score band that
# both said "cold", three spellings of one fact. One human sentence carries
# the pair; the enums stay in the database where they belong.
_STATE_LINES = {
    "no_reply": "No reply yet",
    "replied": "They replied",
    "chat_scheduled": "A chat is set up",
    "chat_done": "You have chatted",
    "advocate": "In your corner",
    "quiet": "Gone quiet",
    "parked": "Parked",
}

# (warmth, thread_state) pairs where the state sentence already implies the
# warmth — "Replied · They replied" says one thing twice, and the whole point
# of the sentence was replacing three chips that did exactly that.
_STATE_IMPLIES_WARMTH = {
    ("replied", "replied"),
    ("chatted", "chat_done"),
    ("advocate", "advocate"),
}


# Machine bookkeeping prefixes on touch notes. `[gmail:<thread>]` is the
# sync's idempotency marker (how a re-scanned thread knows it is already
# logged) and `[capture:<event>]` is the BCC pipeline's provenance pointer.
# Both are load-bearing IN THE DATABASE and meaningless ON THE PAGE — the
# owner's words: "the user doesn't learn anything". Stripped at display,
# never at rest; the export still carries them raw.
_NOTE_MARKER = re.compile(r"^\[(?:gmail|capture):[^\]]*\]\s*")

# `services.set_contact_state`'s audit trail (pipeline.py's `set_state`)
# writes every manual-override touch's note as "manual override:
# <column>=<value>, ..." optionally followed by " — <human note>". The
# column=value pairs are the same kind of machine bookkeeping as the bracket
# markers above — real for the audit trail, meaningless to a user reading
# their OWN history: "thread_state=chat_done" names a database column, not a
# fact about them, and it duplicated the row's own "Manual override"
# kind_label besides. Confirmed live: James Bai's contact page (id=312)
# rendered "thread_state=chat_done — Correction: ..." verbatim as a History
# entry. Stripped at display, same posture as `_NOTE_MARKER` — only the
# human-authored explanation after the dash (if any) is shown.
_MANUAL_OVERRIDE_PREFIX = re.compile(
    r"^manual override:[^—]*(?:—\s*)?", re.IGNORECASE)


def _display_note(note: str | None) -> str:
    note = _NOTE_MARKER.sub("", note or "").strip()
    return _MANUAL_OVERRIDE_PREFIX.sub("", note).strip()


def _status_line(contact: Contact) -> str:
    state = _STATE_LINES.get(
        contact.thread_state, contact.thread_state.replace("_", " ").capitalize()
    )
    if (contact.warmth, contact.thread_state) in _STATE_IMPLIES_WARMTH:
        return state
    return f"{contact.warmth.capitalize()} · {state}"


def _contact_live_context(
    request: HttpRequest, contact: Contact, *, moved: dict | None = None
) -> dict[str, Any]:
    user = request.user
    now = timezone.now()

    touches = list(
        Touch.objects.for_user(user).filter(contact=contact).order_by("-ts")
    )
    kind_labels = dict(TOUCH_KIND_LABELS)
    channel_labels = dict(CHANNEL_LABELS)
    # Display rows for the history: the raw model rows leak enum spellings
    # ("reply_received · email") into the page. Labels here, once, instead of
    # a template filter per cell.
    touch_rows = [
        {
            "ts": t.ts,
            "kind": t.kind,
            "kind_label": kind_labels.get(t.kind, t.kind.replace("_", " ").capitalize()),
            "channel_label": channel_labels.get(t.channel, t.channel),
            "note": _display_note(t.note),
            "inbound": t.kind in _INBOUND_TOUCH_KINDS,
        }
        for t in touches
    ]
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
        # The Timeline axis's `next_event` is a DB enum (app_open / app_close
        # / insight_deadline — scoring.py's `event_kind`), and _contact_live
        # printed it unfiltered: "app_close in 77d, 2 warm". The same card
        # already says the same event in English two lines above it, because
        # the scorer's reasoning string runs it through a verb map ("closes in
        # ~3 months"), so one panel contradicted itself on a single screen.
        # Labelled here rather than in coverage_domain: the enum is what the
        # scorer matches on, and FIRM_DATE_LABELS is the crm surface's own
        # lowercase vocabulary — the same map the Today strip and the calendar
        # already read, so the three cannot drift.
        nxt = (firm_score.get("axes", {}).get("timeline") or {}).get("next_event")
        if nxt:
            firm_score["axes"]["timeline"]["next_event_label"] = (
                _FIRM_DATE_LABELS.get(nxt, nxt.replace("_", " "))
            )

    # Warmth-meter animation endpoints. On a plain GET both are the current
    # level (no visible motion); on a POST that ratcheted, `from` is the old
    # level so the fill animates the jump.
    to_pct = _warmth_pct(contact.warmth)
    from_pct = _warmth_pct(moved["from_warmth"]) if moved else to_pct

    return {
        "contact": contact,
        "touches": touches,
        "touch_rows": touch_rows,
        "state_line": _status_line(contact),
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


@login_required
@require_POST
def contact_opener(request: HttpRequest, pk: int) -> HttpResponse:
    """Save the contact's opener — the draft their Compose button carries.

    This closes a loop that shipped half-built: Compose has always prefilled
    the email body from `contact.opener`, and the Today card wears a "draft
    ready" badge when one exists — but nothing in the product ever WROTE the
    field, so zero of 129 real contacts had one and the badge could never
    appear. One textarea, saved here, and both ends start working.
    """
    contact = get_object_or_404(Contact.objects.for_user(request.user), pk=pk)
    contact.opener = (request.POST.get("opener") or "").strip()
    contact.save(update_fields=["opener"])
    record_event("opener_saved", user=request.user)
    return render(request, "crm/_contact_live.html",
                  _contact_live_context(request, contact))


@login_required
@require_POST
def contact_ai_brief(request: HttpRequest, pk: int) -> HttpResponse:
    """Draft a coffee-chat prep brief for `contact` from their own history
    (see crm/ai_brief.py). POST, not GET: this is a paid API call once
    ANTHROPIC_API_KEY is set, so it must never fire from a prefetch, a
    browser "Reload," or a crawler following a link — only a deliberate
    click. Renders inline via htmx; the panel states plainly this is
    AI-drafted and unconfigured/failed cases show a plain unavailable
    message rather than an error."""
    contact = get_object_or_404(Contact.objects.for_user(request.user), pk=pk)
    brief = ai_brief.generate_coffee_chat_brief(contact)
    if brief is not None:
        record_event("ai_brief_generated", user=request.user)
    return render(request, "crm/_contact_ai_brief.html",
                  {"contact": contact, "brief": brief, "requested": True})
