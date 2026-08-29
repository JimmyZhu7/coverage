"""The Today engine: the cadence queue, the cockpit, and its actions.

Moved whole from crm/views.py (2026-08-05), which had grown to 1,914 lines
with three pages entangled in one namespace. The public names — `week`,
`today_park_all`, `today_act`, and the tested internals — are re-exported by
crm.views, so URLs, tests, and anything else importing the old paths keep
working unchanged.
"""

from __future__ import annotations

from datetime import date, timedelta
from math import ceil

from django.contrib.auth.decorators import login_required
from django.db.models import Count as models_Count, Max as models_Max, Q
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.utils import timezone
from django.utils.timesince import timesince
from django.views.decorators.http import require_POST

from analytics.events import record_event
from analytics.models import UserOpportunity
from coverage_domain import cadence
from coverage_domain.pipeline import (
    BULK_RECEIVED_KIND, CHANNELS, MANUAL_OVERRIDE_KIND, TOUCH_TRANSITIONS,
)
from directory.classify import TARGET_BUCKETS
from directory.dupes import fold_duplicates
from directory.models import Firm, FirmDate, Opportunity

from . import campaigns, debrief as debrief_svc, recruitment, relevance as rel, services
from .models import (BenchDismissal, CalendarEvent, ChatDebrief, Contact, PlayDismissal, Touch, UserFirm)
from .utils import (
    ACTION_LABELS,
    FIRM_DATE_LABELS as _FIRM_DATE_LABELS,
    TOUCH_KIND_LABELS,
    CHANNEL_LABELS,
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
# runs over the OUTPUT (below), and this kind is exempt from it.
#
# `confirm_chat` used to be in this set too, and that bundling was a bug worn
# as a principle. A re-ping guards an EXTERNAL deadline the user never agreed
# to miss; confirm-chat is a QUESTION ("did it happen?") that the user is
# entitled to answer "ask me tomorrow" — and the card offered Skip and Snooze
# buttons whose click wrote snoozed_until and then re-rendered the exact same
# card, a control that visibly did nothing (reported 2026-08-07, the Cindy So
# card). The buttons now work where dismissal is legitimate, and the one kind
# where it isn't renders no such buttons at all — see `snoozable` below.
_SNOOZE_EXEMPT_ACTIONS = frozenset({"reping"})


def _build_actions(user):
    """The cadence queue, shared by Today and Network: fetch the user's
    contacts/touches/tiers/firm-dates, run `cadence.due_actions`, and dress
    each action for display (label, prose reason, warmth, compose link,
    last-touch evidence, deadline chip).
    Returns (actions, contacts)."""
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
    # People the user has told us arrived through a bulk send that was not
    # their own recruiting — a club panel invitation, an event, a survey. Two
    # queries, both `.for_user`-scoped, and usually zero rows. See
    # `crm/campaigns.py` for the founder's 201-thread ICC merge that made this
    # necessary, and `crm/relevance.py` for what the flag is allowed to do
    # (drop a daily action, and nothing else).
    campaign_excluded_ids = campaigns.excluded_contact_ids(user)

    # Firm metadata: names from the directory, tiers from the user's UserFirm
    # rows. cadence falls back to firm_text / a default tier when a contact's
    # firm isn't covered, so this only needs to be best-effort.
    firm_ids = {c.firm_id for c in contacts if c.firm_id}
    tiers = {
        uf.firm_id: uf.tier
        for uf in UserFirm.objects.for_user(user)
        if uf.firm_id
    }
    firm_names = {}
    firm_tracks = {}
    for fid, name, tracks in Firm.objects.filter(id__in=firm_ids).values_list(
        "id", "name", "tracks"
    ):
        firm_names[fid] = name
        firm_tracks[fid] = tracks or []
    # People who are not part of the user's recruiting AT ALL — the founder's
    # 2026-08-25 rule, decided per PERSON off the row's own text by
    # `crm.recruitment` (see that module for why neither firm tier nor school
    # tie is the test). Computed here from rows already in hand — zero extra
    # queries (`tracks` rides the firm query above) — and carried into the
    # contact dicts as one bool, exactly like `campaign_excluded`, so
    # `crm.relevance` stays a pure function of the dict.
    recruitment_hidden_ids = {
        c.id
        for c in contacts
        if recruitment.contact_verdict(
            c, tiers=tiers, firm_tracks=firm_tracks,
            firm_label=firm_names.get(c.firm_id, ""),
        ).verdict == recruitment.HIDE
    }
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
            # Both read by `crm.relevance` to decide what ASK this person can
            # be given: `role` is the free-text title the recruiting-function
            # markers are matched against, `recruiting_contact` is the
            # student's own three-state answer, which always wins over the
            # text. The engine ignores both — it has no opinion about who a
            # coffee chat is appropriate for, and shouldn't.
            "role": c.role,
            "recruiting_contact": c.recruiting_contact,
            # Read by `crm.relevance.contact_relevance` before anything else it
            # tests. A plain bool rather than the campaign row, so the relevance
            # layer stays a pure function of this dict and testable with no
            # database — same posture as `recruiting_contact` above.
            "campaign_excluded": c.id in campaign_excluded_ids,
            # Same shape, prior question: is this PERSON part of the user's
            # recruiting at all (`crm/recruitment.py`)? Gated in
            # `crm.relevance.contact_relevance` right after the campaign
            # bool, with the same inbound override.
            "recruitment_hidden": c.id in recruitment_hidden_ids,
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
        # The SAME clock-silent set the engine's idle clocks use
        # (`cadence._CLOCK_SILENT_KINDS`: `manual_override` AND
        # `bulk_received`), not just the override kind. Skipping only
        # `manual_override` here let a `bulk_received` touch — their own
        # out-of-office auto-reply landing seconds after a genuine reply,
        # or a newsletter — become the "last real touch": `owed_reply`
        # went False and the person vanished from the queue with a reply
        # still owed (live case: contact who replied Aug 21, masked by a
        # same-day bulk touch). A blast is recorded and visible on the
        # contact; it is simply not the touch this page reasons from.
        if t.kind in cadence._CLOCK_SILENT_KINDS:
            continue
        prev = last_real.get(t.contact_id)
        if prev is None or t.ts > prev.ts:
            last_real[t.contact_id] = t

    # WHAT IS LIVE AT THE FIRMS WHERE THIS STUDENT KNOWS SOMEBODY WARM.
    # One FirmDate query and one Opportunity query over the handful of tiered
    # firms with an advocate or a chatted contact on them — nine, on the
    # founder's live account, against the 54 on his list. Used twice below: to
    # give a keep-warm card a reason instead of a day count, and to advance one
    # that the bare clock has not reached yet.
    warm_firm_ids = {
        c.firm_id for c in contacts
        if c.firm_id in tiers
        and c.warmth in _WARM_UPKEEP_WARMTHS
        and c.thread_state not in ("parked", "quiet")
    }
    openings = rel.firm_openings(user, warm_firm_ids, today) if warm_firm_ids else {}
    actions += _opening_keep_warms(
        contact_dicts, actions, openings, last_real, today, snoozed_ids, firm_meta
    )

    # Deadline chips reuse the engine's own closing-soon index rather than
    # re-deriving one: same confirmed-only bar, same region scoping, same
    # window, so a chip can never disagree with the re-ping that produced it.
    merged = {**cadence.CADENCE_DEFAULTS, **params}
    reping_days = int(merged["pre_deadline_reping_days"])
    closing = cadence._closing_soon(firm_dates, today, reping_days)

    for a in actions:
        c = a["contact"]
        a["label"] = ACTION_LABELS.get(a["action"], a["action"])
        # The age is read from the engine's own `ctx`, not scraped back out of
        # its prose, so the rewrite can never disagree with the number that
        # produced the sentence.
        a["reason"] = _prose_dates(_age_in_days(
            _sentenceize(a.get("reason", "")),
            (a.get("ctx") or {}).get("hours"),
            now=now,
        ), today=today)
        a["warmth_pct"] = _warmth_pct(c.get("warmth", "cold"))
        # Compose surface: the opener seeds the draft body so the weekly list
        # doubles as the place outreach starts (§5).
        a["mailto"] = _mailto(
            c.get("email", ""),
            body=(c.get("opener") or ""),
        )
        # A blank opener means Compose opens an EMPTY email. The card says so
        # rather than letting the click discover it (D).
        a["has_draft"] = bool((c.get("opener") or "").strip())

        last = last_real.get(c["id"])
        a["last_kind"] = kind_labels.get(last.kind, last.kind) if last else None
        a["last_on"] = timezone.localtime(last.ts).date() if last else None
        a["last_business_days"] = (
            cadence.business_days_since(a["last_on"], today) if last else None
        )
        # Inbound movement this week: THEY did something recently (a reply, a
        # chat happening or landing on the calendar). The queue's cards
        # otherwise read as static state; this is the one fact that makes a
        # card urgent in the good direction, so it gets its own pulse.
        a["moved"] = bool(
            last
            and last.kind in ("reply_received", "chat", "chat_scheduled")
            and a["last_business_days"] is not None
            and a["last_business_days"] <= 5
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

        # Do THEY owe you nothing and you owe them? The latest real touch
        # being an inbound kind is exactly that: they wrote (or proposed a
        # time) and nothing of yours has landed since. Same set the pace ring
        # calls inbound, for the same reason.
        a["owed_reply"] = bool(last and last.kind in rel.INBOUND_TOUCH_KINDS)

    actions = _gate_and_rank(actions, tiers, openings)
    return actions, contacts


# The warmths a keep-warm card is ever about: people who have actually met the
# student. `replied` is not one of them — an email back is not a relationship
# to maintain yet, and engine branch 7 has a better thing to say about it.
_WARM_UPKEEP_WARMTHS = ("chatted", "advocate")

# How quiet it has to have been before a live opening at their firm is allowed
# to raise a keep-warm card the bare clock has not reached. Two working weeks:
# long enough that the note does not land on top of the last one, short enough
# that a real deadline is still a deadline when it arrives.
OPENING_MIN_IDLE_DAYS = 10


def _opening_keep_warms(
    contact_dicts, actions, openings, last_real, today, snoozed_ids, firm_meta
):
    """Keep-warm cards raised by something REAL happening at the contact's
    firm, for warm contacts the engine's own clock has not reached yet.

    WHY THE VIEW RAISES THESE AND THE ENGINE CANNOT. `coverage_domain.cadence`
    is handed contacts, touches and firm_dates. It has never seen an
    `Opportunity` row and should not: the board is shared-zone Django data,
    filtered through the student's tracks, markets, class year and eligibility
    — four things the engine takes no arguments for. "A role you could apply
    for opened at their firm this week" is a fact only this layer can state.

    WHY THIS IS NOT SECOND-GUESSING THE STUDENT'S SETTINGS. The keep-warm dial
    (`chatted_touch_min_weeks`) answers "how long is too long to go quiet on
    someone", and it still owns that question completely — a contact with no
    opening waits for it, however warm they are. What it cannot answer is "is
    there something to say today", and that is the only question this function
    asks. Measured on the founder's account: he had turned the dial out to six
    weeks (from a default of three) to stop the hollow prompts, which also
    silenced ten chatted contacts at tier-1 and tier-2 banks — including the
    one whose firm had an investment-banking summer deadline on the board.

    THREE GUARDS, so this can never become the nag it replaces:
      - only a contact the engine produced NO action for, so it can never
        duplicate or pre-empt a thank-you, a re-ping or a follow-up;
      - never someone parked, quiet, archived or snoozed;
      - never inside `OPENING_MIN_IDLE_DAYS` of a real touch.
    """
    busy = {a["contact"]["id"] for a in actions}
    out = []
    for c in contact_dicts:
        if c["id"] in busy or c["id"] in snoozed_ids or c.get("archived"):
            continue
        if c.get("warmth") not in _WARM_UPKEEP_WARMTHS:
            continue
        if c.get("thread_state") in ("parked", "quiet"):
            continue
        opening = openings.get(c.get("firm_id"))
        if not opening:
            continue
        last = last_real.get(c["id"])
        if last is None:
            # No dateable touch at all. The engine's own branches already fire
            # for this case (they treat it as maximally stale), so reaching
            # here would mean the contact was excluded for some other reason —
            # not a gap for this function to fill in behind them.
            continue
        idle = (today - timezone.localtime(last.ts).date()).days
        if idle < OPENING_MIN_IDLE_DAYS:
            continue
        meta = firm_meta.get(c.get("firm_id"), {})
        out.append({
            "contact": c,
            "action": "keep_warm",
            # Overwritten by `crm.relevance.keep_warm_reason` once the opening
            # and the relevance are attached; a placeholder here would be a
            # sentence nobody wrote, so it stays empty instead.
            "reason": "",
            # The same ordinal engine branch 5b gives a keep-warm card, read
            # off the engine rather than chosen here, so the two can never
            # disagree about what a keep-warm is worth.
            "priority": 2,
            "tier": meta.get("tier", 3),
            "firm_name": meta.get("name") or c.get("firm_text") or c.get("firm_id"),
            # These only ever exist for a contact at a DIRECTORY firm (the
            # openings index is keyed by firm_id), so the firm is known by
            # construction — never the "No firm listed" placeholder.
            "firm_known": True,
            "ctx": {"opening": opening["kind"], "days_since": idle},
            "from_opening": True,
        })
    return out


# ---------------------------------------------------------------------------
# The bench (Phase 1, 2026-08-27). Parked contacts are not gone, and a live
# opening at their firm may draw ONE of them back into view per day.
# ---------------------------------------------------------------------------
# THE EVIDENCE. 113 of the founder's 182 live contacts are parked — 102
# proposed by cadence branch 6 and bulk-clicked in one minute on Aug 10, 15
# more the same way on Aug 25. He was clearing a screen, not pruning a
# network: both of his only two advocates, 14 chatted contacts and 13 replied
# contacts went with it, and Katy Chen — Nomura, IB VP, tier 1, already
# chatted, a role at her firm closing Sep 30, the highest expected value in
# his entire dataset — became unreachable by any surface.
#
# THE DIAGNOSIS. Park is not the disease; park doing two jobs is. Parking a
# cold non-replier correctly stops the clock, but "park" ALSO means
# "invisible everywhere", and those are different claims. There is no state
# between "in the cadence" and "gone", so the ordinary outcome of cold
# outreach — silence — funnels everyone into "gone" alike.
#
# THE SPLIT IS THE WHOLE DESIGN, and it runs on warmth, exactly like
# `_opening_keep_warms` above runs on warmth for ACTIVE contacts:
#   - cold-parked: stays dark. `reply_received` already un-parks through the
#     pipeline ratchet (TOUCH_TRANSITIONS sets thread_state on ANY inbound
#     reply, whatever it was before) — this function must never duplicate
#     that job, so it never benches them.
#   - replied-parked: already gets `coverage_domain.cadence` branch 3's
#     confirmed-close re-ping ahead of the branch-4 parked skip — the engine
#     already disagrees with blanket park for them. Nothing here changes
#     that; this function does not touch them either.
#   - chatted/advocate-parked: bench-eligible. Somebody the student actually
#     met, whose file the product parked anyway. A live opening at their firm
#     may raise ONE of them.
#
# WHY THIS SITS BESIDE `_opening_keep_warms` RATHER THAN INSIDE IT. That
# function's three guards ("never someone parked, quiet, archived or
# snoozed") assume an ACTIVE relationship — parked is the one state it
# deliberately excludes. This function exists for exactly the case it
# excludes, so merging the two into one ambiguous branch would blur the line
# the whole design rests on. They share warmth vocabulary
# (`_WARM_UPKEEP_WARMTHS`) and the opening machinery (`crm.relevance
# .firm_openings`) on purpose — same "what's real today" question, answered
# for two disjoint populations.
#
# HARD RULES, so this can never become the 29-card flood the design exists
# to prevent:
#   1. No expiry, no timer, nothing un-parks itself — only a tap
#      (`today_bench_act`'s "restore" verb) does. This function recomputes
#      fresh off current state on every render; the ONLY durable signal is
#      `BenchDismissal`, written by the OTHER tap ("leave").
#   2. At most `BENCH_PLAN_MAX` cards.
#   3. Dismissible per contact + opening (`_opening_signature`), and once
#      dismissed it never returns for that SAME opening — a fresh one (a new
#      deadline, a new posting) is a new question and gets its own tap.
BENCH_PLAN_MAX = 1

# warmth -> the thread_state a bench "bring back" restores. Parking only ever
# writes `thread_state`; it never touches `warmth` (`crm.services
# .set_contact_state` calls are always thread_state-only from the park
# paths), so "restore thread_state from warmth" means putting thread_state
# back at the resting value that warmth level ordinarily carries —
# `crm.utils.WARMTH_ORDER`'s own pairing, and the same one branch 5b's gate
# reasons from (chatted's resting state is chat_done, not the terminal
# `replied` branch 7 owns).
BENCH_RESTORE_STATE = {
    "cold": "no_reply",
    "replied": "replied",
    "chatted": "chat_done",
    "advocate": "advocate",
}


def _opening_signature(opening: dict) -> str:
    """A stable id for ONE opening, built only from fields `firm_openings`
    already reads off a real row (kind/date/title) — no invented id, the
    same "no invented facts" discipline `crm.relevance`'s module docstring
    states. Two different openings at the same firm (a fresh deadline
    superseding an old one, a fresh posting) get two different signatures,
    so a dismissal of one never silences the other."""
    date = opening.get("date")
    return f"{opening.get('kind')}|{date.isoformat() if date else ''}|{opening.get('title') or ''}"


def _opening_bench(user, contacts, actions, today) -> list[dict]:
    """The bench: at most `BENCH_PLAN_MAX` parked chatted/advocate contacts
    with a live reason to come back into view today, ranked the same way the
    rest of the queue is (relevance x relationship x trigger), so the one
    card shown is always the best one available.

    `contacts` is the plain `Contact` rows `_build_actions` already loaded —
    no second contact query. `actions` is that same call's OUTPUT, read only
    for the busy-contact set: a bench card must never sit next to a card the
    engine already drew for the identical person, same guard
    `_opening_keep_warms` runs. The last-real-touch clock is read fresh here
    (`_build_actions`'s own copy is private to that call) over just the small
    parked/chatted-or-advocate slice, never the whole contact book.
    """
    busy = {a["contact"]["id"] for a in actions}
    now = timezone.now()

    eligible = [
        c for c in contacts
        if c.firm_id
        and c.id not in busy
        and c.warmth in _WARM_UPKEEP_WARMTHS
        and c.thread_state == "parked"
        and not (c.snoozed_until and c.snoozed_until > now)
    ]
    if not eligible:
        return []

    # Same two view-layer exclusions `_build_actions` applies before a
    # contact ever reaches the queue: a bulk-send panelist or a person the
    # 2026-08-25 recruitment rule says isn't part of this student's
    # recruiting is not owed a recruiting nudge just because they are also
    # parked and chatted.
    excluded_ids = campaigns.excluded_contact_ids(user)
    eligible = [c for c in eligible if c.id not in excluded_ids]
    if not eligible:
        return []

    tiers = {
        uf.firm_id: uf.tier
        for uf in UserFirm.objects.for_user(user)
        if uf.firm_id
    }
    firm_ids = {c.firm_id for c in eligible}
    firm_rows = {
        fid: (name, tracks)
        for fid, name, tracks in Firm.objects.filter(id__in=firm_ids)
        .values_list("id", "name", "tracks")
    }
    firm_tracks = {fid: (tracks or []) for fid, (_, tracks) in firm_rows.items()}
    eligible = [
        c for c in eligible
        if recruitment.contact_verdict(
            c, tiers=tiers, firm_tracks=firm_tracks,
            firm_label=firm_rows.get(c.firm_id, ("", None))[0],
        ).verdict != recruitment.HIDE
    ]
    if not eligible:
        return []

    openings = rel.firm_openings(user, firm_ids, today)
    dismissed = set(
        BenchDismissal.objects.for_user(user)
        .filter(contact_id__in=[c.id for c in eligible])
        .values_list("contact_id", "opening_signature")
    )

    # Latest REAL touch per eligible contact — same clock-silent exclusion
    # (`manual_override`, `bulk_received`) `_build_actions`' own copy applies,
    # kept as its own small query rather than threading that private dict
    # through a second function's signature. Ordered so the first row seen
    # per contact_id is already the latest.
    last_real: dict[int, Touch] = {}
    for t in (
        Touch.objects.for_user(user)
        .filter(contact_id__in=[c.id for c in eligible])
        .exclude(kind__in=cadence._CLOCK_SILENT_KINDS)
        .order_by("contact_id", "-ts")
    ):
        last_real.setdefault(t.contact_id, t)

    candidates = []
    for c in eligible:
        opening = openings.get(c.firm_id)
        if not opening:
            continue
        last = last_real.get(c.id)
        if last is None:
            # Same honest skip `_opening_keep_warms` makes: no dateable
            # touch on record is not evidence this function can reason from.
            continue
        sig = _opening_signature(opening)
        if (c.id, sig) in dismissed:
            continue
        idle = (today - timezone.localtime(last.ts).date()).days
        tier = tiers.get(c.firm_id)
        score = (
            rel._TIER_WEIGHT.get(tier, rel._TIER_UNRANKED_WEIGHT)
            * rel._RELATIONSHIP_WEIGHT.get(c.warmth, 0.8)
            * rel._OPENING_WEIGHT.get(opening["kind"], rel._NOW_NOTHING)
        )
        candidates.append({
            "contact": {
                "id": c.id,
                "name": c.name,
                "email": c.email,
                "warmth": c.warmth,
                "school_affiliation": c.school_affiliation,
            },
            "firm_name": firm_rows.get(c.firm_id, (c.firm_text or c.firm_id, None))[0],
            "tier": tier,
            "opening_signature": sig,
            "days_since": idle,
            "restore_state": BENCH_RESTORE_STATE.get(c.warmth, "no_reply"),
            "reason": rel._opening_clause(opening),
            "strength_clause": rel._STRENGTH_CLAUSE.get(c.warmth, ""),
            "mailto": _mailto(c.email or "", body=""),
            "_score": score,
        })

    candidates.sort(key=lambda b: -b["_score"])
    return candidates[:BENCH_PLAN_MAX]


def _gate_and_rank(actions: list[dict], tiers: dict, openings: dict) -> list[dict]:
    """Decide who the queue may speak about, what it may ask them for, and in
    what order. Everything here is a VIEW decision — see `crm/relevance.py`'s
    module docstring for why none of it belongs in `coverage_domain.cadence`.

    Three passes, in this order because each one narrows what the next has to
    load:

      1. RELEVANCE. A contact at a tiered firm, a contact who shares the
         student's school, or a contact who wrote and is still waiting on an
         answer. Everyone else is dropped from the QUEUE only — they keep every
         other surface in the product, including the contact book, the Network
         page, search and export. Measured on the founder's account the day
         this landed, 14 of his 16 queue items were people at firms he does not
         target and eight of those were the same follow-up sentence.

      2. THE ASK. A recruiting-process contact never receives "propose a 15-min
         chat". They can still be answered, and they can still carry a real
         process deadline; they cannot be asked to coffee.

      3. WHAT IS HAPPENING NOW. `openings` is already loaded by the caller (it
         needs it to raise the opening-driven keep-warms in the first place),
         so a keep-warm card can name a reason instead of a day count. `ev` is
         then the whole ordering: relevance x relationship x trigger.

    Takes no `user`, and that is worth one line: `tiers` and `openings` are the
    only two things about the student this decision needs, both already loaded
    by the caller. Passing the user as well would put a second, unscoped route
    to their rows inside a function whose whole job is filtering.
    """
    kept: list[dict] = []
    for a in actions:
        c = a["contact"]
        relevance = rel.contact_relevance(c, tiers, owed_reply=a["owed_reply"])
        if relevance is rel.REL_NONE:
            continue
        a["relevance"] = relevance
        a["relevance_tier"] = tiers.get(c.get("firm_id"))
        a["is_recruiting"] = rel.is_recruiting_contact(c)
        kept.append(a)

    out: list[dict] = []
    for a in kept:
        a["opening"] = openings.get(a["contact"].get("firm_id"))

        if a["contact"].get("campaign_excluded"):
            # A campaign-excluded contact is only ever here because they wrote
            # and are still owed an answer (`contact_relevance` returns
            # REL_INBOUND or drops them entirely). The inbound override grants
            # exactly one thing — the answer — so whatever the engine wanted
            # (a re-ping about their firm's deadline, "propose a 15-min chat")
            # is rewritten to the one honest ask. Measured before this branch
            # existed: the ICC panelist who replied about the PANEL kept his
            # priority-0 "Re-ping before you submit" card after the founder
            # had already said the send was not his recruiting.
            #
            # `advance` rather than the engine's own action, so the card logs
            # an `outreach` touch (an email you sent), sits in the momentum
            # lane rather than "Don't lose these", and offers Snooze/Skip —
            # a club note is never snooze-exempt. The deadline chip is
            # cleared for the same reason the action is: the user said this
            # relationship is not their recruiting, and the firm's close date
            # still lives on every surface that IS about their recruiting.
            a["action"] = "advance"
            a["priority"] = 1
            a["closes_on"] = None
            a["label"] = rel.CAMPAIGN_REPLY_LABEL
            a["reason"] = rel.CAMPAIGN_REPLY_REASON
        elif a["contact"].get("recruitment_hidden"):
            # Same one-thing override for a recruitment-hidden contact
            # (`crm/recruitment.py`): they are only ever here because they
            # wrote and are owed an answer, so the engine's own ask — which
            # is by construction a recruiting move — is rewritten to the
            # reply and nothing else. Same mechanics as the campaign branch
            # above, its own sentence, because "you said this send was not
            # your recruiting" and "this person is not part of your
            # recruiting" are different claims and the card must make the
            # right one.
            a["action"] = "advance"
            a["priority"] = 1
            a["closes_on"] = None
            a["label"] = rel.UNRELATED_REPLY_LABEL
            a["reason"] = rel.UNRELATED_REPLY_REASON
        elif a["is_recruiting"] and a["action"] in rel.CHAT_PROPOSING_ACTIONS:
            if a["action"] == "advance":
                if not a["owed_reply"]:
                    # Nothing left to say that isn't a chat ask. The engine
                    # fired branch 7 because the thread is warm and quiet, but
                    # the student has ALREADY written back — measured case, the
                    # founder's national campus-recruiting manager, who had
                    # made her introduction and handed him on a fortnight
                    # earlier. There is no courtesy owed and no deadline
                    # pending, so the honest card is no card.
                    continue
                # They wrote to you; the answer is a reply, not an invitation.
                a["label"] = rel.RECRUITING_REPLY_LABEL
                a["reason"] = rel.RECRUITING_REPLY_REASON
            elif not a["opening"]:
                # "Keeping a recruiter warm" with nothing in the process to
                # talk about is the hollow prompt with a different name on it.
                continue
            else:
                a["label"] = rel.RECRUITING_KEEP_WARM_LABEL
                a["reason"] = rel.keep_warm_reason(a)
        elif a["action"] in ("keep_warm", "maintain"):
            a["reason"] = rel.keep_warm_reason(a)

        a["ev"] = rel.expected_value(a)
        out.append(a)

    return out


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
#
# `BULK_RECEIVED_KIND` is the case the derive-by-default rule warned about:
# it joined `TOUCH_TRANSITIONS` (2026-08-22) without being named inbound
# here, so the ring counted a newsletter LANDING as work the user did —
# live, the founder's ring read 6 done in a week where one of the six was
# an inbound blast. It is somebody else's software's action, excluded by
# name exactly as this comment block demands.
PACE_TOUCH_KINDS = (
    frozenset(TOUCH_TRANSITIONS) - _INBOUND_TOUCH_KINDS - {BULK_RECEIVED_KIND}
)

# Today's plan sizing. Both are reasoned, not measured — revisit against the
# founder's actual clear-rate after a couple of weeks of dogfood.
TODAY_PLAN_MIN = 3    # never plan fewer: below this the page stops building momentum
# Was 12. Twelve was a ceiling on a queue nobody had ranked: it let a whole
# afternoon of cold follow-ups onto one screen and, at 500 contacts, would have
# let forty. With `ev` ordering the list, the top five ARE the five highest-value
# things available, so a longer list buys volume rather than value — and the
# remainder is not lost, it is one click away under "Up next" (`held`). Five is
# a morning's work a student will actually finish, which is the only number a
# habit loop can be built on.
TODAY_PLAN_MAX = 5

# How many keep-warm cards with NO live reason behind them may take a plan slot
# in one day. One, because the honest content of such a card is "this
# relationship is going quiet and nothing external says why today" — worth
# saying once, and a wall of them is the hollow queue the founder described.
# Everything past the first is reachable under "Up next".
QUIET_UPKEEP_PLAN_MAX = 1

# How many pending contact proposals the cockpit renders as cards in one
# view. A rendering guard, not a policy: the remainder stays pending and
# surfaces on the next render as taps clear the lane. Shared with
# `crm.views.proposals_bulk`, whose buttons promise "everyone listed here" —
# the view acts on exactly this slice so the promise and the action can
# never disagree (a first whole-mailbox scan left 52 pending while the lane
# showed 24; "Dismiss all" then buried 28 people no card ever showed).
PROPOSALS_RENDER_CAP = 24


def rendered_proposals_qs(user):
    """Pending `ContactProposal` rows, in the exact order and slice the
    cockpit renders them.

    ONE FUNCTION, CALLED FROM BOTH SIDES OF THE PROMISE. `_cockpit_context`
    (below) and `crm.views.proposals_bulk` used to each write their own
    `.filter(status=PENDING).order_by("created")[:PROPOSALS_RENDER_CAP]`,
    and two copies of an ordering rule are one copy away from disagreeing —
    which is exactly how "Dismiss all" once buried 28 people the lane never
    showed (see `PROPOSALS_RENDER_CAP`'s own note). Routing both call sites
    through this one queryset makes that class of drift impossible rather
    than merely documented against: there is only one ordering rule now
    because there is only one place it is written.

    ORDERED BY EVIDENCE RECENCY, NOT `created`. `created` is when the SCAN
    wrote the row, and a first mailbox sweep writes every proposal it finds
    within the same second — measured, 205 people in one pass on the
    founder's real mailbox. Every row in that batch carries functionally
    the same `created`, so ordering on it is really ordering on whatever
    order the scan happened to iterate threads in, which the student reads
    as random because it is. `occurred_at` is the date of the MAIL that
    produced the proposal (the message's own Date header, parsed by
    `capture.discovery._parse_occurred_at`), so it is the one field on this
    row that answers the question the lane is actually for: who reached out,
    or got reached out to, most recently? A banker who replied last week now
    sorts above a cold send from March; under `created` the two could land
    in either order depending on which thread the scan happened to visit
    first.

    `evidence_kind` and firm tier were considered and rejected as the
    primary key. Both describe WHAT happened, not WHEN, and "worth
    tracking at all" already gated on the finding's strength before this
    row ever existed (`capture.discovery._evidence_kind`) — re-litigating
    strength here would be a second, worse copy of that judgment, and
    ordering strangers by their employer's tier is the firm-alphabet bug
    `_today_sort_key` above already had to fix once, in a different lane.

    `occurred_at` is nullable (a Date header that failed to parse), so a
    missing one falls back to `created` — the row's only other timestamp —
    rather than sorting an unparseable row to either extreme. `-id` breaks
    the rare exact tie and keeps the order stable across re-renders and
    across this call and `proposals_bulk`'s, which is the entire point.
    """
    from django.db.models.functions import Coalesce

    from capture.models import ContactProposal

    return (
        ContactProposal.objects.for_user(user)
        .filter(status=ContactProposal.STATUS_PENDING)
        .annotate(_evidence_date=Coalesce("occurred_at", "created"))
        .order_by("-_evidence_date", "-created", "-id")
    )

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
#
# THE LADDER IS FOUR RUNGS, NOT FIVE, and the merge is the point. It used to
# separate "momentum" (thank_you, advance) from "warm upkeep" (keep_warm,
# maintain) and rank the first above the second unconditionally. That split
# bought nothing and cost the queue its best card:
#
#   - The PAGE never made the distinction. Both classes render into the same
#     "Move it forward" lane (`_TODAY_LANES` below has three entries, not
#     five). The student was shown one lane whose internal order was decided
#     by a boundary they could not see.
#   - `ev` already makes the distinction, better. `_NOW_THANK_YOU` (2.6) and
#     `_NOW_ADVANCE` (1.8) outrank `_NOW_NOTHING` (0.4) on the same contact,
#     so a thank-you still leads a hollow keep-warm without a class saying so
#     — and where the keep-warm has a real external clock behind it
#     (`_OPENING_WEIGHT`, 1.6-2.4) it SHOULD lead, which the old ladder made
#     impossible.
#   - MEASURED, live demo account 2026-08-27: Chloe Park — tier 1, Goldman
#     Sachs, already chatted, a confirmed firm date at her firm, ev 17.28, the
#     highest score in the whole queue — sat in the held list behind four
#     `advance` cards scoring 14.4, 14.4, 10.56 and 7.68, purely because
#     `advance` was class 1 and `keep_warm` was class 2. The founder reported
#     the identical shape on his own queue (Katy Chen, Nomura, ev 14.4, held
#     behind two Bain recruiters at ev 4.32).
#
# WHY THE COLD BOUNDARY STAYS ABSOLUTE, and why the tempting next step — "let
# `ev` outrank class between all the non-critical classes" — is wrong. The
# claim that `ev` now floors cold contacts by itself does not survive
# measurement. A cold-class item is not pinned to `_NOW_COLD_DUE` (1.0): a
# stranger at a firm with a confirmed close gets `_NOW_DEADLINE` (3.0), so the
# cold ceiling is 3.0 x 0.8 x 3.0 = 7.2 against an engaged floor of 0.16.
# Measured on the same demo queue, pure-`ev` ordering put five cold JPMorgan
# strangers (ev 5.28 each, riding their firm's deadline) above an ADVOCATE and
# three contacts who had written back — and, at a daily cap of 5, they would
# have taken the entire plan. That is the 29-cold flood returning through a
# different door. `ev` narrows the gap; it does not close it. The rung that
# has to be structural is the one between a stranger and someone who engaged.
CLASS_CRITICAL = 0   # a real clock the world produced; uncapped, unsnoozed
CLASS_ENGAGED = 1    # they gave you something; ordered among themselves by `ev`
CLASS_COLD = 2       # strangers; never outrank the engaged, whatever they score
CLASS_PARK = 3       # bulk strip, never a plan slot

_TODAY_CLASS: dict[str, int] = {
    "reping": CLASS_CRITICAL, "confirm_chat": CLASS_CRITICAL,
    "thank_you": CLASS_ENGAGED, "advance": CLASS_ENGAGED,
    "keep_warm": CLASS_ENGAGED, "maintain": CLASS_ENGAGED,
    "first_outreach": CLASS_COLD, "follow_up": CLASS_COLD,
    "park": CLASS_PARK,
}
_TODAY_CLASS_DEFAULT = CLASS_COLD

# class -> the lane it renders in. Semantic (what KIND of work this is), not
# an echo of the priority number: the old lanes mapped priority 0/1/2+ to
# Overdue/Due Now/Keep Warm, and since six kinds share priority 1 the live
# page showed one undifferentiated "Due Now" lane of 36 and nothing else.
#
# One rung per lane now, which is the shape this list always implied: the
# ladder above has exactly as many plan-eligible classes as there are lanes
# here, so no ordering decision is made on a boundary the page does not draw.
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
    OVERDUE thank-you gets in here without needing its own class).

    Says only that the card is critical IN KIND. Whether it still earns the
    exemption today is `_stale_critical`'s question — see it."""
    return _today_class(a) == 0 or a["priority"] == 0


# How long a critical prompt with no deadline behind it may go unanswered
# before it stops holding a critical slot. Business days since the last real
# touch, the same clock the card's own copy already prints.
#
# THE MEASURED CASE (founder's live queue, 2026-08-24). Daily cap 3, and all
# three slots went to class 0. Two of them were `confirm_chat` cards — Leo
# Ziqiang Yuan at HSBC and William Zhang at Macquarie — asking the identical
# question, "chat was scheduled 16 business days ago, did it happen?", word
# for word, for the sixteenth consecutive working day. Because criticals are
# never capped, those two occupied 2 of 3 slots permanently, and Katy Chen —
# tier 1, already chatted, a role at her firm closing Sep 30, tied for the
# highest expected value in the entire queue — sat in the held list behind
# them with no day on which she could ever come out. Every morning rendered
# the same three cards.
#
# A question nobody has answered in three working weeks is not urgent, it is
# stuck, and the exemption exists for urgency. 15 business days is three full
# working weeks of silence; branch 2 starts asking at 5, so by then the page
# has put the same sentence in front of the student on roughly ten straight
# working days. Ten refusals is evidence. The card does not go away — it moves
# to the "Still open" strip, which costs no plan slot (see `_cockpit_context`).
CRITICAL_STALE_BUSINESS_DAYS = 15


def _stale_critical(a: dict, today) -> bool:
    """Has this critical card stopped earning its never-capped exemption?

    DECAY IS BY UNANSWERED AGE, NEVER BY A BLANKET TIMER, and the split is the
    whole design:

      - A card with a LIVE deadline behind it never decays by age. Nick
        Tehle's re-ping was 7 business days idle against a confirmed Aug 30
        close, and it would still deserve the top slot at 70: the clock that
        makes it critical belongs to the world, not to how long the student
        has been ignoring us. Only the deadline passing can retire it.
      - A card with NO deadline behind it — `confirm_chat`, an overdue
        thank-you — is a question the product chose to ask. Its whole claim on
        a slot is that it is live, and an unanswered question ages out of that
        claim.

    The passed-deadline test is belt-and-braces and known to be unreachable
    from here today: `cadence._closing_soon` already drops a close before
    `today`, so `closes_on` is either None or in the future by the time an
    action exists. It is written anyway because the invariant it depends on
    lives in another module: if that filter is ever loosened, a card pointing
    at an application that has already closed must not inherit a permanent
    exemption on the strength of a date that is over.

    An UNKNOWN age never decays. `last_business_days` is None when the contact
    carries no dateable touch at all, and the honest reading of that is "we
    cannot say how long this has been unanswered", not "forever" — the same
    care the engine takes with its own `bd is None` branches. It keeps the
    slot; nothing here may retire a prompt on a number it does not have.
    """
    if not _is_critical(a):
        return False
    close = a.get("closes_on")
    if close is not None:
        return close < today
    idle = a.get("last_business_days")
    return idle is not None and idle >= CRITICAL_STALE_BUSINESS_DAYS


def _stale_critical_reason(a: dict) -> str:
    """The copy a stuck prompt gets INSTEAD of repeating itself.

    Two identical cards reading "Chat was scheduled 16 business days ago. Did
    it happen?" render as two separate emergencies, and neither sentence says
    the one thing that is actually true about them: this has been open since
    the first of the month and asking again has not worked. Naming the date
    the thread went quiet turns a nag into a fact the student can act on, and
    it stops the copy pretending each morning is the first time.

    `last_on` is set by `_build_actions` off the same latest-real-touch row
    the idle clock reads, so the date here and the age that demoted the card
    can never disagree.
    """
    since = a.get("last_on")
    when = f" from {rel._on_date(since)}" if since else ""
    if a["action"] == "confirm_chat":
        return f"Still unresolved{when}. Log the chat or reschedule."
    return f"Still open{when}. Nothing has moved since."


def _today_sort_key(a: dict):
    """(class, expected value desc, cadence priority, tier, longest-silent
    first, firm name).

    `ev` is the second term and the one that does most of the work:
    relevance x relationship strength x whether something real is happening
    (`crm.relevance.expected_value`).

    CLASS IS STILL FIRST, BUT IT IS NARROW NOW — three plan-eligible rungs
    (`CLASS_CRITICAL`, `CLASS_ENGAGED`, `CLASS_COLD`), and each survives on a
    claim `ev` cannot make. Everything finer than that is `ev`'s to decide.

      - CRITICAL over everything: a confirmed deadline or a dying chat thread
        is time-critical whatever it scores, and it is exempt from the daily
        cap and from Snooze besides (`_is_critical`, `_stale_critical`). That
        exemption is a different kind of statement from a score, so it needs a
        rung rather than a number. Unchanged.
      - ENGAGED over COLD: a stranger does not outrank someone who engaged
        with you, whatever the letterhead — the inversion this key was written
        for. `ev` narrows that gap but does not close it (see `_TODAY_CLASS`
        for the measurement: a cold contact riding their firm's deadline
        scores 5.28 and buries an advocate at 1.08), so the rung stays.
      - INSIDE `ENGAGED`, `ev` DECIDES. This is the change. A thank-you, a
        reply to chase, a keep-warm on somebody whose firm has a role closing
        in three weeks — these are all "this person gave you something, what
        is worth doing about it today", and that is the exact question `ev`
        answers, with the external clock already in it (`_OPENING_WEIGHT`).
        The old ladder answered it with the action's NAME instead, which is
        how the highest-scoring card in the queue ended up held behind cards
        scoring a quarter as much: `advance` sorted above `keep_warm` before
        either score was read. An action string is not evidence about today.

    Tier still breaks ties INSIDE a class — it just no longer outranks the
    relationship. Firm name is last and exists only to make the order stable
    across renders.

    WHAT STILL STOPS A WALL OF KEEP-WARMS, since they can now reach the top of
    the engaged rung: nothing here — and deliberately, because it is not an
    ordering question. `_cockpit_context`'s `QUIET_UPKEEP_PLAN_MAX` caps how
    many keep-warms with NO live reason behind them take a plan slot, and it
    keys on the reason (`opening`) rather than the rank, so a card that climbs
    on a real opening is exactly the card it lets through."""
    return (
        _today_class(a),
        -a.get("ev", 0.0),
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
               # `contact__firm`, not just `contact`: _cockpit.html renders
               # `{% if p.contact.firm %}{{ p.contact.firm.name }}` for every
               # row _chat_prep builds out of this list, so stopping the join
               # at `contact` left one firm SELECT per scheduled chat.
               .select_related("contact", "contact__firm")
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
        # These rows reach the same `p.contact.firm` render path as the
        # CalendarEvent branch above — see its comment.
        .select_related("firm")
        .annotate(
            last_ts=models_Max(
                "touches__ts",
                # The engine's own clock-silent set (manual_override AND
                # bulk_received) — a blast must not refresh this clock
                # any more than it resets the cadence's. See the
                # `last_real` loop in `_build_actions`.
                filter=~Q(touches__kind__in=list(cadence._CLOCK_SILENT_KINDS)),
            )
        )
    )
    for c in untimed:
        if not c.last_ts or c.id in seen_contacts:
            continue
        set_up = timezone.localtime(c.last_ts).date()
        if cadence.business_days_since(set_up, today) > 4:
            continue
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


def _new_at_your_firms(user, limit=5) -> dict:
    """Open campus roles that appeared at the user's TARGET firms this week.

    The "what changed" question, answered from data the product already
    records: `first_seen` is Coverage's own clock (when the row entered our
    database — the honest wording the feed's cards already use), and the
    target list is the survey's UserFirm rows. No targets means no card, not
    an empty card: a rail slot with nothing to say should not spend the
    pixels saying it.
    """
    from crm.models import UserFirm
    from directory.classify import TARGET_BUCKETS, TRACK_LABELS
    from directory.models import Opportunity
    from directory.recommend import (
        role_matches_level,
        role_matches_regions,
        role_matches_tracks,
    )
    from directory.views import _eligibility, _eligibility_profile

    firm_ids = list(UserFirm.objects.for_user(user).values_list("firm_id", flat=True))
    if not firm_ids:
        return {"count": 0, "roles": [], "total_new": 0, "track_label": ""}
    since = timezone.now() - timedelta(days=7)

    # Exclude board DEBUTS: when a firm's oldest row is itself inside the
    # window, the firm just joined Coverage — every posting it has is "new
    # to us" and none of it is news about the FIRM. Measured the day this
    # card was built: two connectors wired that week made the count 242,
    # which is a changelog about Coverage wearing the clothes of a changelog
    # about the market. Same trap as the feed's bulk-import "New" badge, and
    # the same cure: first_seen is our clock, so say things it can honestly
    # support.
    from django.db.models import Min

    debut = {
        row["firm_id"]
        for row in Opportunity.objects.filter(firm_id__in=firm_ids)
        .values("firm_id").annotate(oldest=Min("first_seen"))
        if row["oldest"] and row["oldest"] >= since
    }
    # NOT A ROLE THE STUDENT ALREADY WAVED AWAY. "Not for me" is a decision
    # about the role, not about the surface it was shown on, and this card
    # was the last one still arguing with it: a role dismissed in the feed on
    # Monday came back on Tuesday as news from the firm. Same exclusion its
    # sibling `assistant.situation._new_role_events` already applies to the
    # identical question.
    dismissed_ids = set(
        UserOpportunity.objects.for_user(user)
        .filter(dismissed=True)
        .values_list("opportunity_id", flat=True)
    )
    qs = (Opportunity.objects
          .filter(status="open", bucket__in=TARGET_BUCKETS,
                  firm_id__in=[f for f in firm_ids if f not in debut],
                  first_seen__gte=since)
          .select_related("firm").order_by("-first_seen"))
    if dismissed_ids:
        qs = qs.exclude(id__in=dismissed_ids)
    # The same repeat-listing problem the Opportunities feed already solves
    # (directory.dupes.fold_duplicates): a board scraped twice in one week
    # posts the same requisition twice, and this card showed both — J.P.
    # Morgan's Shanghai "Find Your Fit" appearing back to back in a 5-role
    # list is a copy, not two roles. Folded before counting too, so "321"
    # isn't itself inflated by the same duplicates the list then hides.
    rows, _folded = fold_duplicates(qs)

    # RELEVANT TO WHERE, AND WHEN. Two axes the FIRM-only query is blind to:
    # a Pune, India ops role and a full-time "New Associate" programme both
    # reached a US/HK IB-track sophomore's day-one brief this way — right
    # firm, wrong market, wrong rung of the ladder. Both run in memory over
    # the same already-loaded rows, so they cost no query.
    rows = [o for o in rows if role_matches_regions(o.region, user.regions)]
    rows = [
        o for o in rows
        if role_matches_level(o.bucket, o.class_year_derived,
                               user.target_cycles, user.class_year)
    ]
    # And never a role the student's own stated facts rule OUT entirely (a
    # wrong stated class year, a market that won't sponsor them) — the same
    # blocking verdict `directory.views._eligibility` already issues for
    # Picked-for-you, applied here rather than duplicated.
    elig_profile = _eligibility_profile(user)
    if elig_profile:
        rows = [
            o for o in rows
            if not (lambda v: v and v["blocking"])(_eligibility(o, elig_profile))
        ]

    # RELEVANT TO WHAT THEY RECRUIT FOR — applied LAST, on purpose, so the
    # count either side of it is a fact worth stating.
    #
    # `role_matches_tracks` is an allowlist: a role has to NAME one of the
    # student's tracks to be called news. That is a deliberately hard bar and
    # it will often leave nothing (see the function's own docstring for the
    # 2-of-33 measurement that forced it). The temptation at that point is to
    # loosen the filter until the card has something in it. The honest move is
    # the opposite: keep the bar and say what happened.
    #
    # So the card gets BOTH numbers. `total_new` is everything that is genuinely
    # new at the student's firms, in their market, at their rung, that they are
    # eligible for — the whole truth about what moved this week. `count` is the
    # subset that names their track. When they differ and `count` is zero, the
    # template says "31 new roles at your firms, none in investment banking",
    # which is a real answer; silently rendering nothing would read as a broken
    # card, and padding it with Engineering roles would be the original bug.
    relevant = [o for o in rows if role_matches_tracks(o.title, user.tracks)]
    total_new = len(rows)
    rows = relevant

    # ONE PER FIRM in the displayed list — `count` stays the true total.
    # A firm's own campus recruiting team routinely posts a whole batch of
    # reqs the same week (CICC alone posted three in one run), and without
    # this the batch fills every slot the card has, reading as "CICC, CICC,
    # CICC" instead of naming the breadth of what's actually moving. Same
    # fix as `assistant.situation._new_role_events` for the identical trap.
    seen_firms: set[int] = set()
    roles = []
    for o in rows:
        if o.firm_id in seen_firms:
            continue
        seen_firms.add(o.firm_id)
        roles.append({"title": o.title, "firm": o.firm.name, "id": o.id,
                      "slug": o.firm.slug, "location": o.location})
        if len(roles) >= limit:
            break

    # `track_label` is the one the student would recognise from Settings, not
    # the chip abbreviation — "none in investment banking" reads; "none in IB"
    # reads like a filter state. Only ever set when there is exactly one thing
    # to name; with two or more tracks the template says "none in the tracks
    # you recruit for" rather than listing them.
    tracks = [t for t in (user.tracks or []) if t in TRACK_LABELS]
    return {
        "count": len(rows),
        "roles": roles,
        "total_new": total_new,
        "track_label": TRACK_LABELS[tracks[0]].lower() if len(tracks) == 1 else "",
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
            # The raw kind, alongside the human `label` above. `_plays` keys
            # its dismissal tuple on this (plus firm + date) rather than on
            # the label, so a copy-editing pass on `_FIRM_DATE_LABELS` can
            # never resurrect or re-dismiss a fact it didn't touch.
            "event_kind": fd.event_kind,
            "date": fd.date,
            "days": days,
            "when": "today" if days == 0 else ("1d" if days == 1 else f"{days}d"),
            # Mirrors the cadence engine's own urgency bar, so the colour here
            # and the lane a contact lands in cannot disagree.
            "urgent": days <= 7,
        })
    return out


# ---------------------------------------------------------------------------
# Plays: a dated world fact joined to the student's own people at that firm.
# ---------------------------------------------------------------------------
# THE GAP THIS CLOSES. The founder's Today page rendered zero cards the week
# he sent ~50 personalised coffee-chat requests: nothing was DUE, and the
# cadence queue is the page's only content source. Meanwhile the page already
# knew J.P. Morgan closes in 3 days and he has 6 people there, and Goldman's
# insight programme opens Sep 1 with 5 of his new batch at Goldman. The
# ingredients existed on two different rails — `_next_deadlines`'s dated
# facts, and the student's own contacts — and never met. This is the join,
# and only the join: no prose, no model call, nothing rendered that isn't
# read off a row.
#
# Confirmed dates only, unchanged from `_next_deadlines`'s own bar — a
# rumoured date is not a play, it is the countdown bug this codebase already
# refused to ship once.
PLAYS_MAX = 3
# How many of the soonest confirmed dates to pull before dismissal/one-per-
# firm narrowing. Wider than PLAYS_MAX so a dismissed fact or two still
# leaves a full three to show; `_next_deadlines` is one indexed query so the
# extra rows cost nothing extra to fetch.
_PLAYS_CANDIDATE_POOL = 20

# The contact-state groups a play's breakdown may name, warm-to-cold. Read
# off `Contact.warmth` for NON-archived contacts only — `parked` (below) is
# the archived count and is reported separately so a person's warmth at the
# moment they were parked never gets counted twice.
_PLAY_WARMTH_GROUPS = ("replied", "chatted", "advocate", "cold")
_PLAY_WARMTH_LABELS = {
    "replied": "replied", "chatted": "chatted", "advocate": "advocate",
    "cold": "cold",
}


def _plays(user, today) -> list[dict]:
    """At most `PLAYS_MAX` cards: a confirmed, dated fact about a firm, joined
    to a plain count of this student's own contacts there.

    JOIN, NOT RE-DERIVE. The fact side is exactly `_next_deadlines`'s own
    output — this function adds no opinion about what counts as confirmed or
    upcoming. The people side is a straight `Contact` count by firm and
    warmth/archived state. Nothing here computes relevance, ranks a
    relationship, or writes a sentence a human didn't already establish by
    filling in a row.
    """
    candidates = _next_deadlines(user, today, limit=_PLAYS_CANDIDATE_POOL)
    if not candidates:
        return []

    dismissed = set(
        PlayDismissal.objects.for_user(user)
        .values_list("firm_id", "event_kind", "date")
    )

    # One play per firm. `_next_deadlines` is already sorted soonest-date
    # first, so the first row seen per firm is that firm's nearest confirmed
    # date — the same "earliest wins" rule the deadlines rail itself applies.
    seen_firms: set[int] = set()
    facts = []
    for d in candidates:
        firm_id = d["firm"].id
        if firm_id in seen_firms:
            continue
        seen_firms.add(firm_id)
        # THE ANTI-NAG GATE. Filtered out here, before the cap, so a
        # dismissed fact never occupies one of the at-most-3 slots and never
        # blocks a real one behind it.
        if (firm_id, d["event_kind"], d["date"]) in dismissed:
            continue
        facts.append(d)
        if len(facts) >= PLAYS_MAX:
            break
    if not facts:
        return []

    firm_ids = [d["firm"].id for d in facts]
    by_firm: dict[int, list] = {fid: [] for fid in firm_ids}
    for c in (
        Contact.objects.for_user(user)
        .filter(firm_id__in=firm_ids)
        .only("id", "firm_id", "warmth", "archived")
    ):
        by_firm[c.firm_id].append(c)

    plays = []
    for d in facts:
        firm = d["firm"]
        cs = by_firm.get(firm.id, [])
        live = [c for c in cs if not c.archived]
        parked = [c for c in cs if c.archived]

        breakdown = []
        accounted = 0
        for w in _PLAY_WARMTH_GROUPS:
            n = sum(1 for c in live if c.warmth == w)
            if n:
                breakdown.append(
                    {"key": w, "label": _PLAY_WARMTH_LABELS[w], "count": n}
                )
                accounted += n
        # `warmth` is a plain, unconstrained column (see Contact.warmth) — a
        # value outside the known ladder still has to be counted somewhere,
        # or the card's own arithmetic would drift from the roster behind
        # it, which is rule 4: counts must equal what renders.
        other = len(live) - accounted
        if other:
            breakdown.append({"key": "other", "label": "other", "count": other})
        if parked:
            breakdown.append(
                {"key": "parked", "label": "parked", "count": len(parked)}
            )

        plays.append({
            "firm": firm,
            "label": d["label"],
            "event_kind": d["event_kind"],
            "date": d["date"],
            "when": d["when"],
            "urgent": d["urgent"],
            "breakdown": breakdown,
            # The single source both the card and this function's own tests
            # read — never resummed from `breakdown` a second time.
            "total": len(cs),
            # WHAT THE "View contacts" LINK CAN ACTUALLY SHOW. That button
            # points at `directory:firm_detail`, whose "My Network here"
            # section excludes archived contacts (directory.views.
            # _my_network_at) — the same exclusion the Network board itself
            # applies. `total` above counts parked people INTO the card's
            # headline sentence on purpose (a parked relationship is still a
            # relationship you have at this firm, worth knowing about), but
            # a sentence that says "3 contacts here" over a button that
            # shows 0 is the counts-must-equal-what-renders bug this repo
            # has now shipped five times. `live_total` is what the button's
            # destination will actually display, so the template can choose
            # the right destination instead of the one destination this
            # lane always used to offer. The parked count itself gets its
            # own link in `_cockpit.html` — to `crm:contact_archived`,
            # scoped to this firm — rather than being folded silently into
            # a number the firm page cannot back up.
            "live_total": len(live),
            # A firm with a live date and nobody there is still a play: the
            # card reads as a sourcing prompt rather than a roster.
            "sourcing": not cs,
        })
    return plays


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
        # _cockpit.html's waiting list renders `{% if c.firm %}{{ c.firm.name }}`
        # per person, so without this each of the `limit` rows cost its own
        # firm SELECT — the single largest N+1 on the Today page.
        .select_related("firm")
        .annotate(
            last_ts=models_Max(
                "touches__ts",
                # The engine's own clock-silent set (manual_override AND
                # bulk_received) — a blast must not refresh this clock
                # any more than it resets the cadence's. See the
                # `last_real` loop in `_build_actions`.
                filter=~Q(touches__kind__in=list(cadence._CLOCK_SILENT_KINDS)),
            )
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
    rows = [
        r for r in schedule
        if r["is_today"] and r["timed"] and r["contact"]
    ]
    if not rows:
        return []

    # Both lookups below used to run per chat (two SELECTs each), so a day
    # with several chats paid a query per chat for data that is one indexed
    # read in bulk. Batched into exactly two queries regardless of how many
    # chats the day holds — "assembly, not new data", as the docstring says.
    contact_ids = [r["contact"].id for r in rows]
    firm_ids = {r["contact"].firm_id for r in rows if r["contact"].firm_id}

    # Latest non-dismissed debrief per contact. Ordered oldest-first so the
    # last write into the dict is the newest row, matching the per-contact
    # `.order_by("-created").first()` this replaces.
    learned_by_contact: dict[int, str] = {}
    for d in (ChatDebrief.objects.for_user(user)
              .filter(contact_id__in=contact_ids, dismissed=False)
              .exclude(learned="")
              .order_by("created")
              .only("contact_id", "learned")):
        learned_by_contact[d.contact_id] = d.learned

    # Soonest confirmed date per firm. Ordered latest-first for the same
    # reason, mirroring `.order_by("date").first()`.
    firm_date_by_firm: dict[int, FirmDate] = {}
    if firm_ids:
        for fd in (FirmDate.objects
                   .filter(firm_id__in=firm_ids, date__gte=today, confidence=1.0)
                   .order_by("-date")):
            firm_date_by_firm[fd.firm_id] = fd

    out = []
    for row in rows:
        c = row["contact"]
        learned = learned_by_contact.get(c.id, "")
        firm_date = firm_date_by_firm.get(c.firm_id) if c.firm_id else None
        out.append({
            "contact": c,
            "at": row["at"],
            "when": row["when"],
            "title": row["title"],
            "learned": learned,
            "firm_date": firm_date,
            "firm_date_days": (firm_date.date - today).days if firm_date else None,
            "firm_date_label": (
                _FIRM_DATE_LABELS.get(firm_date.event_kind,
                                      firm_date.event_kind.replace("_", " "))
                if firm_date else ""
            ),
        })
    return out


# ---------------------------------------------------------------------------
# Day-one seeds — the queue's answer when it has nothing of its own to say.
# ---------------------------------------------------------------------------
# THE HOLE THESE FILL. After onboarding with zero or one contact, Today said
# "You're all caught up." With ONE contact just touched, cadence's follow-up
# window puts the next action six business days out, so the page stays silent
# for a week — at exactly the moment a new student most needs a push. "Caught
# up" over an empty network is false comfort, and the queue is the habit loop:
# it going dark on day one is the product failing at its own premise.
#
# WHAT A SEED IS, AND IS NOT. A seed is DERIVED FROM STATE on every render and
# stored nowhere. There is no seed table, no "starter task" row, no completion
# flag — a seed exists precisely as long as the condition that justifies it is
# still true, and disappears the render after it stops being true. That is the
# whole design: fake tasks that need dismissing are a second queue, and a
# second queue on a page whose pitch is "this is more trustworthy than your
# spreadsheet" is the one thing it cannot afford.
#
# Every seed also has to be TRUE. "Connect Gmail" is offered only where Gmail
# Live is actually configured on this deploy (`gmail_live.is_configured()`) —
# a dark deploy must never advertise a button that goes nowhere, the same rule
# Settings' own Gmail card already follows.
#
# THE TWO SILENCES. An empty queue means one of two opposite things, and the
# whole trigger rule is telling them apart:
#
#   nothing to do because you have nothing yet  -> seeds
#   nothing to do because you handled it        -> "You're all caught up."
#
# A student with thirty contacts who has snoozed every one of them is in the
# second case: they read the queue and made a decision about every row in it.
# Answering that with "Add 3 people at Goldman Sachs" overwrites an earned
# message with a beginner's one — the same false-comfort failure as the
# original bug, just pointed the other way. So the network size is half the
# rule and the silence is the other half; neither alone is honest.
SEED_MAX = 3               # never more: this is a nudge, not a curriculum
SEED_FIRM_TARGET = 3       # contacts at a target firm before it stops asking
SEED_FIRM_SLOTS = 2        # at most two firm seeds, so the list isn't one note
# Live contacts below which an account is still being BUILT rather than run.
# Under five people there is no network for an empty queue to be an
# achievement over; at five and up, silence is something the student did.
SEED_NETWORK_FLOOR = 5


def _seed_firm_dates(firm_ids: list[int], today) -> dict[int, dict]:
    """The soonest REAL date per firm, for the seed's because-line.

    Two sources, in order of authority: a confirmed `FirmDate` (confidence
    1.0 — the same bar the cadence engine and the Deadlines rail hold, so a
    rumour never becomes a countdown), and failing that the soonest deadline
    on an open campus role at that firm.

    Both are one bulk query over the (at most `SEED_FIRM_SLOTS`) candidate
    firms, ordered latest-first so the last write into the dict is the
    earliest date — the same batching pattern `_chat_prep` uses.
    """
    if not firm_ids:
        return {}
    out: dict[int, dict] = {}
    for fd in (FirmDate.objects
               .filter(firm_id__in=firm_ids, date__gte=today, confidence=1.0)
               .order_by("-date")):
        out[fd.firm_id] = {
            "label": _FIRM_DATE_LABELS.get(
                fd.event_kind, fd.event_kind.replace("_", " ")),
            "date": fd.date,
            "days": (fd.date - today).days,
        }

    missing = [fid for fid in firm_ids if fid not in out]
    if missing:
        for o in (Opportunity.objects
                  .filter(firm_id__in=missing, status="open",
                          bucket__in=TARGET_BUCKETS, deadline__gte=today)
                  .order_by("-deadline")
                  .only("firm_id", "deadline")):
            out[o.firm_id] = {
                "label": "Applications close",
                "date": o.deadline,
                "days": (o.deadline - today).days,
            }
    return out


def _starter_seeds(user, today) -> list[dict]:
    """Concrete first moves, derived from the student's OWN choices.

    Only ever called for a queue with nothing to say AND a network still
    being built (see the gate in `_cockpit_context`), so an account that is
    merely quiet today pays none of these queries at all. Ordered by what
    unblocks what: no target firms means nothing else can be computed, so it
    goes first; the add-people seeds are the actual habit; import and Gmail
    are accelerants; the track seed is the quietest because a wrong track
    only mis-sorts a feed. Capped at SEED_MAX.

    Each entry is `{key, title, why, cta, href}` — a real destination that
    already exists, never a placeholder.
    """
    seeds: list[dict] = []
    live = Contact.objects.for_user(user).filter(archived=False)

    targets = list(
        UserFirm.objects.for_user(user)
        .exclude(firm_id=None)
        .select_related("firm")
    )

    if not targets:
        # The unfinished-onboarding banner (week.html) already says exactly
        # this, with the same link, whenever `onboarded_at` is NULL. Two
        # copies of one instruction on one screen reads as a bug, so this
        # seed only speaks for the account that FINISHED the wizard and
        # still has no firms — the case nothing else on the page covers.
        if user.onboarded_at is not None:
            seeds.append({
                "key": "firms",
                "title": "Pick your target firms",
                "why": "Coverage builds the queue from the firms you're chasing. "
                       "It doesn't know any yet.",
                "cta": "Pick firms",
                "href": f"{reverse('accounts:settings')}#firms",
            })
    else:
        counts = {
            row["firm_id"]: row["n"]
            for row in (live.exclude(firm_id=None)
                        .values("firm_id")
                        .annotate(n=models_Count("id")))
        }
        thin = [uf for uf in targets
                if counts.get(uf.firm_id, 0) < SEED_FIRM_TARGET]
        # Highest tier first (tier 1 is the firm they care most about), then
        # emptiest, then name so the order is stable across renders.
        thin.sort(key=lambda uf: (
            uf.tier if uf.tier is not None else 9,
            counts.get(uf.firm_id, 0),
            uf.firm.name,
        ))
        thin = thin[:SEED_FIRM_SLOTS]
        dates = _seed_firm_dates([uf.firm_id for uf in thin], today)
        for uf in thin:
            have = counts.get(uf.firm_id, 0)
            need = SEED_FIRM_TARGET - have
            name = uf.firm.name
            close = dates.get(uf.firm_id)
            if close:
                # A real, dated reason to move today. `days` is the honest
                # distance, not a rounded "soon".
                when = "today" if close["days"] == 0 else (
                    "tomorrow" if close["days"] == 1 else f"{close['days']} days")
                why = f"{close['label']} {close['date']:%b %-d}. That's {when}."
            elif have:
                why = f"{have} there so far. Three is where a firm starts to know you."
            else:
                why = "Nobody there yet. Three is where a firm starts to know you."
            seeds.append({
                "key": f"firm-{uf.firm_id}",
                "title": (f"Add {need} people at {name}" if have == 0
                          else f"Add {need} more at {name}"),
                "why": why,
                "cta": "Add someone",
                "href": (f"{reverse('crm:contact_new')}"
                         f"?firm={uf.firm.slug}&quick=1"),
            })

    # AHEAD OF THE CSV SEED, and that order is the point (2026-08-27). This
    # block used to sit last, below `track` — with `SEED_MAX` at 3 and a
    # brand-new account offering firms/import/track, it was truncated away
    # on exactly the render it exists for. Verified on a fresh account: the
    # page said "Start here 3" and Connect Gmail was not one of the three.
    #
    # It leads now because of what connecting actually does on an empty
    # account. The first-connect pass sweeps six months of the student's own
    # SENT mail and proposes everyone they wrote to at a directory firm
    # (capture/gmail_live.py) — evidence they already have, versus a
    # spreadsheet they have not written. "Import first" was only ever right
    # while the historical pass searched per-contact and could find nobody
    # new.
    #
    # NEVER promised on a deploy where it is dark. `is_configured()` is the
    # same runtime gate every gmail_live entry point holds and the same one
    # Settings' own card branches on — a seed offering a connection this
    # install cannot make is the exact class of over-claim this page exists
    # to avoid.
    #
    # Imported HERE, not at module scope: `capture.gmail_live` pulls in the
    # Google API client stack and imports `crm.models` on the way, and this
    # module is imported by `crm.views` at startup. A local import keeps that
    # weight off every process that never renders a silent queue, and keeps
    # the crm -> capture -> crm loop from having to exist at all.
    from capture import gmail_live
    from capture.models import GmailConnection

    if len(seeds) < SEED_MAX and gmail_live.is_configured():
        if not GmailConnection.objects.for_user(user).exists():
            seeds.append({
                "key": "gmail",
                "title": "Connect Gmail",
                "why": "Already emailing people? Coverage reads six months of "
                       "your sent mail once and offers whoever you wrote to "
                       "at a firm on your board. Nothing is added until you "
                       "tap. After that, replies log themselves.",
                "cta": "Connect Gmail",
                "href": f"{reverse('accounts:settings')}#gmail-live",
            })

    if len(seeds) < SEED_MAX:
        # The "small enough that one paste changes it" half of this is the
        # gate's job (SEED_NETWORK_FLOOR) — nothing here runs above it. What
        # is left to check is whether they already did it: `source="import"`
        # is written by accounts.services.import_contacts, so a user who has
        # run it once never sees this again.
        if not live.filter(source="import").exists():
            seeds.append({
                "key": "import",
                "title": "Import your contacts",
                "why": "Already keeping a list? Bring the spreadsheet in and the "
                       "cadence starts on all of it at once.",
                "cta": "Import a CSV",
                "href": reverse("accounts:import"),
            })

    if len(seeds) < SEED_MAX and not user.tracks:
        seeds.append({
            "key": "track",
            "title": "Set your track",
            "why": "IB, markets, PE. Pick one and the roles and news on this "
                   "page narrow to it.",
            "cta": "Set your track",
            "href": f"{reverse('accounts:settings')}#profile",
        })

    return seeds[:SEED_MAX]


# ---------------------------------------------------------------------------
# The quiet-day forecast — Phase 1 of the "quiet day is a statement, not an
# absence" work. A page with nothing due says what is coming and when,
# instead of rendering a bare "you're all caught up" or, worse, an
# achievement banner it has no evidence for (see `_cockpit_context`'s
# `quiet` gate below).
# ---------------------------------------------------------------------------
def _next_wave(user, today) -> dict | None:
    """The next date a batch of cold, no-reply follow-ups comes due, and how
    many. The forecast half of the quiet-day line.

    Mirrors `cadence.due_actions`' branch 6 (cold / no_reply cadence)
    exactly, using its own `business_days_since` and the user's own
    `followup_after_business_days` — never a parallel calendar guess, so
    this can never name a date the engine itself would disagree with once
    that date arrives. Two contacts branch 6 would route elsewhere are
    excluded up front for the same reason: a contact already at
    `max_cold_touches` outbound touches gets `park`, not another
    `follow_up`, and a contact with no dateable touch on record reads as
    already due (branch 6 treats a missing date as "definitely stale
    enough"), so it belongs in TODAY's queue, not a future wave.

    Also excludes campaign-excluded and recruitment-hidden contacts —
    `crm.relevance.contact_relevance` drops a not-yet-replied contact in
    either set entirely (`REL_NONE`) once their follow-up comes due, so
    counting them here would forecast a wave bigger than the one that
    actually renders on the day it lands ("counts equal what renders").

    Returns `{"date": date, "count": int}` for the earliest such date, or
    `None` when there is nothing pending to forecast (no cold/no_reply
    contact with an outbound touch under the park threshold)."""
    params = _cadence_params(user)
    merged = {**cadence.CADENCE_DEFAULTS, **params}
    followup_bd = int(merged["followup_after_business_days"])
    max_cold = int(merged["max_cold_touches"])

    contacts = list(
        Contact.objects.for_user(user)
        .filter(archived=False, warmth="cold", thread_state="no_reply")
    )
    if not contacts:
        return None
    excluded_ids = campaigns.excluded_contact_ids(user) | recruitment.hidden_contact_ids(user)
    contacts = [c for c in contacts if c.id not in excluded_ids]
    if not contacts:
        return None

    touches = Touch.objects.for_user(user).filter(
        contact_id__in=[c.id for c in contacts]
    )
    by_contact: dict[int, list[Touch]] = {}
    for t in touches:
        by_contact.setdefault(t.contact_id, []).append(t)

    forecast: dict = {}
    for c in contacts:
        ctouches = sorted(by_contact.get(c.id, ()), key=lambda t: t.ts)
        real = [t for t in ctouches if t.kind not in cadence._CLOCK_SILENT_KINDS]
        if not real:
            continue
        outbound = sum(1 for t in ctouches if t.kind in cadence._OUTBOUND_KINDS)
        if outbound == 0 or outbound >= max_cold:
            continue
        lt_date = real[-1].ts.date()
        if cadence.business_days_since(lt_date, today) >= followup_bd:
            continue  # already due today's queue would have surfaced it

        d = lt_date
        while cadence.business_days_since(lt_date, d) < followup_bd:
            d += timedelta(days=1)
        forecast[d] = forecast.get(d, 0) + 1

    if not forecast:
        return None
    earliest = min(forecast)
    return {"date": earliest, "count": forecast[earliest]}


def _quiet_line(wave: dict | None) -> str:
    """The one sentence a genuinely quiet Today page shows — never a nag,
    never an empty grid. `wave` is `_next_wave`'s result. No LLM and no
    prose variants beyond this one: a date computation rendered as a
    sentence, in the product's own copy voice (sentence case, no em dash)."""
    if not wave:
        return "Quiet on the cadence. Nothing on deck right now."
    n = wave["count"]
    when = f"{wave['date'].strftime('%a %b')} {wave['date'].day}"
    verb = "lands" if n == 1 else "land"
    plural = "" if n == 1 else "s"
    return f"Quiet on the cadence. Next wave: {n} follow-up{plural} {verb} {when}."


def _cockpit_context(user) -> dict:
    """The Today cockpit: a capped, momentum-ordered daily plan in three
    semantic lanes, an honest held-back remainder, a weekly pace figure, the
    chats that are already on the calendar, and a recent-activity feed."""
    today = timezone.localdate()
    actions, contacts = _build_actions(user)
    # The bench (see `_opening_bench`'s module note above it): at most one
    # parked chatted/advocate contact, drawn back into view by a live opening
    # at their firm today. Computed off `actions`/`contacts` already loaded
    # above — no re-run of the cadence engine.
    bench = _opening_bench(user, contacts, actions, today)
    pace = _pace(user, today)
    cap = _daily_cap(pace["goal"], pace["done"], today)

    for a in actions:
        a["touch_kind"] = _ACTION_TOUCH.get(a["action"])
        # Whether the card may offer Snooze/Skip. A card whose action is
        # snooze-exempt must not draw the buttons: writing snoozed_until on
        # an exempt card is worse than a no-op, it silently snoozes the
        # contact's OTHER actions while the visible card stays put.
        a["snoozable"] = a["action"] not in _SNOOZE_EXEMPT_ACTIONS

    ordered = sorted(actions, key=_today_sort_key)

    # STALENESS DECAY FOR THE CRITICAL LANE (see `_stale_critical`). A critical
    # prompt that has gone unanswered for three working weeks stops holding a
    # critical slot and moves to its own strip.
    #
    # WHY IT GOES TO A STRIP RATHER THAN BACK INTO THE RANKED PLAN. Demoting a
    # stuck card into `rest` would rank it on `ev`, and `ev` measures the
    # RELATIONSHIP, not the freshness of the prompt: Leo Ziqiang Yuan scores
    # 7.2 because he is a tier-1 contact who has chatted, and a three-week-old
    # "did it happen?" would have walked straight back into the plan ahead of
    # real work with one extra step in between. The card is not worth a slot;
    # it is worth being findable. So it keeps its full controls and its own
    # heading, and costs the day nothing.
    #
    # WHY IT IS NOT ARCHIVED, SNOOZED OR AUTO-RESOLVED. The question is still
    # open — somebody scheduled a chat and nobody wrote down what happened —
    # and only the student can answer it. Deciding on their behalf that the
    # chat did or did not happen is the single largest claim this page can
    # make (the same reason `_ACTION_TOUCH` refuses to map `confirm_chat` to a
    # one-click "chat"). Decay changes where the card sits, never what is true.
    #
    # WHY HERE AND NOT IN `coverage_domain.cadence`. The layering rule is
    # engine says what is DUE, this layer says what is worth TODAY. The chat
    # IS still unconfirmed, so branch 2 is right to keep returning it and must
    # keep returning it — suppressing it there would delete the only record
    # that an unresolved scheduled chat exists, and would need a golden-fixture
    # rewrite for a question the engine was never asked. "How long has this
    # prompt been on screen unanswered" is not a property of the contact; it is
    # a property of the queue's own asking, and the engine has no notion of
    # having asked. It is also this layer that grants the exemption in the
    # first place (`_is_critical`, `_TODAY_CLASS`), so the qualifier belongs
    # beside it. No engine change, no fixture change.
    for a in ordered:
        a["stale_critical"] = _stale_critical(a, today)
        if a["stale_critical"]:
            a["reason"] = _stale_critical_reason(a)

    park = [a for a in ordered if _today_class(a) == CLASS_PARK]
    still_open = [
        a for a in ordered if _today_class(a) != CLASS_PARK and a["stale_critical"]
    ]
    critical = [
        a for a in ordered
        if _today_class(a) != CLASS_PARK and _is_critical(a) and not a["stale_critical"]
    ]
    # Unchanged by the decay: `_stale_critical` can only ever be true for a
    # card `_is_critical` already covers, so nothing new falls in here.
    rest = [
        a for a in ordered
        if _today_class(a) != CLASS_PARK and not _is_critical(a)
    ]

    # Fill rule: CLASS_CRITICAL is always shown in full, even past the cap — a
    # confirmed deadline is never something the page decides you'll get to
    # tomorrow. Whatever slots remain fill from CLASS_ENGAGED then CLASS_COLD
    # in sort order, so the oldest-silent cold contacts drain FIFO across the
    # week instead of a 31-card batch landing whole.
    #
    # ONE EXCEPTION, and it is the "rare" half of the keep-warm decision (the
    # reason strings in `crm.relevance` are the "reason-seeking" half). A
    # keep-warm card whose firm has nothing live at it — no confirmed date, no
    # deadline on a role this student could apply for, no role opened there
    # this week — is the product asking for an email it cannot justify. At most
    # QUIET_UPKEEP_PLAN_MAX of those reach a plan slot on any one day; the rest
    # pace out under "Up next".
    #
    # Capped rather than demoted, deliberately. Sorting them behind everything
    # else would have put ten cold Citi follow-ups above the one person who
    # actually sat down with the student — the exact tier-over-momentum
    # inversion `_TODAY_CLASS` was written to kill, arriving by a different
    # door. A warm contact still leads a quiet day. There is just never a wall
    # of them.
    slots = max(0, cap - len(critical))
    planned_rest: list[dict] = []
    held: list[dict] = []
    quiet_used = 0
    for a in rest:
        quiet = a["action"] in ("keep_warm", "maintain") and not a.get("opening")
        if len(planned_rest) < slots and not (quiet and quiet_used >= QUIET_UPKEEP_PLAN_MAX):
            planned_rest.append(a)
            quiet_used += 1 if quiet else 0
        else:
            held.append(a)
    planned = critical + planned_rest

    planned_lanes = {key: [] for key, _ in _TODAY_LANES}
    for a in planned:
        planned_lanes["critical" if _is_critical(a) else
                      ("cold" if _today_class(a) == CLASS_COLD
                       else "momentum")].append(a)
    held_by_lane: dict[str, int] = {key: 0 for key, _ in _TODAY_LANES}
    for a in held:
        held_by_lane["cold" if _today_class(a) == CLASS_COLD else "momentum"] += 1

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

    # People the mailbox scan judged worth tracking, waiting for a tap.
    # PROPOSALS, not contacts — nothing exists in the CRM until accept (see
    # capture/discovery.py). Capped as a rendering guard only; the judgment
    # chain usually keeps real volume below it, but a first whole-mailbox
    # scan does not (205 on the founder's own real mailbox in one pass), so
    # the lane count says "24 of 205" rather than a bare 24, and the bulk
    # buttons act on exactly the rendered slice — see `rendered_proposals_qs`
    # for the ordering rule (evidence recency, not scan insertion order) and
    # `PROPOSALS_RENDER_CAP` for why the slice itself has to match.
    pending_proposals = rendered_proposals_qs(user)
    proposals_total = pending_proposals.count()
    proposals = list(
        pending_proposals.select_related("firm")[:PROPOSALS_RENDER_CAP]
    )

    # Autopilot's reviewed batch, if one is waiting — the strip that turns
    # this lane into one tap. Two reads, both display-only:
    #
    #   * the newest REVIEWED run with accepts: its counts and its id are
    #     the strip ("N ready to add — one tap"), and the tap POSTs to
    #     crm:autopilot_apply, which is capture.autopilot.apply_run behind
    #     one button. Decide never wrote to the CRM; this tap is the only
    #     thing that does. (See capture/autopilot.py's module docstring for
    #     why the tap survives every hands-off ambition: it IS the Limited
    #     Use posture.)
    #   * every pending proposal that carries an ESCALATE decision gets the
    #     AI's own quoted reason pinned to its card (`autopilot_quote`),
    #     whatever run it came from — the card is the escalation path, and
    #     a card that says only "Not in your network" would hide the one
    #     line ("Somil Agarwal is no longer with Allen & Company…") that
    #     makes the decision quick.
    from capture.models import AutopilotDecision, AutopilotRun

    autopilot_review = None
    # ALL reviewed runs, not just the newest: two can coexist (a second
    # decide pass over rows the first never saw), and showing only the
    # newest made the older one a decision disclosed to nobody — reviewed,
    # waiting, and invisible until applied. The strip discloses the sum,
    # and the tap (`autopilot.apply_reviewed_through`, behind
    # crm:autopilot_apply on the NEWEST run's pk) applies every reviewed
    # run up to the one it names, so the number shown is exactly the
    # number applied.
    ap_runs = list(
        AutopilotRun.objects.for_user(user)
        .filter(status=AutopilotRun.STATUS_REVIEWED)
        .order_by("created")
    )
    ap_accepts = sum(r.accepts for r in ap_runs)
    if ap_runs and ap_accepts:
        autopilot_review = {
            "run": ap_runs[-1],
            "accepts": ap_accepts,
            "escalations": sum(r.escalations for r in ap_runs),
            "evidence_note": ap_runs[-1].evidence_note,
        }
    # The OTHER four states — nothing to do, startable (with its price),
    # deciding, stopped. Computed in `capture.autopilot.today_state` rather
    # than here so the strip has one source of truth and this module keeps
    # one line. See that function for what each phase means.
    from capture import autopilot as autopilot_service

    autopilot_state = autopilot_service.today_state(user)
    if proposals:
        ap_notes = {}
        for d in (
            AutopilotDecision.objects.for_user(user)
            .filter(
                proposal_id__in=[p.pk for p in proposals],
                decision=AutopilotDecision.DECIDE_ESCALATE,
            )
            .order_by("created")
        ):
            ap_notes[d.proposal_id] = d
        for p in proposals:
            note = ap_notes.get(p.pk)
            p.autopilot_quote = note.quote if note else ""

    # The other half of "found in your inbox": an ATS saying one of the
    # student's applications moved. PROPOSALS again — nothing is written to
    # UserOpportunity until the tap (see capture/appmail.py). Same rendering
    # cap and the same reasoning; the one-believable-role rule keeps real
    # volume far below it.
    from capture.appmail import EVENT_LABELS
    from capture.models import ApplicationEvent

    app_events = []
    for row in (
        ApplicationEvent.objects.for_user(user)
        .filter(status=ApplicationEvent.STATUS_PENDING)
        .select_related("firm", "opportunity")
        .order_by("created")[:24]
    ):
        label, action = EVENT_LABELS.get(
            row.event_type, (row.get_event_type_display(), "Update")
        )
        app_events.append({"row": row, "label": label, "action": action})

    # What the mail itself STATED — departures, out-of-office returns,
    # routing addresses (capture.mailfacts). Two card shapes in one lane:
    # `applied` rows announce an automatic, reversible action and carry the
    # verbatim quote that justified it plus an Undo; `pending` rows are the
    # no-quote / nothing-to-change fallback, surfaced instead of acted on. A
    # referral fact that spawned a proposal is NOT re-carded here — its
    # quote renders on the proposal card itself (see below), one person, one
    # card.
    from capture.models import MailFact

    mail_facts = [
        {"row": f, "label": f.get_kind_display(), "undoable": f.status == MailFact.STATUS_APPLIED}
        for f in (
            MailFact.objects.for_user(user)
            .filter(status__in=[MailFact.STATUS_PENDING, MailFact.STATUS_APPLIED])
            .exclude(kind=MailFact.KIND_REFERRAL, proposal__isnull=False)
            .select_related("contact", "proposal")
            .order_by("created")[:24]
        )
    ]
    # The referral quote, onto the proposal card it belongs to. One query,
    # only when there are proposals to annotate.
    if proposals:
        quotes = {
            f.proposal_id: f.quote
            for f in MailFact.objects.for_user(user)
            .filter(proposal__in=proposals)
            .exclude(quote="")
        }
        for p in proposals:
            p.referral_quote = quotes.get(p.id, "")

    # E10: when one contact holds both a debrief and a thank-you, the two
    # cards stop pretending not to know about each other.
    debriefs = debrief_svc.pending(user)
    debrief_contact_ids = {d["contact"].id for d in debriefs}
    for a in planned + held + still_open:
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
    chat_prep = _chat_prep(user, today, schedule)

    # THE GATE. Both halves, because an empty queue means two opposite things
    # (see the seeds header above) and only the pair tells them apart.
    #
    # SILENT: no planned lane, no chat to prep, no debrief to write, and
    # nothing pacing out behind the cap. `park` is deliberately not in this
    # test — a "Gone quiet" strip is a list of EXITS, not forward work, so a
    # queue that holds only parks is silent in every sense that matters.
    #
    # STILL BEING BUILT: fewer than SEED_NETWORK_FLOOR live contacts. Without
    # this, an account that has snoozed thirty real people gets told to go add
    # three more — beginner advice overwriting a message the student earned.
    # `contacts` is the live, non-archived list `_build_actions` already
    # loaded, so this half of the rule costs no query at all.
    #
    # The gate is also the cost control: every query `_starter_seeds` runs
    # happens inside this branch, so a student with a working queue — the
    # common case, and the one a perf pass just cleaned up — pays exactly
    # nothing for this feature.
    #
    # `still_open` IS in the test, and that is the line between it and `park`.
    # A parked contact is a decision the student already made; a stale
    # `confirm_chat` is a question they still owe an answer to. A queue holding
    # nothing but stuck prompts is not a silent queue, and telling that student
    # to go add three people at Goldman would talk straight past the two things
    # actually waiting on them.
    seeds = (
        _starter_seeds(user, today)
        if (not lanes and not debriefs and not chat_prep and not held
            and not still_open
            and len(contacts) < SEED_NETWORK_FLOOR)
        else []
    )

    # Whether this Today page has genuinely nothing on it. Two distinct
    # empty stories already exist and both must keep their earned copy
    # unchanged (see test_today.py / test_today_seeds.py):
    #
    #   - "You're all caught up": every due contact was handled or snoozed
    #     by hand. Deliberate, earned, and true — must not be overwritten
    #     just because there is no CONCRETE forecast to name.
    #   - "Done for today ... N contacts have gone quiet; park them below":
    #     a real park backlog is a decision waiting on the student, not
    #     nothing. `park` therefore gates this exactly like `lanes`/`held`/
    #     `still_open` — the fix here is narrower than "a park-only queue is
    #     silent" (that's the SEED gate's own, different question above).
    #
    # This header exists to say ONE new thing neither of those says: a
    # concrete date and count for what's coming. So it only takes over when
    # there both (a) is nothing else on the page and (b) `_next_wave` found
    # an actual future wave to name — a queue with no forecast at all keeps
    # falling through to whichever of the two existing lines already fits
    # it, which is the more honest sentence when there is truly nothing to
    # predict.
    #
    # `plays` and `bench` are two sibling sections landing on this same page
    # from separate work — this function has no way to name variables that
    # may not exist yet, so it doesn't try. `_cockpit.html`'s own `{% if %}`
    # ANDs this flag with `not plays and not bench`, which Django evaluates
    # as true whenever those keys are absent; add `and not <your_section>`
    # there when your section lands so the header keeps composing correctly.
    would_be_quiet = (
        len(contacts) > 0
        and not seeds
        and not lanes
        and not held
        and not still_open
        and not park
        and not debriefs
        and not chat_prep
        and not proposals
        and not app_events
        and not mail_facts
        and not autopilot_review
    )
    # The one query this feature costs is gated behind everything above,
    # same cost discipline as `_starter_seeds`: a working queue, a park
    # backlog, or a handled/snoozed queue all skip it entirely.
    wave = _next_wave(user, today) if would_be_quiet else None
    quiet = would_be_quiet and wave is not None
    quiet_line = _quiet_line(wave) if quiet else ""

    # Every contact the queue is already speaking about. "Waiting on reply" is
    # the page's silent bucket, so it must not re-list somebody who has a card
    # six inches above it.
    busy_ids = {a["contact"]["id"] for a in planned + held + park + still_open}
    busy_ids |= debrief_contact_ids
    busy_ids |= {r["contact"].id for r in schedule if r["contact"]}
    # Campaign-excluded contacts are excluded here too. "Waiting on reply" is
    # a daily anxiety surface ("did I drop anything?"), and a reply to a club
    # panel invitation was never owed in the recruiting sense. Measured on the
    # audit account: classifying the ICC merge "not my recruiting" emptied the
    # queue and left all 16 unanswered panelists sitting under "Waiting on
    # reply, nothing due yet" forever — on the founder's real account that is
    # 190-odd club recipients drowning every genuine recruiting wait. They
    # keep the contact book, search, history and every export, same as the
    # queue rule (`crm/campaigns.py`). The Network board hides them and says
    # so; nothing anywhere deletes them.
    busy_ids |= campaigns.excluded_contact_ids(user)
    # And the recruitment-hidden, for the identical reason: a reply the WRIT
    # 150 professor never sent was never owed in the recruiting sense, and
    # "Waiting on reply" listing them forever would be the queue rule
    # (`crm/recruitment.py`) disagreeing with its own board.
    busy_ids |= recruitment.hidden_contact_ids(user)

    return {
        "lanes": lanes,
        "proposals": proposals,
        "proposals_total": proposals_total,
        "app_events": app_events,
        "mail_facts": mail_facts,
        "autopilot_review": autopilot_review,
        "autopilot_state": autopilot_state,
        "planned_total": len(planned),
        "held": held,
        "held_total": len(held),
        # Criticals that aged out of the exemption. Not `held` — held is work
        # queued for a day soon and paces out at the cap; these are questions
        # already overdue that the plan has stopped budgeting for. Filing them
        # under "more queued" would promise a morning on which they arrive,
        # and there is no such morning.
        "still_open": still_open,
        "still_open_total": len(still_open),
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
        "chat_prep": chat_prep,
        # Day-one seeds: concrete first moves derived from state on every
        # render, never stored. Empty for any account whose queue has work
        # in it. See `_starter_seeds`.
        "seeds": seeds,
        # Dated world facts joined to the student's own people at that firm —
        # see `_plays`'s own module note for the gap this closes. Capped at
        # PLAYS_MAX and independent of the queue/cap machinery above: a play
        # can render on a day the cadence queue is completely empty.
        "plays": _plays(user, today),
        # The quiet-day header (see the `quiet` computation above): `line`
        # is only meaningful when `quiet` is true, but it's cheap enough
        # (empty string otherwise) to hand over unconditionally rather than
        # make the template guard two keys instead of one.
        "quiet": quiet,
        "quiet_line": quiet_line,
        "deadlines": _next_deadlines(user, today),
        "new_at_firms": _new_at_your_firms(user),
        "waiting": _waiting_on_reply(user, busy_ids),
        "activity": activity,
        "contact_count": len(contacts),
        # The bench: parked contacts are not gone. At most one card — see
        # `_opening_bench` and `BENCH_PLAN_MAX`.
        "bench": bench,
        # The raw, uncapped queue — carried through only so week() (the full
        # page view, below) can hand it to the daily brief. Deliberately NOT
        # used by _cockpit.html itself: that template is also rendered by
        # crm.views' htmx partial refresh (e.g. after dismissing a debrief),
        # and generating a brief is a real LLM call that has no business
        # firing as a side effect of an unrelated card action.
        "_actions_for_brief": actions,
    }


@login_required
def week(request: HttpRequest) -> HttpResponse:
    """Today: a capped daily plan in three semantic lanes, an honest "Up next"
    remainder, and a rail carrying the weekly pace ring, the chats already on
    the calendar, and recent activity. The commodity layer (directory stats)
    sits BELOW the queue — Today is the relationship page."""
    cockpit = _cockpit_context(request.user)
    # THE BRIEF IS NOT GENERATED HERE. It used to be, and that put a
    # synchronous Anthropic call on the request path of the page students
    # open every morning: measured, the model's latency landed on the
    # response almost exactly 1:1 (55.7ms with the row already present,
    # 2079.9ms when the reply took 2.0s), and `assistant.client`'s timeout
    # means the worst case is a 45-SECOND Today page. Once per user per day,
    # on the first load of the day — which is the morning load, i.e. the one
    # that sets the whole day's impression of how fast this product is.
    #
    # So: render whatever is already cached (one indexed read, free), and
    # let `crm.views.daily_brief` do the generating over htmx once the page
    # is interactive. `is_pending` is what tells the template whether to draw
    # the placeholder that fetches it — without that check a dark deploy (no
    # API key) would render a spinner that never resolves.
    #
    # assistant.brief never imports anything from crm — this app already
    # imports FROM assistant (assistant.tools reads crm.today._build_actions),
    # so the reverse import has to stay one-directional or the two apps
    # import each other. assistant.situation is the same story: pure reads,
    # no LLM, but it still only belongs on the full page load — see its own
    # module docstring on why it isn't cached, and why it still must not fire
    # from the htmx partial refresh (a bug here degrades gracefully to no
    # cards, but it is a query cost the partial-refresh path has no business
    # paying on every card click).
    from assistant.brief import get_cached as get_cached_brief, is_pending as brief_pending
    from assistant.situation import build_situation

    situation = build_situation(request.user)
    cockpit.pop("_actions_for_brief", None)
    daily_brief = get_cached_brief(request.user)
    return render(
        request,
        "crm/week.html",
        {**cockpit, **_dashboard_context(request.user),
         "daily_brief": daily_brief,
         # Only when there is nothing cached AND the feature is live.
         "daily_brief_pending": daily_brief is None and brief_pending(request.user),
         # Capped to 3 for the card strip — same number the brief's own
         # queue-card cap uses (assistant.brief.MAX_SITUATION_SUMMARIZED),
         # so the sentence above never references a 4th change nobody can
         # see a card for. `situation["events"]` is already priority-ordered
         # (role_closed, then deadline_moved, then new_role_at_known_firm).
         "situation_events": situation.get("events", [])[:3],
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
    actions, _ = _build_actions(request.user)
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


@login_required
@require_POST
def play_dismiss(request: HttpRequest) -> HttpResponse:
    """Dismiss one Today play, keyed on the FACT — `(firm, event_kind, date)`
    — never on the card's rendered position or a row id.

    THE WHOLE POINT. `PlayDismissal` writes the values, not a foreign key to
    the `FirmDate` row: a firm's board gets re-scraped and that row is edited
    IN PLACE, so keying on its id would keep suppressing a fact whose date has
    since moved. Storing the date as a value means a later, DIFFERENT date is
    a different tuple and the play returns on its own — no undo needed,
    nothing to expire. See `crm.models.PlayDismissal` and `_plays`.
    """
    try:
        firm_id = int(request.POST.get("firm", ""))
        fact_date = date.fromisoformat(request.POST.get("date", ""))
    except (TypeError, ValueError):
        return HttpResponse(status=400)
    event_kind = (request.POST.get("event_kind") or "").strip()
    if not event_kind:
        return HttpResponse(status=400)
    get_object_or_404(Firm, pk=firm_id)
    PlayDismissal.all_objects.get_or_create(
        user=request.user, firm_id=firm_id, event_kind=event_kind, date=fact_date,
    )
    record_event(
        "play_dismissed", user=request.user, firm_id=firm_id,
        event_kind=event_kind, date=fact_date.isoformat(),
    )
    return render(request, "crm/_cockpit.html", _cockpit_context(request.user))


@login_required
@require_POST
def today_bench_act(request: HttpRequest, pk: int, verb: str) -> HttpResponse:
    """The bench's two equal-weight taps (see `_opening_bench`):

    - `restore`: the ONLY thing that un-parks a bench contact — no timer
      ever does. Goes through the same audited manual-override path every
      other thread_state change in this module uses, restoring
      `thread_state` to the resting value their own `warmth` implies
      (`BENCH_RESTORE_STATE`), never a guess.
    - `leave`: the equal-weight alternative. Writes a `BenchDismissal` row
      for this contact and THIS opening only — the student's original park
      decision is respected by default, and a fresh opening at the same firm
      later is a new question, not a repeat of this one.

    Re-derives the bench from the engine rather than trusting anything about
    the click beyond the contact id, the same posture `today_park_all` takes
    with the park strip: a stale card (the opening already gone, or someone
    else's tenant id) simply matches nothing and this is a no-op re-render.
    """
    if verb not in ("restore", "leave"):
        return HttpResponse(status=400)
    contact = get_object_or_404(Contact.objects.for_user(request.user), pk=pk)
    cockpit = _cockpit_context(request.user)
    candidate = next(
        (b for b in cockpit["bench"] if b["contact"]["id"] == contact.id), None
    )
    if candidate is not None:
        if verb == "restore":
            services.set_contact_state(
                request.user.id, contact.id,
                thread_state=candidate["restore_state"],
                note=f"Un-parked from the bench — {candidate['firm_name']}: "
                     f"{candidate['reason']}".strip(),
            )
            record_event("bench_restored", user=request.user, source="today")
            cockpit = _cockpit_context(request.user)
        else:
            BenchDismissal.all_objects.create(
                user=request.user, contact=contact,
                opening_signature=candidate["opening_signature"],
            )
            record_event("bench_dismissed", user=request.user, source="today")
            cockpit = _cockpit_context(request.user)
    return render(request, "crm/_cockpit.html", cockpit)


_PAREN = _re.compile(r"\s*\([^)]*\)")
_HOURS_AGO = _re.compile(r"\b\d+h ago\b")
_ISO_DATE = _re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")


def _prose_dates(reason: str, *, today=None) -> str:
    """Rewrite the engine's `close.isoformat()` as the date the rest of the
    card already speaks: "app closes 2026-08-30" -> "app closes Aug 30".

    The reping card was the one place on the page saying a date in ISO — its
    own deadline chip two lines up says "Closes Aug 30" for the same row, so
    one card spoke the same date two ways. Same posture as `_age_in_days`:
    the engine's raw fragment stays untouched at the source (coverage_domain
    is another workstream), and this is purely presentation. The year is kept
    only when it is not this year — a January queue looking at an August
    close needs it, today's queue does not."""
    if not reason:
        return reason
    this_year = (today or timezone.localdate()).year

    def _fmt(match: _re.Match) -> str:
        from datetime import date as _date

        try:
            d = _date(int(match[1]), int(match[2]), int(match[3]))
        except ValueError:
            return match[0]
        base = f"{d.strftime('%b')} {d.day}"
        return base if d.year == this_year else f"{base}, {d.year}"

    return _ISO_DATE.sub(_fmt, reason)
# Past this, an hour count stops being a measurement and starts being
# arithmetic homework. The thank-you window is 24h, so anything inside two
# days still earns hours; beyond that the surface speaks days like everything
# else on it.
_AGE_HOURS_MAX = 48.0


def _age_in_days(reason: str, hours: float | None, *, now) -> str:
    """Rewrite the engine's "58h ago" as "2d ago" once the hours stop earning
    their place.

    The cadence engine measures the thank-you branch in hours because the
    window it enforces IS hours (`chat done 58h ago — send thank-you (within
    24h)`), and `_sentenceize` strips every parenthetical — so the window that
    justified the unit never reaches the screen while the bare hour count
    does. One chat then rendered three ways in one scroll: "Chatted 2d ago"
    on the Debrief card, "Chat done 58h ago" here, and "2 business days ago"
    on this same card's ledger row.

    Days are derived from the calendar date, not from `hours / 24`, so this
    agrees with the Debrief card's own `(today - chat_date).days` rather than
    rounding past it. The engine's raw fragment stays untouched at the source
    (coverage_domain is another workstream) — the action dict already carries
    `hours` structurally, which is what makes this a presentation-layer fix."""
    if not reason or hours is None or hours < _AGE_HOURS_MAX:
        return reason
    when = timezone.localtime(now - timedelta(hours=hours)).date()
    days = (timezone.localtime(now).date() - when).days
    return _HOURS_AGO.sub(f"{days}d ago", reason)


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
    (campus openings, deadlines) plus the user's own application funnel.

    `open_now`/`tracked_live`/`hk`/`us` used to sit here: three of four cells
    describing the BOARD (1,053 open, 5,291 tracked including 4,238 roles the
    Opportunities feed itself hides as not-yours) on a page whose only job is
    "what does Jimmy do today". The HK/US split never moved day to day and
    the corpus size is inventory, not a task. Meanwhile the pipeline count
    below was 0 — the fact this page never said.

    Replaced with two personal numbers already computed for Opportunities:
    open roles at the firms this user actually targets, and how many of
    those name their own class year and are still unsaved. The board-wide
    figures still exist for the founder view at /instrument/.
    """
    today = timezone.localdate()

    campus = Opportunity.objects.filter(status="open", bucket__in=TARGET_BUCKETS)
    closing_10 = campus.filter(deadline__range=(today, today + timedelta(days=9))).count()

    firm_ids = set(UserFirm.objects.for_user(user).values_list("firm_id", flat=True))
    # Minus the ones this student has said are not for them. This cell is a
    # personal number — "open roles at the firms YOU target" — and it links
    # to a feed that hides those rows, so counting them here made the ribbon
    # promise more than the board it sends you to could show. `closing_10`
    # beside it is deliberately NOT filtered: it is a board-wide figure about
    # the market's calendar, not a list of things for this student to do.
    dismissed_ids = set(
        UserOpportunity.objects.for_user(user)
        .filter(dismissed=True)
        .values_list("opportunity_id", flat=True)
    )
    at_your_firms = (
        campus.filter(firm_id__in=firm_ids).exclude(id__in=dismissed_ids).count()
        if firm_ids else 0
    )

    # Local import: directory.views owns the eligibility verdict and the
    # Opportunities-page count it feeds; crm.today borrows the same function
    # rather than re-deriving the contract, so the two pages can never
    # disagree about what "names your year" means.
    # `_STAGE_LABELS` for the same reason: directory.views owns the one
    # vocabulary for what a pipeline stage is CALLED, and the ribbon's funnel
    # label used to be a hardcoded literal that spelled two of them the way
    # the database does — "Submitted › Interview › Offer" against the
    # "Applied"/"Interviewing" every other surface shows for the identical
    # rows. Reading the label instead of restating it is what stops it
    # drifting again.
    from directory.views import (
        _FUNNEL_STATES, _STAGE_LABELS, _eligibility_profile,
        _eligible_unsaved_count,
    )

    elig_profile = _eligibility_profile(user)
    # FOLDED FIRST, like the feed. This chip and the Opportunities banner
    # answer one question ("how many open roles name your year and aren't
    # saved") and gave two answers to it — 209 here against the feed's 206 —
    # because the feed counts its materialised rows AFTER `fold_duplicates`
    # and this counted the raw queryset. A board scraped twice in one week
    # carries the same requisition twice; it is one role to a student, and
    # the number that leads them to the "Save them all" banner has to be the
    # number that banner then states.
    eligible_unsaved = 0
    if elig_profile and elig_profile.get("class_year"):
        folded, _ = fold_duplicates(campus)
        eligible_unsaved = _eligible_unsaved_count(user, folded, elig_profile)

    uo = UserOpportunity.objects.for_user(user)
    funnel = {
        state: uo.filter(applied_status__iexact=state).count()
        for state in _FUNNEL_STATES
    }
    funnel_label = " › ".join(_STAGE_LABELS[state] for state in _FUNNEL_STATES)

    return {
        "dash": {
            "at_your_firms": at_your_firms,
            "closing_10": closing_10,
            "eligible_unsaved": eligible_unsaved,
            # Whether the eligibility check RAN. Zero eligible-unsaved means
            # two very different things — "you saved them all" versus "you
            # never told us your year, so nothing was checked" — and the
            # ribbon cell must not say "caught up" about a check that never
            # happened.
            "has_year": bool(elig_profile and elig_profile.get("class_year")),
            "funnel": funnel,
            "funnel_label": funnel_label,
        },
    }


